"""Tests for the calibration wizard. Input, tones and every audio command are simulated."""

import builtins
import contextlib
import io
import os
import struct
import subprocess
import sys
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401
from hermetic import FakeClock, HermeticTestCase, sinks_short  # noqa: E402

import hda_split.config as cfg_mod  # noqa: E402
import wizard  # noqa: E402

wizard_generate = wizard.generate_probe_tone  # the real one, captured before any patching
NODE = "alsa_output.pci-0000_65_00.6.pro-output-0"
CARD_NAME = "alsa_card.pci-0000_65_00.6"
CARD = {
    "id": "2", "short": "PCH", "long": "HDA Intel PCH", "codecs": "Realtek ALC295",
    "is_analog_hda": True, "pci_id": "0000_65_00.6", "card_name": CARD_NAME, "node_target": NODE,
}
GPU = {
    "id": "1", "short": "NVidia", "long": "HDA NVidia", "codecs": "Nvidia GPU HDMI",
    "is_analog_hda": False, "pci_id": "0000_01_00.1", "card_name": "alsa_card.pci-0000_01_00.1",
    "node_target": "alsa_output.pci-0000_01_00.1.pro-output-0",
}


def cards_listing(profile):
    return (
        f"Card #49\n\tName: alsa_card.pci-0000_01_00.1\n\tActive Profile: off\n\n"
        f"Card #50\n\tName: {CARD_NAME}\n\tDriver: alsa\n\tActive Profile: {profile}\n"
    )


def automute_listing(value):
    return (
        "Simple mixer control 'Auto-Mute Mode',0\n  Capabilities: enum\n"
        "  Items: 'Disabled' 'Speaker Only' 'Line Out+Speaker'\n"
        f"  Item0: '{value}'\n"
    )


def ports_listing(n=4, node=NODE):
    return "\n".join(f"{node}:playback_AUX{i}" for i in range(n)) + "\nother.node:playback_FL"


class WizardBase(HermeticTestCase):
    profile = "output:analog-stereo"
    automute = "Enabled"
    aux_ports = 4

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.out = io.StringIO()
        self.prompts = []
        self.answers = []
        self.cards = [CARD]
        for p in (
            mock.patch.object(wizard, "time", self.clock),
            mock.patch.object(wizard, "generate_probe_tone", self._fast_tone),
            mock.patch.object(wizard, "discover_hda_cards", lambda: list(self.cards)),
            mock.patch.object(builtins, "input", self._input),
            mock.patch.object(sys, "stdout", self.out),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.sp.allow(
            ["pactl", "set-card-profile"], ["amixer", "-c"], ["systemctl", "--user", "restart"], ["pw-play"],
        )
        self.set_state()
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(["Headphones", "Speakers", "Both"]))

    @staticmethod
    def _fast_tone(path, active, **kw):
        wizard_generate(path, active, duration_sec=0.01, gap_sec=0.01)

    def set_state(self):
        self.sp.stub(["pactl", "list", "cards"], stdout=cards_listing(self.profile))
        self.sp.stub(["amixer", "-c", "2", "sget", "Auto-Mute Mode"], stdout=automute_listing(self.automute))
        self.sp.stub(["pw-link", "-i"], stdout=ports_listing(self.aux_ports))

    def _input(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def run_wizard(self):
        """Runs the wizard; returns the SystemExit code (None if it returned normally)."""
        try:
            wizard.run_wizard()
        except SystemExit as e:
            return e.code
        return None

    # --- assertions over the recorded commands
    def played(self):
        return self.sp.find("pw-play")

    def assertNoConfigWritten(self):
        self.assertFalse(self.paths.CONFIG_FILE.exists(), "config.env must not exist")
        self.assertFalse(self.paths.PW_DROPIN_FILE.exists(), "drop-in must not exist")

    def saved(self):
        return cfg_mod.load_config()

    def assertRestored(self, profile=None, automute=None):
        profiles = self.sp.find("pactl", "set-card-profile")
        automutes = [a for a in self.sp.find("amixer") if "Auto-Mute Mode" in a and "set" in a]
        if profile is not None:
            self.assertEqual(profiles[-1], ["pactl", "set-card-profile", CARD_NAME, profile])
            self.assertEqual(profiles[0][-1], "pro-audio")
        if automute is not None:
            self.assertEqual(automutes[-1][-3:], ["set", "Auto-Mute Mode", automute])


class ProbeToneTests(HermeticTestCase):
    def read(self, path):
        with wave.open(str(path), "rb") as wf:
            self.assertEqual(wf.getsampwidth(), 2)
            n = wf.getnchannels()
            raw = wf.readframes(wf.getnframes())
            rate = wf.getframerate()
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
        return n, rate, [samples[i::n] for i in range(n)]

    def make(self, active, **kw):
        path = self.base / "tone.wav"
        wizard.generate_probe_tone(path, active, **kw)
        return self.read(path)

    def test_tone_is_four_channel_48k(self):
        n, rate, _ = self.make([0, 1], duration_sec=0.1, gap_sec=0.05)
        self.assertEqual((n, rate), (4, 48000))

    def test_tone_has_signal_only_on_active_channels(self):
        for active in ([0, 1], [2, 3], [0, 2], [1, 3]):
            with self.subTest(active=active):
                _, _, ch = self.make(active, duration_sec=0.1, gap_sec=0.05)
                for i in range(4):
                    has_signal = any(v != 0 for v in ch[i])
                    self.assertEqual(has_signal, i in active, f"channel {i}")

    def test_tone_plays_active_channels_one_after_the_other_with_a_gap(self):
        _, _, ch = self.make([0, 2], duration_sec=0.1, gap_sec=0.05)
        nz0 = [i for i, v in enumerate(ch[0]) if v]
        nz2 = [i for i, v in enumerate(ch[2]) if v]
        self.assertLess(max(nz0), min(nz2), "pulses must not overlap")
        self.assertGreater(min(nz2) - max(nz0), int(0.05 * 48000) - 2, "a silent gap separates the pulses")
        self.assertEqual(len(ch[0]), 2 * int(0.1 * 48000) + int(0.05 * 48000))

    def test_tone_is_soft(self):
        _, _, ch = self.make([0], duration_sec=0.1)
        self.assertLessEqual(max(abs(v) for v in ch[0]), int(32767 * 0.2) + 1)
        self.assertEqual(ch[0][0], 0, "fade-in starts from silence")


class PwPlayTests(HermeticTestCase):
    def test_pw_play_targets_the_node_without_a_port_suffix_and_uses_four_channels(self):
        cmd = wizard.build_pw_play_cmd(NODE, Path("/tmp/x.wav"))
        self.assertEqual(cmd[0], "pw-play")
        self.assertEqual(cmd[cmd.index("--target") + 1], NODE)
        self.assertNotIn(":", cmd[cmd.index("--target") + 1])
        self.assertEqual(cmd[cmd.index("--channels") + 1], "4")
        self.assertEqual(cmd[cmd.index("--channel-map") + 1], "AUX0,AUX1,AUX2,AUX3")
        self.assertEqual(cmd[-1], "/tmp/x.wav")
        props = cmd[cmd.index("--properties") + 1]
        for token in ("stream.dont-remix=true", "node.dont-fallback=true"):
            self.assertIn(token, props)

    def test_play_test_tone_runs_pw_play_on_a_real_four_channel_wav(self):
        seen = {}

        def inspect(argv):
            with wave.open(argv[-1], "rb") as wf:
                seen["channels"], seen["frames"] = wf.getnchannels(), wf.getnframes()
            seen["path"] = argv[-1]
            return ""

        self.sp.stub(["pw-play"], stdout=inspect)
        self.assertTrue(wizard.play_test_tone(NODE, 2, 3))
        self.assertEqual(seen["channels"], 4)
        self.assertGreater(seen["frames"], 48000)
        self.assertFalse(os.path.exists(seen["path"]), "the temporary wav must be removed")
        kwargs = self.sp.calls[-1]["kwargs"]
        self.assertEqual(kwargs["timeout"], 20)

    def test_play_test_tone_reports_failures_instead_of_raising(self):
        for stub, label in (
            (dict(exc=FileNotFoundError("pw-play")), "missing"),
            (dict(exc=subprocess.TimeoutExpired("pw-play", 20)), "timeout"),
            (dict(rc=1, stderr="no target"), "exit 1"),
        ):
            with self.subTest(label), contextlib.redirect_stdout(io.StringIO()):
                self.sp.stub(["pw-play"], **stub)
                self.assertFalse(wizard.play_test_tone(NODE, 0, 1))


class CalibrationTests(WizardBase):
    def calibrate(self):
        return wizard.calibrate_aux_channels(NODE)

    def test_headphones_then_speakers_maps_aux01_to_headphones(self):
        self.answers = ["", "h", "", "s"]
        self.assertEqual(self.calibrate(), ("playback_AUX0", "playback_AUX1", "playback_AUX2", "playback_AUX3"))

    def test_speakers_then_headphones_swaps_the_mapping(self):
        self.answers = ["", "s", "", "h"]
        self.assertEqual(self.calibrate(), ("playback_AUX2", "playback_AUX3", "playback_AUX0", "playback_AUX1"))

    def test_pairs_are_probed_as_aux01_then_aux23(self):
        self.answers = ["", "h", "", "s"]
        self.calibrate()
        self.assertEqual(len(self.played()), 2)
        self.assertEqual(self.prompts[0][:5], "Press")

    def test_replay_and_invalid_answers_play_again_without_deciding(self):
        self.answers = ["", "r", "x", "replay", "h", "", "s"]
        self.calibrate()
        self.assertEqual(len(self.played()), 5)  # 4 plays for pair 1 (h on the 4th), 1 for pair 2

    def test_inconsistent_answers_are_rejected_and_abort_when_declined(self):
        for first, second in (("h", "h"), ("s", "s"), ("n", "n"), ("h", "n"), ("n", "s"), ("n", "h")):
            with self.subTest(first=first, second=second):
                self.answers = ["", first, "", second, "n"]
                with self.assertRaises(wizard.WizardAbort):
                    self.calibrate()

    def test_inconsistent_answers_can_be_retried(self):
        self.answers = ["", "h", "", "h", "", "", "h", "", "s"]  # retry prompt answered by ENTER (= yes)
        self.assertEqual(self.calibrate()[0], "playback_AUX0")


class RunWizardTests(WizardBase):
    def happy_answers(self, a="h", b="s"):
        return ["", a, "", b]

    def test_full_run_saves_mapping_original_state_and_dropin(self):
        self.answers = self.happy_answers("h", "s")
        self.assertIsNone(self.run_wizard())
        cfg = self.saved()
        self.assertEqual((cfg["HP_PORT_L"], cfg["HP_PORT_R"]), ("playback_AUX0", "playback_AUX1"))
        self.assertEqual((cfg["SPK_PORT_L"], cfg["SPK_PORT_R"]), ("playback_AUX2", "playback_AUX3"))
        self.assertEqual(cfg["ALSA_CARD_ID"], "2")
        self.assertEqual(cfg["PCI_ID"], "0000_65_00.6")
        self.assertEqual(cfg["CARD_NAME"], CARD_NAME)
        self.assertEqual(cfg["NODE_TARGET"], NODE)
        self.assertEqual(cfg["ORIGINAL_PROFILE"], "output:analog-stereo")
        self.assertEqual(cfg["ORIGINAL_AUTOMUTE"], "Enabled")
        self.assertTrue(self.paths.PW_DROPIN_FILE.exists())
        self.assertEqual(len(self.sp.find("systemctl", "--user", "restart", "pipewire")), 1)

    def test_swapped_answers_save_the_swapped_mapping(self):
        self.answers = self.happy_answers("s", "h")
        self.assertIsNone(self.run_wizard())
        cfg = self.saved()
        self.assertEqual((cfg["HP_PORT_L"], cfg["HP_PORT_R"]), ("playback_AUX2", "playback_AUX3"))
        self.assertEqual((cfg["SPK_PORT_L"], cfg["SPK_PORT_R"]), ("playback_AUX0", "playback_AUX1"))
        self.assertIn("audio.position = [ AUX2 AUX3 ]", self.paths.PW_DROPIN_FILE.read_text().split("Speakers")[0])

    def test_inconsistent_answers_are_never_saved_and_state_is_restored(self):
        for first, second in (("h", "h"), ("s", "s"), ("n", "n"), ("h", "n")):
            with self.subTest(first=first, second=second):
                self.sp.reset()
                self.answers = ["", first, "", second, "n"]
                self.assertEqual(self.run_wizard(), 1)
                self.assertNoConfigWritten()
                self.assertRestored(profile="output:analog-stereo", automute="Enabled")
                self.assertEqual(self.sp.find("systemctl"), [])

    def test_retry_after_inconsistent_answers_can_still_succeed(self):
        self.answers = ["", "h", "", "h", "y", "", "h", "", "s"]
        self.assertIsNone(self.run_wizard())
        self.assertEqual(self.saved()["HP_PORT_L"], "playback_AUX0")

    def test_eof_exits_without_saving_and_restores(self):
        self.answers = [""]  # reaches the first test, then input() hits EOF
        self.assertEqual(self.run_wizard(), 1)
        self.assertNoConfigWritten()
        self.assertRestored(profile="output:analog-stereo", automute="Enabled")

    def test_eof_before_any_prompt_exits_cleanly(self):
        self.answers = []
        self.assertEqual(self.run_wizard(), 1)
        self.assertNoConfigWritten()

    def test_keyboard_interrupt_exits_without_saving_and_restores(self):
        self.answers = ["", KeyboardInterrupt()]
        self.assertEqual(self.run_wizard(), 1)
        self.assertNoConfigWritten()
        self.assertRestored(profile="output:analog-stereo", automute="Enabled")

    def test_hardware_without_four_aux_channels_exits_1_writes_nothing_and_restores(self):
        for ports in (1, 2, 3):
            with self.subTest(ports=ports):
                self.sp.reset()
                self.aux_ports = ports
                self.set_state()
                self.answers = []
                self.assertEqual(self.run_wizard(), 1)
                self.assertNoConfigWritten()
                self.assertRestored(profile="output:analog-stereo", automute="Enabled")
                self.assertEqual(self.played(), [], "no tone may be played when the split is impossible")
                self.assertEqual(self.prompts, [])
                self.assertIn("Nothing was saved", self.out.getvalue())

    def test_missing_pro_audio_node_exits_1_and_restores(self):
        self.sp.stub(["pw-link", "-i"], stdout="some.other:playback_FL")
        self.answers = []
        self.assertEqual(self.run_wizard(), 1)
        self.assertNoConfigWritten()
        self.assertRestored(profile="output:analog-stereo", automute="Enabled")

    def test_wizard_never_sets_a_percent_volume_and_never_mutes(self):
        self.answers = self.happy_answers()
        self.run_wizard()
        amixer = self.sp.find("amixer")
        self.assertTrue(amixer)
        for argv in amixer:
            joined = " ".join(argv)
            self.assertNotIn("%", joined)
            self.assertNotIn("100", joined)
            if "set" in argv:
                self.assertIn(argv[-1], ("unmute", "Disabled"), joined)
        self.assertEqual(
            sorted(" ".join(a[4:]) for a in amixer if "set" in a),
            ["Auto-Mute Mode Disabled", "Headphone unmute", "Master unmute", "Speaker unmute"],
        )

    def test_probe_is_aimed_at_the_node_not_a_port(self):
        self.answers = self.happy_answers()
        self.run_wizard()
        for argv in self.played():
            self.assertEqual(argv[argv.index("--target") + 1], NODE)
            self.assertEqual(argv[argv.index("--channels") + 1], "4")

    def test_failed_sink_verification_exits_1_but_keeps_the_saved_config(self):
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(["Headphones", "alsa_output.x"]))
        self.answers = self.happy_answers()
        self.assertEqual(self.run_wizard(), 1)
        self.assertTrue(self.paths.CONFIG_FILE.exists())
        self.assertIn("Speakers, Both", self.out.getvalue())
        self.assertLessEqual(sum(self.clock.sleeps), 7.0, "sink polling must be bounded")

    def test_no_cards_and_no_analog_card_exit_1_without_touching_anything(self):
        self.cards = []
        self.assertEqual(self.run_wizard(), 1)
        self.cards = [GPU]
        self.assertEqual(self.run_wizard(), 1)
        self.assertNoConfigWritten()
        self.assertEqual(self.sp.find("pactl", "set-card-profile"), [])

    def test_multiple_analog_cards_ask_which_one(self):
        other = {**CARD, "id": "5", "long": "Other HDA", "card_name": "alsa_card.pci-0000_70_00.1",
                 "pci_id": "0000_70_00.1", "node_target": "alsa_output.pci-0000_70_00.1.pro-output-0"}
        self.cards = [CARD, other]
        self.answers = ["x", "9", "2"]
        self.assertIs(wizard.select_analog_card(self.cards), other)
        self.answers = [""]
        self.assertIs(wizard.select_analog_card(self.cards), CARD)


class OriginalStateTests(WizardBase):
    def write_old_config(self, **extra):
        self.write_config(ORIGINAL_PROFILE="output:analog-stereo+input:analog-stereo", ORIGINAL_AUTOMUTE="Enabled", **extra)

    def test_rerun_while_in_pro_audio_keeps_the_recorded_original_values(self):
        self.write_old_config()
        self.profile, self.automute = "pro-audio", "Disabled"
        self.set_state()
        self.answers = ["", "h", "", "s"]
        self.assertIsNone(self.run_wizard())
        cfg = self.saved()
        self.assertEqual(cfg["ORIGINAL_PROFILE"], "output:analog-stereo+input:analog-stereo")
        self.assertEqual(cfg["ORIGINAL_AUTOMUTE"], "Enabled")

    def test_pro_audio_is_never_recorded_as_the_original_profile(self):
        self.profile, self.automute = "pro-audio", "Disabled"
        self.set_state()
        self.answers = ["", "h", "", "s"]
        self.assertIsNone(self.run_wizard())
        cfg = self.saved()
        self.assertEqual(cfg["ORIGINAL_PROFILE"], "")
        self.assertEqual(cfg["ORIGINAL_AUTOMUTE"], "")
        self.assertIn("cannot be recorded", self.out.getvalue())

    def test_old_values_of_a_different_card_are_not_reused(self):
        self.write_config(PCI_ID="0000_aa_00.0", ORIGINAL_PROFILE="foreign", ORIGINAL_AUTOMUTE="foreign")
        self.profile, self.automute = "pro-audio", "Disabled"
        self.set_state()
        snap = wizard.snapshot_original_state(CARD)
        self.assertEqual((snap["ORIGINAL_PROFILE"], snap["ORIGINAL_AUTOMUTE"]), ("", ""))

    def test_rerun_outside_pro_audio_records_the_current_values(self):
        self.write_old_config()
        self.profile, self.automute = "output:analog-stereo", "Speaker Only"
        self.set_state()
        snap = wizard.snapshot_original_state(CARD)
        self.assertEqual(snap["ORIGINAL_PROFILE"], "output:analog-stereo")
        self.assertEqual(snap["ORIGINAL_AUTOMUTE"], "Speaker Only")

    def test_aborted_rerun_in_pro_audio_does_not_restore_pro_audio_as_a_profile(self):
        self.write_old_config()
        self.profile, self.automute = "pro-audio", "Disabled"
        self.set_state()
        self.answers = ["", "h", "", "h", "n"]
        self.assertEqual(self.run_wizard(), 1)
        profiles = self.sp.find("pactl", "set-card-profile")
        self.assertEqual([p[-1] for p in profiles], ["pro-audio"], "must not 'restore' pro-audio")
        # config from the earlier successful run is untouched
        self.assertEqual(self.saved()["ORIGINAL_PROFILE"], "output:analog-stereo+input:analog-stereo")


class HelperTests(WizardBase):
    def test_card_profile_and_automute_parsing(self):
        self.assertEqual(wizard.get_card_profile(CARD_NAME), "output:analog-stereo")
        self.assertEqual(wizard.get_card_profile("alsa_card.unknown"), "")
        self.assertEqual(wizard.get_automute("2"), "Enabled")
        self.sp.stub(["amixer", "-c", "2", "sget", "Auto-Mute Mode"], rc=1)
        self.assertEqual(wizard.get_automute("2"), "")

    def test_run_c_forces_the_c_locale(self):
        wizard.run_c(["pactl", "info"])
        env = self.sp.calls[-1]["kwargs"]["env"]
        self.assertEqual((env["LC_ALL"], env["LANG"]), ("C", "C"))


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
