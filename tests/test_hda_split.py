"""Core behaviour tests (config, pins, engine parsing, MPRIS, jack monitor).

Run with:  python3 -m unittest discover -s tests -v
Every test is hermetic: see tests/hermetic.py (no real pactl/amixer/pw-link/systemctl is ever executed).
"""

import os
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (must come before any Qt / hda_split import)
from hermetic import FakeClock, FakeProc, HermeticTestCase  # noqa: E402

import hda_split.config as cfg_mod  # noqa: E402
import hda_split.engine as eng_mod  # noqa: E402
import hda_split.monitor as mon_mod  # noqa: E402
import hda_split.mpris as mpris_mod  # noqa: E402
from hda_split.monitor import JackMonitorThread  # noqa: E402

MOCK_PACTL_SINKS = """
Sink #42
	State: RUNNING
	Name: Speakers
	Description: Internal Speakers
	Mute: no
	Volume: front-left: 49152 /  75% / -7.50 dB,   front-right: 49152 /  75% / -7.50 dB
Sink #81
	State: RUNNING
	Name: Headphones
	Description: Headphones (Jack)
	Mute: yes
	Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
Sink #90
	State: IDLE
	Name: Both
	Description: Simultaneous Output (Both)
	Mute: no
	Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
Sink #105
	State: SUSPENDED
	Name: bluez_output.AA_BB_CC.1
	Description: Sony WH-1000XM5
	Mute: no
	Volume: front-left: 52428 /  80% / -5.80 dB
"""

MOCK_PACTL_SINKS_SHORT = """
42\tSpeakers\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING
81\tHeadphones\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING
90\tBoth\tPipeWire\tfloat32le 2ch 48000Hz\tIDLE
105\tbluez_output.AA_BB_CC.1\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED
"""

MOCK_PACTL_SINK_INPUTS = """
Sink Input #112
	Sink: 81
	Mute: no
	Volume: front-left: 65536 / 100% / 0.00 dB
	Properties:
		application.name = "Firefox"
		application.process.binary = "firefox"
		media.name = "YouTube - Music"
		application.process.id = "4512"
Sink Input #115
	Sink: 42
	Mute: no
	Volume: front-left: 32768 /  50% / -18.06 dB
	Properties:
		application.name = "playback.Headphones"
		media.role = "filter"
"""


class TestHdaSplitSuite(HermeticTestCase):
    def test_1_write_pipewire_dropin_includes_isolated_both_sink(self):
        """Verifies that the native drop-in creates Headphones, Speakers, and the isolated Both sink."""
        sample_cfg = {
            "ALSA_CARD_ID": "2",
            "PCI_ID": "0000_65_00.6",
            "CARD_NAME": "alsa_card.pci-0000_65_00.6",
            "NODE_TARGET": "alsa_output.pci-0000_65_00.6.pro-output-0",
            "HP_PORT_L": "playback_AUX0",
            "HP_PORT_R": "playback_AUX1",
            "SPK_PORT_L": "playback_AUX2",
            "SPK_PORT_R": "playback_AUX3",
        }
        cfg_mod.save_config(sample_cfg)
        self.assertTrue(cfg_mod.PW_DROPIN_FILE.exists())
        self.assertEqual(cfg_mod.PW_DROPIN_FILE, self.paths.PW_DROPIN_FILE)

        content = cfg_mod.PW_DROPIN_FILE.read_text()
        self.assertIn("libpipewire-module-loopback", content)
        self.assertIn("libpipewire-module-combine-stream", content)
        self.assertIn('node.name = "Both"', content)
        self.assertIn("audio.position = [ AUX0 AUX1 ]", content)
        self.assertIn("audio.position = [ AUX2 AUX3 ]", content)

    def test_2_persistent_app_pins_save_and_load(self):
        """Verifies JSON saving and loading of per-application pins."""
        cfg_mod.save_pinned_apps({"firefox": "Speakers", "discord": "Headphones"})
        loaded = cfg_mod.load_pinned_apps()
        self.assertEqual(loaded.get("firefox"), "Speakers")
        self.assertEqual(loaded.get("discord"), "Headphones")

    def test_3_isolated_route_stream_no_global_pw_link(self):
        """Routing to 'Both' moves only the input to the Both sink, without any pw-link call."""
        engine = self.make_engine()
        self.sp.allow(["pactl", "move-sink-input"])
        self.sp.reset()

        engine.route_stream("112", "Both")
        self.assertEqual(self.sp.argvs(), [["pactl", "move-sink-input", "112", "Both"]])
        self.assertEqual(self.sp.find("pw-link"), [])

    def test_4_stream_parsing_and_loopback_filtering(self):
        """Internal loopback streams are filtered and MPRIS titles are associated."""
        self.sp.stub(["pactl", "list", "sink-inputs"], stdout=MOCK_PACTL_SINK_INPUTS)
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=MOCK_PACTL_SINKS_SHORT)
        engine = self.make_engine()
        with mock.patch.object(
            eng_mod, "get_mpris_tab_titles", return_value={"firefox": "Lecture 04 - ML Security"}
        ):
            streams = engine.get_streams()

        self.assertEqual(len(streams), 1)
        s = streams[0]
        self.assertEqual(s["id"], "112")
        self.assertEqual(s["app_key"], "firefox")
        self.assertEqual(s["media_title"], "Lecture 04 - ML Security")
        self.assertEqual(s["sink_name"], "Headphones")

    def test_5_sink_master_volumes_and_external_bluetooth_detection(self):
        """Verifies Master volume reading and external Bluetooth headphone detection."""
        self.sp.stub(["pactl", "list", "sinks"], stdout=MOCK_PACTL_SINKS)
        engine = self.make_engine()

        info = engine.get_sink_info()
        self.assertEqual(info["Speakers"]["volume"], 75)
        self.assertFalse(info["Speakers"]["muted"])
        self.assertEqual(info["Headphones"]["volume"], 100)
        self.assertTrue(info["Headphones"]["muted"])

        ext = engine.get_external_sinks()
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0]["description"], "Sony WH-1000XM5")

    def test_6_dynamic_jack_detection_numid(self):
        """Verifies dynamic parsing of Jack numid and reading of on/off state."""
        self.sp.stub(
            ["amixer", "-c", "2", "controls"],
            stdout="numid=14,iface=CARD,name='Headphone Jack'\nnumid=15,iface=MIXER,name='Master Playback Volume'",
        )
        self.sp.stub(
            ["amixer", "-c", "2", "cget", "numid=14"],
            stdout="numid=14,iface=CARD,name='Headphone Jack'\n  ; type=BOOLEAN,access=r-------,values=1\n  : values=on",
        )
        engine = self.make_engine()
        self.assertEqual(engine.jack_numid, "14")
        self.assertTrue(engine.is_headphone_plugged())

    def test_7_monitor_thread_emits_signal_and_recovers_on_eof(self):
        """The jack thread reports a plug event, and after `alsactl monitor` ends (suspend/resume) it
        signals hardware_resumed and starts a fresh `alsactl monitor hw:<card>` instead of dying."""
        clock = FakeClock()
        engine = SimpleNamespace(card_id="2")
        plugged = iter([False, True])  # at thread start, then when the first monitor line arrives
        engine.is_headphone_plugged = lambda: next(plugged, True)

        thread = JackMonitorThread(engine)
        thread.msleep = lambda ms: clock.sleep(ms / 1000)
        events, resumed, launches = [], [], []
        thread.jack_state_changed.connect(events.append)
        thread.hardware_resumed.connect(lambda: resumed.append(True))

        def first_run():
            yield "node hw:2, #14 (0,0,0,Headphone Jack,0) VALUE\n"
            clock.advance(15.0)  # ran longer than STABLE_SECS, then EOF as on suspend

        def second_run():
            thread._running = False  # recovery happened; stop the loop
            return iter(())

        def popen_factory(argv):
            launches.append(argv)
            return FakeProc(argv, lines=first_run if len(launches) == 1 else second_run)

        self.sp.stub(["stdbuf"], proc=popen_factory)
        with mock.patch.object(mon_mod, "time", clock):
            thread.run()

        self.assertEqual(events, [True])
        self.assertEqual(resumed, [True], "hardware_resumed must fire after a stable monitor exits")
        self.assertEqual(len(launches), 2, "the monitor command must be restarted after EOF")
        self.assertEqual(launches[0], launches[1])
        self.assertEqual(launches[0][-3:], ["alsactl", "monitor", "hw:2"])

    def test_8_config_string_cleaning_and_defaults(self):
        """Verifies string cleaning utility and default configuration loading (no config on disk)."""
        self.assertEqual(cfg_mod.clean_val('  "hello world"  '), "hello world")
        self.assertEqual(cfg_mod.clean_val(" 'test' "), "test")
        self.assertEqual(cfg_mod.clean_val(""), "")

        self.assertFalse(cfg_mod.CONFIG_FILE.exists())
        cfg = cfg_mod.load_config()
        self.assertEqual(cfg["ALSA_CARD_ID"], cfg_mod.DEFAULT_CONFIG["ALSA_CARD_ID"])
        self.assertEqual(cfg["NODE_TARGET"], cfg_mod.DEFAULT_CONFIG["NODE_TARGET"])
        self.assertFalse(cfg_mod.CONFIG_FILE.exists(), "load_config must not create the file")

    def test_9_engine_pin_management(self):
        """Verifies pin and unpin operations within AudioEngine (persisted in the temp dir only)."""
        engine = eng_mod.AudioEngine()
        engine.pin_app("spotify", "Headphones")
        self.assertEqual(engine.pinned_apps.get("spotify"), "Headphones")
        self.assertEqual(cfg_mod.load_pinned_apps(), {"spotify": "Headphones"})

        engine.unpin_app("spotify")
        self.assertNotIn("spotify", engine.pinned_apps)

        engine.pin_app("vlc", "Speakers")
        engine.clear_all_pins()
        self.assertEqual(len(engine.pinned_apps), 0)
        self.assertEqual(cfg_mod.load_pinned_apps(), {})
        self.assertTrue(str(cfg_mod.PINS_FILE).startswith(str(self.base)))

    def test_10_mpris_playerctl_title_resolution(self):
        """Verifies MPRIS browser tab and media title resolution via playerctl."""
        self.sp.stub(
            ["playerctl"], stdout="firefox: YouTube Video Title\nspotify: Song Name"
        )
        titles = mpris_mod.get_mpris_tab_titles()
        self.assertEqual(titles.get("firefox"), "YouTube Video Title")
        self.assertEqual(titles.get("spotify"), "Song Name")
        self.assertEqual(self.sp.find("playerctl")[0][:3], ["playerctl", "-a", "metadata"])


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
