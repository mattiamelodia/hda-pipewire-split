"""Regression tests for hda_split.config and hda_split.engine. Each test is named for the bug it pins."""

import os
import re
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401
from hermetic import (  # noqa: E402
    FakeClock,
    HermeticTestCase,
    sink_input,
    sinks_full,
    sinks_short,
)

import hda_split.config as cfg_mod  # noqa: E402
import hda_split.engine as eng_mod  # noqa: E402
from hda_split.engine import AudioEngine  # noqa: E402

CARD = "2"
TARGET = "alsa_output.pci-0000_65_00.6.pro-output-0"
SCONTROLS = (
    "Simple mixer control 'Master',0\n"
    "Simple mixer control 'Headphone',0\n"
    "Simple mixer control 'Speaker',0\n"
    "Simple mixer control 'Bass Speaker',0\n"
    "Simple mixer control 'Capture',0"
)
CONTROLS = (
    "numid=3,iface=CARD,name='Headphone Jack'\n"
    "numid=18,iface=MIXER,name='Auto-Mute Mode'\n"
    "numid=20,iface=MIXER,name='Master Playback Switch'"
)


def amixer_calls(sp):
    return [a for a in sp.argvs() if a and a[0] == "amixer"]


def is_amixer_mutation(argv):
    return len(argv) > 3 and argv[3] in ("set", "cset", "sset")


class HardwareReadyTests(HermeticTestCase):
    def setUp(self):
        super().setUp()
        self.sp.allow_audio_mutations()
        self.sp.stub(["amixer", "-c", CARD, "controls"], stdout=CONTROLS)
        self.sp.stub(["amixer", "-c", CARD, "scontrols"], stdout=SCONTROLS)

    def test_ensure_hardware_ready_never_sets_volume_or_mutes(self):
        engine = self.make_engine()
        self.sp.reset()
        engine.ensure_hardware_ready()

        mutations = [a for a in amixer_calls(self.sp) if is_amixer_mutation(a)]
        self.assertTrue(mutations, "expected some unmute / Auto-Mute calls")
        for argv in mutations:
            joined = " ".join(argv)
            self.assertNotIn("%", joined, f"percent volume set: {joined}")
            self.assertNotRegex(joined, r"\b(cset|sset)\b")
            self.assertNotRegex(joined, r"\bmute\b", f"mutes an amp: {joined}")
            self.assertNotRegex(joined, r"\d+dB|\bplayback\b", joined)
        self.assertEqual(
            sorted(" ".join(a[4:]) for a in mutations),
            sorted([
                "Auto-Mute Mode Disabled",
                "Master unmute", "Headphone unmute", "Speaker unmute", "Bass Speaker unmute",
            ]),
        )
        # profile: pro-audio only
        self.assertEqual(
            self.sp.find("pactl", "set-card-profile"),
            [["pactl", "set-card-profile", "alsa_card.pci-0000_65_00.6", "pro-audio"]],
        )

    def test_ensure_hardware_ready_falls_back_to_auto_mute_off(self):
        engine = self.make_engine()
        self.sp.stub(["amixer", "-c", CARD, "set", "Auto-Mute Mode", "Disabled"], rc=1, stderr="no")
        self.sp.reset()
        engine.ensure_hardware_ready()
        self.assertIn(["amixer", "-c", CARD, "set", "Auto-Mute Mode", "off"], amixer_calls(self.sp))

    def test_constructing_engine_and_force_all_streams_never_unloads_modules(self):
        self.sp.stub(["pactl", "list", "sink-inputs"], stdout=sink_input(
            7, "41", {"application.name": "Firefox", "application.process.binary": "firefox",
                      "application.process.id": "10"}))
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(["Headphones", "Speakers", "Both"], 40))
        engine = self.make_engine()
        engine.force_all_streams("Headphones")
        engine.force_all_streams("Both", respect_pins=True)
        engine.route_stream("7", "Speakers")
        self.assertTrue(self.sp.calls)
        for argv in self.sp.argvs():
            self.assertNotIn("unload-module", argv)
            self.assertNotIn("load-module", argv)

    def test_route_after_force_leaves_no_alsa_mute(self):
        self.sp.stub(["pactl", "list", "sink-inputs"], stdout=sink_input(
            7, "41", {"application.name": "Firefox", "application.process.binary": "firefox",
                      "application.process.id": "10"}))
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(["Headphones", "Speakers", "Both"], 40))
        engine = self.make_engine()
        engine.force_all_streams("Speakers")
        engine.route_stream("7", "Headphones")
        engine.set_default_target("Headphones")
        for argv in amixer_calls(self.sp):
            if not is_amixer_mutation(argv):
                continue
            self.assertNotIn(argv[-1], ("mute", "off", "0"), f"left an ALSA control muted: {argv}")
            self.assertNotIn("%", " ".join(argv))
        unmuted = {a[4] for a in amixer_calls(self.sp) if a[-1:] == ["unmute"]}
        self.assertEqual(unmuted, {"Master", "Headphone", "Speaker", "Bass Speaker"})


class ForceAllStreamsTests(HermeticTestCase):
    def setUp(self):
        super().setUp()
        self.sp.allow_audio_mutations()
        props = lambda app, binary, pid: {  # noqa: E731
            "application.name": app, "application.process.binary": binary, "application.process.id": pid}
        self.sp.stub(["pactl", "list", "sink-inputs"], stdout=(
            sink_input(11, "41", props("Spotify", "spotify", "1"))      # pinned, on Speakers
            + sink_input(12, "41", props("Firefox", "firefox", "2"))    # unpinned, on Speakers
            + sink_input(13, "40", props("Firefox", "firefox", "2"))    # unpinned, already on Headphones
        ))
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(["Headphones", "Speakers", "Both"], 40))
        self.engine = self.make_engine()
        self.engine.pinned_apps = {"spotify": "Speakers"}
        self.sp.reset()

    def moves(self):
        return [a[2:] for a in self.sp.find("pactl", "move-sink-input")]

    def test_force_all_streams_respect_pins_skips_pinned_and_counts_only_moved(self):
        moved = self.engine.force_all_streams("Headphones", respect_pins=True)
        self.assertEqual(moved, 1)
        self.assertEqual(self.moves(), [["12", "Headphones"]])

    def test_force_all_streams_without_pin_respect_moves_pinned_apps_too(self):
        moved = self.engine.force_all_streams("Headphones")
        self.assertEqual(moved, 2)
        self.assertEqual(sorted(self.moves()), [["11", "Headphones"], ["12", "Headphones"]])

    def test_force_all_streams_sets_default_sink_before_moving_streams(self):
        self.engine.force_all_streams("Headphones", respect_pins=True)
        argvs = self.sp.argvs()
        default_idx = argvs.index(["pactl", "set-default-sink", "Headphones"])
        move_idx = argvs.index(["pactl", "move-sink-input", "12", "Headphones"])
        self.assertLess(default_idx, move_idx)

    def test_force_all_streams_does_not_count_failed_moves(self):
        self.sp.stub(["pactl", "move-sink-input", "12"], rc=1, stderr="No such entity")
        self.assertEqual(self.engine.force_all_streams("Headphones", respect_pins=True), 0)

    def test_apply_pins_moves_only_streams_not_already_on_pinned_sink(self):
        streams = [
            {"id": "1", "app_key": "spotify", "sink_name": "Headphones"},
            {"id": "2", "app_key": "spotify", "sink_name": "Speakers"},
            {"id": "3", "app_key": "other", "sink_name": "Headphones"},
        ]
        self.engine.apply_pins(streams)
        self.assertEqual(self.moves(), [["1", "Speakers"]])
        self.assertEqual(streams[0]["sink_name"], "Speakers")


class EnsureSinksTests(HermeticTestCase):
    def setUp(self):
        super().setUp()
        self.sp.allow_audio_mutations()
        self.clock = FakeClock()
        p = mock.patch.object(eng_mod, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def test_ensure_sinks_exist_returns_false_without_restarting_when_pactl_info_is_empty(self):
        engine = self.make_engine()
        self.paths.PW_DROPIN_FILE.unlink()  # even a missing/stale drop-in must not trigger a restart
        self.sp.stub(["pactl", "info"], stdout="")
        self.sp.reset()
        self.assertFalse(engine.ensure_sinks_exist())
        self.assertFalse(engine.ensure_sinks_exist(force_reload=True))
        self.assertEqual(self.sp.find("systemctl"), [])
        self.assertEqual(self.sp.find("pactl", "set-card-profile"), [])
        self.assertEqual(self.clock.sleeps, [])

    def test_ensure_sinks_exist_polls_for_sinks_after_restart(self):
        engine = self.make_engine()
        self.sp.stub(["pactl", "info"], stdout="Server String: /run/user/1000/pulse/native")
        polls = []

        def sinks(argv):
            polls.append(argv)
            # sinks only show up on the 4th listing: one before the restart, then 2 empty polls, then ready
            return sinks_short(["Headphones", "Speakers", "Both"]) if len(polls) >= 4 else ""

        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks)
        self.sp.reset()

        self.assertTrue(engine.ensure_sinks_exist())

        restarts = self.sp.find("systemctl", "--user", "restart", "pipewire")
        self.assertEqual(len(restarts), 1)
        argvs = self.sp.argvs()
        restart_idx = argvs.index(restarts[0])
        listings = [i for i, a in enumerate(argvs) if a[:4] == ["pactl", "list", "sinks", "short"]]
        self.assertEqual(listings[0] < restart_idx, True, "first check happens before the restart")
        after = [i for i in listings if i > restart_idx]
        self.assertGreaterEqual(len(after), 3, "must keep polling after the restart until sinks appear")
        self.assertGreaterEqual(len(self.clock.sleeps), 2)
        # hardware is prepared only once the restart is done
        profile_idx = argvs.index(["pactl", "set-card-profile", "alsa_card.pci-0000_65_00.6", "pro-audio"])
        self.assertGreater(profile_idx, restart_idx)

    def test_ensure_sinks_exist_reports_failure_when_sinks_never_appear(self):
        engine = self.make_engine()
        self.sp.stub(["pactl", "info"], stdout="Server String: x")
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(["alsa_output.x"]))
        self.assertFalse(engine.ensure_sinks_exist())
        self.assertLess(sum(self.clock.sleeps), 6.0, "polling must be bounded (about 5 s)")

    def test_internal_sinks_present_compares_exact_names(self):
        engine = self.make_engine()
        decoys = ["Headphones2", "alsa_output.Speakers", "Both-old", "xHeadphones", "Speakers.monitor"]
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(decoys))
        self.assertFalse(engine._internal_sinks_present())
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(decoys + ["Headphones", "Speakers"]))
        self.assertFalse(engine._internal_sinks_present(), "Both is still missing")
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(decoys + ["Headphones", "Speakers", "Both"]))
        self.assertTrue(engine._internal_sinks_present())

    def test_ensure_sinks_exist_with_lookalike_names_does_not_report_success(self):
        engine = self.make_engine()
        engine._last_restart = self.clock.monotonic()  # inside the cooldown: no restart is attempted
        self.sp.stub(["pactl", "info"], stdout="Server String: x")
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(["Headphones2", "Speakers2", "Both2"]))
        self.sp.reset()
        self.assertFalse(engine.ensure_sinks_exist())
        self.assertEqual(self.sp.find("systemctl"), [])

    def test_ensure_sinks_exist_does_not_restart_twice_within_cooldown(self):
        engine = self.make_engine()
        self.sp.stub(["pactl", "info"], stdout="Server String: x")
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout="")
        self.sp.reset()
        engine.ensure_sinks_exist()
        engine.ensure_sinks_exist()
        self.assertEqual(len(self.sp.find("systemctl", "--user", "restart")), 1)
        self.clock.advance(engine.RESTART_COOLDOWN + 1)
        engine.ensure_sinks_exist()
        self.assertEqual(len(self.sp.find("systemctl", "--user", "restart")), 2)

    def test_ensure_sinks_exist_rewrites_stale_dropin_and_restarts(self):
        engine = self.make_engine()
        self.paths.PW_DROPIN_FILE.write_text("# stale")
        self.sp.stub(["pactl", "info"], stdout="Server String: x")
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(["Headphones", "Speakers", "Both"]))
        self.sp.reset()
        self.assertTrue(engine.ensure_sinks_exist())
        self.assertEqual(self.paths.PW_DROPIN_FILE.read_text(), cfg_mod.render_pipewire_dropin(engine.cfg))
        self.assertEqual(len(self.sp.find("systemctl", "--user", "restart", "pipewire")), 1)

    def test_ensure_sinks_exist_healthy_system_restarts_nothing(self):
        engine = self.make_engine()
        self.sp.stub(["pactl", "info"], stdout="Server String: x")
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(["Headphones", "Speakers", "Both"]))
        self.sp.reset()
        self.assertTrue(engine.ensure_sinks_exist())
        self.assertEqual(self.sp.find("systemctl"), [])


class EnforceHardwareLinksTests(HermeticTestCase):
    def setUp(self):
        super().setUp()
        self.sp.allow_audio_mutations()
        self.sp.stub(["pactl", "list", "sinks"], stdout=sinks_full([
            (40, "Headphones", "Headphones (Jack)", "pw-loopback-1", 100, False),
            (41, "Speakers", "Internal Speakers", "pw-loopback-2", 100, False),
            (42, "Both", "Both", "", 100, False),
        ]))

    def enforce(self, outputs, links):
        self.sp.stub(["pw-link", "-o"], stdout="\n".join(outputs))
        self.sp.stub(["pw-link", "-l"], stdout="\n".join(links))
        engine = self.make_engine()
        self.sp.reset()
        engine.enforce_hardware_links()
        return [a for a in self.sp.argvs() if a[0] == "pw-link" and a[1] not in ("-o", "-l", "-i")]

    def test_enforce_hardware_links_touches_only_our_loopback_nodes(self):
        t = TARGET
        outputs = [
            "output.pw-loopback-1:output_AUX0", "output.pw-loopback-1:output_AUX1",
            "Spotify:output_AUX0",                      # unrelated node that also has an AUX0 port
            "mpv:output_AUX1",
            "playback.Speakers:output_FL", "playback.Speakers:output_FR",
            "other.Speakers:output_FL",                 # name merely contains 'Speakers'
            "My Speakers:output_FR",
            "playback.Speakers.extra:output_FL",
        ]
        links = [
            "output.pw-loopback-1:output_AUX0", f"  |-> {t}:playback_AUX2",   # stray: Headphones -> speaker pair
            "output.pw-loopback-1:output_AUX1", f"  |-> {t}:playback_AUX1",   # correct
            "playback.Speakers:output_FL", f"  |-> {t}:playback_AUX2",        # correct
            "Spotify:output_AUX0", "  |-> other:in",                          # must stay untouched
            "other.Speakers:output_FL", f"  |-> {t}:playback_AUX0",           # must stay untouched
        ]
        ran = self.enforce(outputs, links)
        self.assertEqual(ran, [
            ["pw-link", "-d", "output.pw-loopback-1:output_AUX0", f"{t}:playback_AUX2"],
            ["pw-link", "output.pw-loopback-1:output_AUX0", f"{t}:playback_AUX0"],
            ["pw-link", "playback.Speakers:output_FR", f"{t}:playback_AUX3"],
        ])
        flat = " ".join(" ".join(a) for a in ran)
        for foreign in ("Spotify", "mpv", "other.Speakers", "My Speakers", "Speakers.extra"):
            self.assertNotIn(foreign, flat)

    def test_enforce_hardware_links_handles_output_loopback_named_by_node_group(self):
        t = TARGET
        ran = self.enforce(
            ["output.pw-loopback-2:output_AUX2", "output.pw-loopback-2:output_AUX3"],
            [],
        )
        self.assertEqual(ran, [
            ["pw-link", "output.pw-loopback-2:output_AUX2", f"{t}:playback_AUX2"],
            ["pw-link", "output.pw-loopback-2:output_AUX3", f"{t}:playback_AUX3"],
        ])

    def test_enforce_hardware_links_uses_configured_swapped_ports(self):
        self.write_config(HP_PORT_L="playback_AUX2", HP_PORT_R="playback_AUX3",
                          SPK_PORT_L="playback_AUX0", SPK_PORT_R="playback_AUX1")
        self.sp.stub(["pw-link", "-o"], stdout="output.pw-loopback-1:output_FL\noutput.pw-loopback-1:output_FR")
        self.sp.stub(["pw-link", "-l"], stdout="")
        engine = eng_mod.AudioEngine()
        self.sp.reset()
        engine.enforce_hardware_links()
        ran = [a for a in self.sp.argvs() if a[0] == "pw-link" and a[1] not in ("-o", "-l")]
        self.assertEqual(ran, [
            ["pw-link", "output.pw-loopback-1:output_FL", f"{TARGET}:playback_AUX2"],
            ["pw-link", "output.pw-loopback-1:output_FR", f"{TARGET}:playback_AUX3"],
        ])

    def test_enforce_hardware_links_is_a_no_op_when_everything_is_linked(self):
        t = TARGET
        ran = self.enforce(
            ["output.pw-loopback-1:output_AUX0", "output.pw-loopback-1:output_AUX1"],
            ["output.pw-loopback-1:output_AUX0", f"  |-> {t}:playback_AUX0",
             "output.pw-loopback-1:output_AUX1", f"  |-> {t}:playback_AUX1"],
        )
        self.assertEqual(ran, [])

    def test_enforce_hardware_links_does_nothing_without_pipewire_ports(self):
        self.assertEqual(self.enforce([], []), [])


class GetStreamsTests(HermeticTestCase):
    def streams_for(self, blocks, mpris=None):
        self.sp.stub(["pactl", "list", "sink-inputs"], stdout="".join(blocks))
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout="40\tHeadphones\tPipeWire\n41\tSpeakers\tPipeWire")
        engine = self.make_engine()
        with mock.patch.object(eng_mod, "get_mpris_tab_titles", return_value=mpris or {}):
            return engine.get_streams()

    def test_get_streams_keeps_stream_titled_coffee_filter_tutorial(self):
        streams = self.streams_for([sink_input(3, "40", {
            "application.name": "Firefox", "application.process.binary": "firefox",
            "media.name": "Coffee filter tutorial", "node.name": "Firefox", "application.process.id": "10"})])
        self.assertEqual([s["id"] for s in streams], ["3"])
        self.assertEqual(streams[0]["media_name"], "Coffee filter tutorial")

    def test_get_streams_filters_real_loopback_and_combine_plumbing(self):
        blocks = [
            sink_input(1, "41", {
                "application.name": "pw-loopback-1", "application.process.id": "99",
                "node.name": "output.pw-loopback-1", "node.group": "pw-loopback-1",
                "media.name": "pw-loopback-1 output"}),
            sink_input(2, "41", {
                "application.name": "Simultaneous Output (Both)", "application.process.id": "99",
                "node.name": "output.Both_Speakers", "node.group": "combine-sink-x",
                "media.name": "Simultaneous Output (Both) output"}),
            sink_input(3, "41", {  # only the node.group gives it away
                "application.name": "pw-loopback-9", "application.process.id": "99",
                "node.name": "renamed", "node.group": "pw-loopback-9"}),
            sink_input(4, "41", {  # media.role filter
                "application.name": "something", "media.role": "filter", "application.process.id": "98"}),
            sink_input(5, "40", {"application.name": "Firefox", "application.process.binary": "firefox",
                                 "application.process.id": "10", "node.name": "Firefox"}),
        ]
        streams = self.streams_for(blocks)
        self.assertEqual([s["id"] for s in streams], ["5"])

    def test_get_streams_filters_output_named_loopback_nodes_even_without_a_group(self):
        streams = self.streams_for([sink_input(1, "41", {
            "application.name": "Simultaneous Output (Both)", "application.process.id": "99",
            "node.name": "output.Both_Headphones"})])
        self.assertEqual(streams, [])

    def test_get_streams_drops_streams_without_any_identity_and_resolves_sink_names(self):
        blocks = [
            sink_input(1, "41", {"media.name": "anonymous"}),
            sink_input(2, "41", {"application.name": "mpv", "application.process.binary": "mpv"}, volume=35, muted=True),
        ]
        streams = self.streams_for(blocks)
        self.assertEqual([s["id"] for s in streams], ["2"])
        s = streams[0]
        self.assertEqual((s["sink_name"], s["volume"], s["muted"], s["app_key"]), ("Speakers", 35, True, "mpv"))

    def test_get_streams_returns_empty_list_when_pactl_is_silent(self):
        self.assertEqual(self.streams_for([]), [])


class MprisMatchingTests(HermeticTestCase):
    @staticmethod
    def stream(app, binary):
        return {"app": app, "binary": binary, "media_title": ""}

    def apply(self, streams, titles):
        AudioEngine._apply_mpris_titles(streams, titles)
        return [s["media_title"] for s in streams]

    def test_firefox_title_is_not_given_to_discord_or_spotify(self):
        streams = [self.stream("Discord", "electron"), self.stream("Spotify", "spotify"),
                   self.stream("Firefox", "firefox")]
        titles = self.apply(streams, {"firefox.instance_9": "Firefox Tab"})
        self.assertEqual(titles, ["", "", "Firefox Tab"])

    def test_player_title_goes_only_to_its_own_app(self):
        streams = [self.stream("Discord", "electron"), self.stream("Spotify", "spotify")]
        self.assertEqual(self.apply(streams, {"spotify": "Song"}), ["", "Song"])

    def test_two_firefox_streams_get_no_title(self):
        streams = [self.stream("Firefox", "firefox"), self.stream("Firefox", "firefox")]
        self.assertEqual(self.apply(streams, {"firefox.instance_1": "Tab A"}), ["", ""])

    def test_two_firefox_players_and_one_stream_get_no_title(self):
        streams = [self.stream("Firefox", "firefox")]
        self.assertEqual(self.apply(streams, {"firefox.instance_1": "A", "firefox.instance_2": "B"}), [""])

    def test_single_firefox_stream_gets_its_title(self):
        streams = [self.stream("Firefox", "firefox")]
        self.assertEqual(self.apply(streams, {"firefox.instance_9": "Only Tab"}), ["Only Tab"])

    def test_chromium_aliases_apply_only_to_chromium_family_streams(self):
        for app, binary in (("Chromium", "chromium"), ("Brave", "brave"), ("Google Chrome", "chrome")):
            with self.subTest(app=app):
                self.assertEqual(self.apply([self.stream(app, binary)], {"chrome.instance_1": "Tab"}), ["Tab"])
        for app, binary in (("Firefox", "firefox"), ("Discord", "electron"), ("Spotify", "spotify")):
            with self.subTest(app=app):
                self.assertEqual(self.apply([self.stream(app, binary)], {"chrome.instance_1": "Tab"}), [""])

    def test_streams_without_identity_never_match(self):
        self.assertEqual(self.apply([self.stream("", "")], {"firefox": "A", "chrome": "B"}), [""])

    def test_mpris_title_reaches_get_streams_end_to_end(self):
        self.sp.stub(["pactl", "list", "sink-inputs"], stdout=(
            sink_input(1, "40", {"application.name": "Firefox", "application.process.binary": "firefox",
                                 "application.process.id": "1"})
            + sink_input(2, "40", {"application.name": "Discord", "application.process.binary": "electron",
                                   "application.process.id": "2"})))
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout="40\tHeadphones\tPipeWire")
        engine = self.make_engine()
        with mock.patch.object(eng_mod, "get_mpris_tab_titles", return_value={"firefox.instance_3": "FF Tab"}):
            by_id = {s["id"]: s for s in engine.get_streams()}
        self.assertEqual(by_id["1"]["media_title"], "FF Tab")
        self.assertEqual(by_id["2"]["media_title"], "")

    def test_dbus_fallback_reads_titles_when_playerctl_is_missing(self):
        import hda_split.mpris as mpris_mod

        self.missing_cmds.add("playerctl")
        self.sp.stub(["dbus-send", "--session", "--dest=org.freedesktop.DBus"], stdout=(
            'method return time=1.0 sender=:1.2 -> destination=:1.99 serial=3 reply_serial=2\n'
            '   array [\n      string "org.freedesktop.DBus"\n'
            '      string "org.mpris.MediaPlayer2.firefox.instance_123"\n   ]'))
        self.sp.stub(["dbus-send", "--session", "--reply-timeout=500"], stdout=(
            '   variant       array [\n      dict entry(\n         string "xesam:title"\n'
            '         variant             string "Tab: with colon"\n      )\n   ]'))
        titles = mpris_mod.get_mpris_tab_titles()
        self.assertEqual(titles, {"firefox.instance_123": "Tab: with colon"})
        self.assertEqual(self.sp.find("playerctl"), [])

    def test_playerctl_titles_keep_colons_inside_the_title(self):
        import hda_split.mpris as mpris_mod

        self.sp.stub(["playerctl"], stdout="firefox: Part 1: Intro\nvlc: ")
        self.assertEqual(mpris_mod.get_mpris_tab_titles(), {"firefox": "Part 1: Intro"})


class AppKeyTests(HermeticTestCase):
    def test_get_app_key_uses_application_name_for_electron_apps(self):
        self.assertEqual(AudioEngine.get_app_key({"app": "Discord", "binary": "electron"}), "discord")
        self.assertEqual(AudioEngine.get_app_key({"app": "Slack", "binary": "electron33"}), "slack")

    def test_get_app_key_uses_binary_for_normal_apps(self):
        self.assertEqual(AudioEngine.get_app_key({"app": "Firefox", "binary": "firefox"}), "firefox")
        self.assertEqual(AudioEngine.get_app_key({"app": "Firefox Developer", "binary": "firefox-developer-edition"}),
                         "firefox-developer-edition")

    def test_get_app_key_other_runtimes_and_missing_parts(self):
        key = AudioEngine.get_app_key
        self.assertEqual(key({"app": "Foo", "binary": "python3.12"}), "foo")
        self.assertEqual(key({"app": "Foo", "binary": "wine64-preloader"}), "foo")
        self.assertEqual(key({"app": "", "binary": "electron"}), "electron")
        self.assertEqual(key({"app": "X", "binary": ""}), "x")
        self.assertEqual(key({}), "")


class VolumeTests(HermeticTestCase):
    def test_volume_is_clamped_to_0_100(self):
        self.sp.allow_audio_mutations()
        engine = self.make_engine()
        self.sp.reset()
        engine.set_sink_volume("Headphones", 250)
        engine.set_sink_volume("Headphones", -5)
        engine.set_sink_volume("Headphones", 42)
        engine.set_stream_volume("12", 101)
        engine.set_stream_volume("12", -1)
        self.assertEqual([a[-1] for a in self.sp.argvs()], ["100%", "0%", "42%", "100%", "0%"])

    def test_mute_commands_and_toggle(self):
        self.sp.allow_audio_mutations()
        self.sp.stub(["pactl", "list", "sinks"], stdout=sinks_full([
            (40, "Headphones", "HP", "pw-loopback-1", 100, True), (41, "Speakers", "S", "pw-loopback-2", 50, False)]))
        engine = self.make_engine()
        self.sp.reset()
        self.assertFalse(engine.toggle_all_sinks_mute())  # Headphones were muted -> unmute both
        self.assertEqual(self.sp.find("pactl", "set-sink-mute"),
                         [["pactl", "set-sink-mute", "Headphones", "0"], ["pactl", "set-sink-mute", "Speakers", "0"]])
        engine.set_stream_mute("9", True)
        self.assertEqual(self.sp.argvs()[-1], ["pactl", "set-sink-input-mute", "9", "1"])

    def test_toggle_all_sinks_mute_does_not_touch_alsa(self):
        self.sp.allow_audio_mutations()
        engine = self.make_engine()
        self.sp.reset()
        engine.toggle_all_sinks_mute()
        self.assertEqual(amixer_calls(self.sp), [])
        self.assertEqual(self.sp.find("pactl", "set-card-profile"), [])


class SinkInfoTests(HermeticTestCase):
    def test_sink_info_parses_group_volume_and_mute(self):
        self.sp.stub(["pactl", "list", "sinks"], stdout=sinks_full([
            (40, "Headphones", "Headphones (Jack)", "pw-loopback-1", 130, True),
            (41, "bluez_output.AA", "Sony", "", 80, False),
            (42, f"{TARGET}", "Pro", "", 100, False)]))
        engine = self.make_engine()
        info = engine.get_sink_info()
        self.assertEqual(info["Headphones"]["group"], "pw-loopback-1")
        self.assertEqual(info["Headphones"]["volume"], 130)
        self.assertTrue(info["Headphones"]["muted"])
        self.assertEqual([s["name"] for s in engine.get_external_sinks()], ["bluez_output.AA"])


class ConfigTests(HermeticTestCase):
    def test_run_cmd_forces_c_utf8_locale_and_a_timeout(self):
        self.sp.stub(["echo-tool"], stdout=" hi \n")
        with mock.patch.dict(os.environ, {"LC_ALL": "it_IT.UTF-8", "LANG": "it_IT.UTF-8"}):
            self.assertEqual(cfg_mod.run_cmd(["echo-tool", "x"]), "hi")
        kwargs = self.sp.calls[-1]["kwargs"]
        self.assertEqual(kwargs["env"]["LC_ALL"], "C.UTF-8")
        self.assertEqual(kwargs["timeout"], cfg_mod.CMD_TIMEOUT)
        self.assertGreater(kwargs["timeout"], 0)
        self.assertEqual(kwargs["errors"], "replace")

    def test_run_silent_passes_locale_and_timeout_too(self):
        self.sp.allow(["pactl", "set-default-sink"])
        self.assertTrue(AudioEngine._run_silent(["pactl", "set-default-sink", "Both"]))
        kwargs = self.sp.calls[-1]["kwargs"]
        self.assertEqual(kwargs["env"]["LC_ALL"], "C.UTF-8")
        self.assertEqual(kwargs["timeout"], cfg_mod.CMD_TIMEOUT)

    def test_run_cmd_returns_empty_string_on_timeout_failure_and_missing_binary(self):
        import subprocess

        self.sp.stub(["slow"], exc=subprocess.TimeoutExpired("slow", 5))
        self.sp.stub(["bad"], rc=3, stderr="boom")
        self.sp.stub(["gone"], exc=FileNotFoundError("gone"))
        for cmd in ("slow", "bad", "gone"):
            self.assertEqual(cfg_mod.run_cmd([cmd]), "")

    def test_run_cmd_failures_are_logged_once_per_distinct_reason(self):
        self.sp.stub(["bad"], rc=3, stderr="boom")
        with self.assertLogs("hda_split", "WARNING") as cm:
            for _ in range(5):
                cfg_mod.run_cmd(["bad"])
        self.assertEqual(len(cm.output), 1)
        self.assertIn("boom", cm.output[0])

    def test_run_silent_logs_failures_and_returns_false(self):
        self.sp.stub(["pactl", "move-sink-input"], rc=1, stderr="No such entity")
        with self.assertLogs("hda_split", "WARNING") as cm:
            self.assertFalse(AudioEngine._run_silent(["pactl", "move-sink-input", "1", "x"]))
        self.assertIn("No such entity", cm.output[0])

    def test_atomic_write_leaves_no_temp_file(self):
        target = self.base / "sub" / "file.json"
        cfg_mod.atomic_write_text(target, "one")
        cfg_mod.atomic_write_text(target, "two")
        self.assertEqual(target.read_text(), "two")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["file.json"])
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_atomic_write_failure_cleans_up_and_keeps_old_content(self):
        target = self.base / "file.txt"
        cfg_mod.atomic_write_text(target, "old")
        with mock.patch.object(cfg_mod.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cfg_mod.atomic_write_text(target, "new")
        self.assertEqual(target.read_text(), "old")
        self.assertEqual([p.name for p in self.base.iterdir() if p.name.startswith(".file")], [])
        self.assertEqual(sorted(p.name for p in self.base.iterdir() if p.is_file()), ["file.txt"])

    def test_save_config_and_pins_leave_only_final_files(self):
        self.write_config()
        cfg_mod.save_pinned_apps({"a": "Both"})
        self.assertEqual(sorted(p.name for p in self.config_dir.iterdir()), ["config.env", "pinned_apps.json"])
        self.assertEqual([p.name for p in self.pw_conf_dir.iterdir()], ["99-hda-split.conf"])

    def test_resolve_card_index_follows_pci_id_when_the_index_changed(self):
        cards = [{"id": "3", "pci_id": "0000_65_00.6"}, {"id": "1", "pci_id": "0000_00_1f.3"}]
        with mock.patch.object(cfg_mod, "discover_hda_cards", return_value=cards):
            self.assertEqual(cfg_mod.resolve_card_index({"PCI_ID": "0000_65_00.6", "ALSA_CARD_ID": "2"}), "3")
            self.assertEqual(
                cfg_mod.resolve_card_index({"CARD_NAME": "alsa_card.pci-0000_65_00.6-extra", "ALSA_CARD_ID": "2"}), "3")
            self.assertEqual(cfg_mod.resolve_card_index({"PCI_ID": "0000_99_00.0", "ALSA_CARD_ID": "2"}), "2")
            self.assertEqual(cfg_mod.resolve_card_index({"ALSA_CARD_ID": "5"}), "5")
            self.assertEqual(cfg_mod.resolve_card_index({}), "0")

    def test_engine_card_id_uses_pci_lookup(self):
        cards = [{"id": "3", "pci_id": "0000_65_00.6"}]
        with mock.patch.object(cfg_mod, "discover_hda_cards", return_value=cards):
            engine = self.make_engine(ALSA_CARD_ID="2")
        self.assertEqual(engine.card_id, "3")
        self.assertIn(["amixer", "-c", "3", "controls"], self.sp.argvs())

    def test_resolve_pipewire_names_handles_sof_suffix_and_prefix_collisions(self):
        sof = "alsa_card.pci-0000_00_1f.3-platform-skl_hda_dsp_generic"
        self.assertEqual(cfg_mod.resolve_pipewire_names("0000_00_1f.3", [sof]),
                         (sof, "alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.pro-output-0"))
        self.assertEqual(cfg_mod.resolve_pipewire_names("0000_00_1f.3", ["alsa_card.pci-0000_00_1f.30"]),
                         ("alsa_card.pci-0000_00_1f.3", "alsa_output.pci-0000_00_1f.3.pro-output-0"))

    def test_port_channels_matches_the_exact_node_only(self):
        self.sp.stub(["pw-link", "-i"], stdout=(
            "a.b:playback_AUX0\na.b2:playback_AUX1\na.b:playback_AUX1\nx:a.b:playback_AUX2"))
        self.assertEqual(cfg_mod.port_channels("a.b"), ["playback_AUX0", "playback_AUX1"])

    def test_load_config_parses_quotes_and_comments_and_fills_defaults(self):
        self.config_dir.mkdir(parents=True)
        self.paths.CONFIG_FILE.write_text(
            '# comment\nALSA_CARD_ID="4"\nNODE_TARGET=\'alsa_output.x.pro-output-0\'\nPCI_ID = 0000_01_00.1\n')
        cfg = cfg_mod.load_config()
        self.assertEqual(cfg["ALSA_CARD_ID"], "4")
        self.assertEqual(cfg["NODE_TARGET"], "alsa_output.x.pro-output-0")
        self.assertEqual(cfg["PCI_ID"], "0000_01_00.1")
        self.assertEqual(cfg["HP_PORT_L"], "playback_AUX0")

    def test_load_config_uses_discovery_when_the_file_is_missing(self):
        card = {"id": "7", "pci_id": "0000_aa_00.1", "card_name": "alsa_card.pci-0000_aa_00.1",
                "node_target": "alsa_output.pci-0000_aa_00.1.pro-output-0"}
        with mock.patch.object(cfg_mod, "discover_hda_cards", return_value=[card]):
            cfg = cfg_mod.load_config()
        self.assertEqual((cfg["ALSA_CARD_ID"], cfg["PCI_ID"]), ("7", "0000_aa_00.1"))
        self.assertEqual(cfg["NODE_TARGET"], card["node_target"])

    def test_dropin_uses_configured_ports_and_target(self):
        text = cfg_mod.render_pipewire_dropin({**hermetic.DEFAULT_CFG, "HP_PORT_L": "playback_AUX2",
                                               "HP_PORT_R": "playback_AUX3", "SPK_PORT_L": "playback_AUX0",
                                               "SPK_PORT_R": "playback_AUX1"})
        hp = text.split('node.name = "playback.Headphones"')[1].split("}")[0]
        spk = text.split('node.name = "playback.Speakers"')[1].split("}")[0]
        self.assertIn("audio.position = [ AUX2 AUX3 ]", hp)
        self.assertIn("audio.position = [ AUX0 AUX1 ]", spk)
        self.assertEqual(len(re.findall(re.escape(f'target.object = "{TARGET}"'), text)), 2)

    def test_notification_goes_through_the_recorder_with_a_timeout(self):
        cfg_mod.send_notification("t", "m", urgency="low")
        call = self.sp.calls[-1]
        self.assertEqual(call["argv"][0], "notify-send")
        self.assertEqual(call["argv"][-2:], ["t", "m"])
        self.assertEqual(call["kwargs"]["timeout"], cfg_mod.CMD_TIMEOUT)

    def test_temp_paths_are_isolated_from_the_real_home(self):
        for p in (cfg_mod.CONFIG_DIR, cfg_mod.CONFIG_FILE, cfg_mod.PINS_FILE, cfg_mod.PW_CONF_DIR,
                  cfg_mod.PW_DROPIN_FILE, eng_mod.PW_DROPIN_FILE):
            self.assertTrue(str(p).startswith(str(self.base)), p)
        self.assertTrue(os.environ["HOME"].startswith(tempfile.gettempdir()))


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
