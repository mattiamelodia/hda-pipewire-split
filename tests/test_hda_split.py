import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import hda_split.config as cfg_mod
import hda_split.engine as eng_mod
import hda_split.mpris as mpris_mod
from hda_split.monitor import JackMonitorThread


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


class TestHdaSplitSuite(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp_dir.name)
        cfg_mod.CONFIG_DIR = self.base / "config"
        cfg_mod.CONFIG_FILE = cfg_mod.CONFIG_DIR / "config.env"
        cfg_mod.PINS_FILE = cfg_mod.CONFIG_DIR / "pinned_apps.json"
        cfg_mod.PW_CONF_DIR = self.base / "pipewire.conf.d"
        cfg_mod.PW_DROPIN_FILE = cfg_mod.PW_CONF_DIR / "99-hda-split.conf"

        eng_mod.PW_DROPIN_FILE = cfg_mod.PW_DROPIN_FILE

    def tearDown(self):
        self.tmp_dir.cleanup()

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

    @patch("hda_split.engine.subprocess.run")
    @patch("hda_split.engine.run_cmd", return_value="")
    def test_3_isolated_route_stream_no_global_pw_link(self, _mock_cmd, mock_run):
        """Verifies that routing to 'Both' moves only the input to the Both sink without global pw-links."""
        engine = eng_mod.AudioEngine()
        mock_run.reset_mock()

        engine.route_stream("112", "Both")
        calls = [c.args[0] for c in mock_run.call_args_list]
        self.assertIn(["pactl", "move-sink-input", "112", "Both"], calls)
        for cmd in calls:
            self.assertNotIn("pw-link", cmd)

    @patch("hda_split.engine.get_mpris_tab_titles", return_value={"firefox": "Lecture 04 - ML Security"})
    @patch("hda_split.engine.run_cmd")
    def test_4_stream_parsing_and_loopback_filtering(self, mock_cmd, _mock_mpris):
        """Verifies that internal loopback streams are filtered and MPRIS titles are associated."""
        def side_effect(args):
            if args == ["pactl", "list", "sink-inputs"]:
                return MOCK_PACTL_SINK_INPUTS
            if args == ["pactl", "list", "sinks", "short"]:
                return MOCK_PACTL_SINKS_SHORT
            return ""

        mock_cmd.side_effect = side_effect
        engine = eng_mod.AudioEngine()
        streams = engine.get_streams()

        self.assertEqual(len(streams), 1)
        s = streams[0]
        self.assertEqual(s["id"], "112")
        self.assertEqual(s["app_key"], "firefox")
        self.assertEqual(s["media_title"], "Lecture 04 - ML Security")
        self.assertEqual(s["sink_name"], "Headphones")

    @patch("hda_split.engine.run_cmd")
    def test_5_sink_master_volumes_and_external_bluetooth_detection(self, mock_cmd):
        """Verifies Master volume reading and external Bluetooth headphone detection."""
        mock_cmd.side_effect = lambda args: MOCK_PACTL_SINKS if args == ["pactl", "list", "sinks"] else ""
        engine = eng_mod.AudioEngine()

        info = engine.get_sink_info()
        self.assertEqual(info["Speakers"]["volume"], 75)
        self.assertFalse(info["Speakers"]["muted"])
        self.assertEqual(info["Headphones"]["volume"], 100)
        self.assertTrue(info["Headphones"]["muted"])

        ext = engine.get_external_sinks()
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0]["description"], "Sony WH-1000XM5")

    @patch("hda_split.engine.run_cmd")
    def test_6_dynamic_jack_detection_numid(self, mock_cmd):
        """Verifies dynamic parsing of Jack numid and reading of on/off state."""
        def side_effect(args):
            if "controls" in args:
                return "numid=14,iface=CARD,name='Headphone Jack'\nnumid=15,iface=MIXER,name='Master Playback Volume'"
            if "numid=14" in args:
                return "numid=14,iface=CARD,name='Headphone Jack'\n  ; type=BOOLEAN,access=r-------,values=1\n  : values=on"
            return ""

        mock_cmd.side_effect = side_effect
        engine = eng_mod.AudioEngine()
        self.assertEqual(engine.jack_numid, "14")
        self.assertTrue(engine.is_headphone_plugged())

    @patch("hda_split.monitor.shutil.which", return_value="/usr/bin/alsactl")
    @patch("hda_split.monitor.subprocess.Popen")
    def test_7_monitor_thread_emits_signal_and_recovers_on_eof(self, mock_popen, _mock_which):
        """Verifies that JackMonitorThread emits the instant signal and recovers after an EOF (suspend)."""
        mock_engine = MagicMock()
        mock_engine.card_id = "2"
        states = [False, True, True]
        mock_engine.is_headphone_plugged.side_effect = lambda: states.pop(0) if states else True

        proc_mock = MagicMock()
        proc_mock.stdout = ["node hw:2, #14 (0,0,0,Headphone Jack,0) VALUE\n"]
        mock_popen.return_value = proc_mock

        thread = JackMonitorThread(mock_engine)
        received_events = []
        resumed_called = []

        def on_change(state):
            received_events.append(state)
            thread._running = False

        thread.jack_state_changed.connect(on_change)
        thread.hardware_resumed.connect(lambda: resumed_called.append(True))
        thread.run()

        self.assertEqual(received_events, [True])

    # --- NUOVI TEST AGGIUNTI (Ampliamento) ---

    def test_8_config_string_cleaning_and_defaults(self):
        """Verifies string cleaning utility and default configuration loading."""
        self.assertEqual(cfg_mod.clean_val('  "hello world"  '), "hello world")
        self.assertEqual(cfg_mod.clean_val(" 'test' "), "test")
        self.assertEqual(cfg_mod.clean_val(""), "")

        cfg = cfg_mod.load_config()
        self.assertIn("ALSA_CARD_ID", cfg)
        self.assertIn("NODE_TARGET", cfg)

    def test_9_engine_pin_management(self):
        """Verifies pin and unpin operations within AudioEngine."""
        engine = eng_mod.AudioEngine()
        engine.pin_app("spotify", "Headphones")
        self.assertEqual(engine.pinned_apps.get("spotify"), "Headphones")

        engine.unpin_app("spotify")
        self.assertNotIn("spotify", engine.pinned_apps)

        engine.pin_app("vlc", "Speakers")
        engine.clear_all_pins()
        self.assertEqual(len(engine.pinned_apps), 0)

    @patch("hda_split.mpris.run_cmd")
    @patch("hda_split.mpris.shutil.which", return_value="/usr/bin/playerctl")
    def test_10_mpris_playerctl_title_resolution(self, _mock_which, mock_run):
        """Verifies MPRIS browser tab and media title resolution via playerctl."""
        mock_run.return_value = "firefox: YouTube Video Title\nspotify: Song Name"
        titles = mpris_mod.get_mpris_tab_titles()
        self.assertEqual(titles.get("firefox"), "YouTube Video Title")
        self.assertEqual(titles.get("spotify"), "Song Name")


if __name__ == "__main__":
    unittest.main(verbosity=2)