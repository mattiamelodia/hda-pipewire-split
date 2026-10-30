"""Tests for the CLI entry point (main.py): headless paths must never load Qt or run the wizard unasked."""

import argparse
import builtins
import contextlib
import io
import os
import sys
import uuid
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401
from hermetic import HermeticTestCase, get_qapp, sink_input, sinks_full, sinks_short, wait_until  # noqa: E402

import hda_split.config as cfg_mod  # noqa: E402
import hda_split.engine as eng_mod  # noqa: E402
import main as main_mod  # noqa: E402

REAL_SETUP_LOGGING = main_mod.setup_logging


class MainBase(HermeticTestCase):
    def setUp(self):
        super().setUp()
        self.sp.allow_audio_mutations()
        self.out, self.err = io.StringIO(), io.StringIO()
        self.notes = []
        for p in (
            mock.patch.object(main_mod, "setup_logging", lambda: None),
            mock.patch.object(main_mod, "send_notification", lambda *a, **k: self.notes.append((a, k))),
            mock.patch.object(builtins, "input", self._no_input),
            mock.patch.object(main_mod, "daemonize_process", self._no_daemon),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.input_calls = []
        self.daemonized = []

    def _no_input(self, *a, **k):
        self.input_calls.append(a)
        raise AssertionError("input() must not be called on this path")

    def _no_daemon(self):
        self.daemonized.append(True)
        raise AssertionError("must not daemonize")

    def run_main(self, *argv):
        """Runs main.main(); returns (exit code or None, stdout, stderr)."""
        code = None
        with mock.patch.object(sys, "argv", ["main.py", *argv]), \
                contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            try:
                main_mod.main()
            except SystemExit as e:
                code = e.code
        return code, self.out.getvalue(), self.err.getvalue()

    def configure(self):
        self.write_config()

    @contextlib.contextmanager
    def qt_blocked(self):
        """Simulates a machine/process where PyQt6 was never imported; any import of it fails loudly.

        The engine module is re-imported inside the block so that its own imports are checked as well."""
        import hda_split

        names = [k for k in sys.modules if k == "PyQt6" or k.startswith("PyQt6.")]
        names += [k for k in ("hda_split.gui", "hda_split.monitor", "hda_split.engine") if k in sys.modules]
        saved = {k: sys.modules.pop(k) for k in names}
        sys.modules["PyQt6"] = None  # makes `import PyQt6...` raise ImportError
        try:
            yield
        finally:
            for k in [k for k in sys.modules if k == "PyQt6" or k.startswith("PyQt6.")]:
                del sys.modules[k]
            if "hda_split.engine" in sys.modules and "hda_split.engine" in saved:
                del sys.modules["hda_split.engine"]
            sys.modules.update(saved)
            if "hda_split.engine" in saved:
                hda_split.engine = saved["hda_split.engine"]


class CliPathsTests(MainBase):
    def sinks(self):
        self.sp.stub(["pactl", "list", "sink-inputs"], stdout=sink_input(
            5, "99", {"application.name": "Firefox", "application.process.binary": "firefox",
                      "application.process.id": "10"}))
        self.sp.stub(["pactl", "list", "sinks", "short"], stdout=sinks_short(["Headphones", "Speakers", "Both"], 40))

    def test_cli_paths_do_not_import_pyqt6(self):
        self.configure()
        self.sinks()
        self.sp.stub(["pactl", "list", "sinks"], stdout=sinks_full([
            (40, "Headphones", "HP", "pw-loopback-1", 100, False), (41, "Speakers", "S", "pw-loopback-2", 100, False)]))
        for flag in ("--force-hp", "--force-spk", "--force-both", "--toggle-mute"):
            with self.subTest(flag=flag), self.qt_blocked():
                code, out, err = self.run_main(flag)
                self.assertIsNone(code, err)
                self.assertTrue(out.strip(), flag)
                self.assertFalse([k for k in sys.modules if k.startswith("PyQt6.")])
        self.assertEqual(self.input_calls, [])

    def test_wizard_flag_does_not_import_pyqt6(self):
        ran = []
        with self.qt_blocked(), mock.patch.object(sys.modules["wizard"], "run_wizard", lambda: ran.append(1)):
            self.assertIsNone(self.run_main("--wizard")[0])
        self.assertEqual(ran, [1])

    def test_force_flags_route_to_the_right_sink_and_report_the_count(self):
        self.configure()
        self.sinks()
        for flag, sink in (("--force-hp", "Headphones"), ("--force-spk", "Speakers"), ("--force-both", "Both")):
            with self.subTest(flag=flag):
                self.sp.reset()
                self.out.truncate(0), self.out.seek(0)
                code, out, _ = self.run_main(flag)
                self.assertIsNone(code)
                self.assertIn(f"Routed 1 active stream(s) to {sink}", out)
                self.assertIn(["pactl", "set-default-sink", sink], self.sp.argvs())
                self.assertIn(["pactl", "move-sink-input", "5", sink], self.sp.argvs())

    def test_force_flags_override_pins_because_the_user_asked_explicitly(self):
        self.configure()
        self.sinks()
        cfg_mod.save_pinned_apps({"firefox": "Speakers"})
        self.run_main("--force-hp")
        self.assertIn(["pactl", "move-sink-input", "5", "Headphones"], self.sp.argvs())

    def test_toggle_mute_does_not_call_ensure_hardware_ready_or_touch_alsa(self):
        self.configure()
        self.sp.stub(["pactl", "list", "sinks"], stdout=sinks_full([
            (40, "Headphones", "HP", "pw-loopback-1", 100, False), (41, "Speakers", "S", "pw-loopback-2", 100, False)]))
        with mock.patch.object(eng_mod.AudioEngine, "ensure_hardware_ready",
                               side_effect=AssertionError("ensure_hardware_ready called")) as hw:
            code, out, err = self.run_main("--toggle-mute")
        self.assertIsNone(code, err)
        hw.assert_not_called()
        self.assertIn("Outputs muted", out)
        mutations = [a for a in self.sp.argvs() if a[0] in ("amixer",) and "set" in a]
        self.assertEqual(mutations, [])
        self.assertEqual(self.sp.find("pactl", "set-card-profile"), [])
        self.assertEqual(self.sp.find("systemctl"), [])
        self.assertEqual(self.sp.find("pactl", "set-sink-mute"),
                         [["pactl", "set-sink-mute", "Headphones", "1"], ["pactl", "set-sink-mute", "Speakers", "1"]])

    def test_toggle_mute_unmutes_when_headphones_are_muted(self):
        self.configure()
        self.sp.stub(["pactl", "list", "sinks"], stdout=sinks_full([
            (40, "Headphones", "HP", "pw-loopback-1", 100, True), (41, "Speakers", "S", "pw-loopback-2", 100, True)]))
        code, out, _ = self.run_main("--toggle-mute")
        self.assertIn("Outputs unmuted", out)

    def test_handle_cli_actions_returns_false_without_flags_and_does_nothing(self):
        ns = argparse.Namespace(tray=False, wizard=False, force_hp=False, force_spk=False, force_both=False,
                                toggle_mute=False)
        self.assertFalse(main_mod.handle_cli_actions(ns))
        self.assertEqual(self.sp.calls, [])


class NotConfiguredTests(MainBase):
    def no_tty(self):
        p = mock.patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: False))
        p.start()
        self.addCleanup(p.stop)

    def test_no_config_and_no_tty_exits_1_without_prompting_or_wizard(self):
        self.no_tty()
        self.assertFalse(self.paths.CONFIG_FILE.exists())
        with mock.patch.object(sys.modules["wizard"], "run_wizard", side_effect=AssertionError("wizard ran")) as wz:
            for argv in ([], ["--tray"], ["--force-hp"], ["--force-spk"], ["--force-both"], ["--toggle-mute"]):
                with self.subTest(argv=argv):
                    self.notes.clear()
                    self.sp.reset()
                    code, out, err = self.run_main(*argv)
                    self.assertEqual(code, 1)
                    self.assertIn("--wizard", err)
                    self.assertEqual(len(self.notes), 1)
                    self.assertEqual(self.notes[0][1].get("urgency"), "critical")
                    self.assertEqual(self.input_calls, [])
                    self.assertEqual(self.daemonized, [])
                    self.assertEqual(self.sp.find("pactl", "set-default-sink"), [])
                    self.assertEqual([a for a in self.sp.argvs() if a[0] in ("amixer", "pw-link", "systemctl")], [])
            wz.assert_not_called()
        self.assertFalse(self.paths.CONFIG_FILE.exists())

    def test_no_config_with_a_terminal_runs_the_wizard_then_continues(self):
        p = mock.patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: True))
        p.start()
        self.addCleanup(p.stop)
        self.sp.stub(["pactl", "list", "sink-inputs"], stdout="")

        def fake_wizard():
            self.write_config()

        with mock.patch.object(sys.modules["wizard"], "run_wizard", side_effect=fake_wizard) as wz:
            code, out, err = self.run_main("--force-hp")
        self.assertIsNone(code, err)
        wz.assert_called_once_with()
        self.assertIn("Routed 0 active stream(s) to Headphones", out)

    def test_configured_system_never_starts_the_wizard(self):
        self.configure()
        with mock.patch.object(sys.modules["wizard"], "run_wizard", side_effect=AssertionError("wizard ran")):
            main_mod.ensure_configured()

    def test_wizard_flag_runs_wizard_even_without_config_and_returns(self):
        ran = []
        with mock.patch.object(sys.modules["wizard"], "run_wizard", lambda: ran.append(1)):
            code, _, _ = self.run_main("--wizard")
        self.assertIsNone(code)
        self.assertEqual(ran, [1])

    def test_missing_pactl_exits_1_before_anything_else(self):
        self.missing_cmds.add("pactl")
        with mock.patch.object(sys.modules["wizard"], "run_wizard", side_effect=AssertionError("wizard ran")):
            for argv in (["--wizard"], ["--force-hp"], []):
                code, _, err = self.run_main(*argv)
                self.assertEqual(code, 1)
                self.assertIn("pactl", err)
        self.assertEqual(self.sp.calls, [])


class ArgumentTests(MainBase):
    def test_parse_arguments(self):
        for flag, attr in (("--tray", "tray"), ("--wizard", "wizard"), ("--force-hp", "force_hp"),
                           ("--force-spk", "force_spk"), ("--force-both", "force_both"),
                           ("--toggle-mute", "toggle_mute")):
            with mock.patch.object(sys, "argv", ["main.py", flag]):
                ns = main_mod.parse_arguments()
            self.assertTrue(getattr(ns, attr), flag)
        with mock.patch.object(sys, "argv", ["main.py"]), contextlib.redirect_stderr(io.StringIO()):
            ns = main_mod.parse_arguments()
        self.assertFalse(any(vars(ns).values()))

    def test_debug_logging_is_opt_in(self):
        import logging

        for value, level in (("1", logging.DEBUG), ("", logging.WARNING)):
            with self.subTest(HDA_SPLIT_DEBUG=value), mock.patch.object(main_mod.logging, "basicConfig") as bc, \
                    mock.patch.dict(os.environ, {"HDA_SPLIT_DEBUG": value}):
                REAL_SETUP_LOGGING()
            self.assertEqual(bc.call_args.kwargs["level"], level)


class SingleInstanceTests(MainBase):
    def test_second_instance_defers_to_the_first_and_asks_it_to_show(self):
        app = get_qapp()
        name = f"hda-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        server = main_mod.acquire_single_instance(name, notify_existing=False)
        self.assertIsNotNone(server)
        received = []

        def on_conn():
            conn = server.nextPendingConnection()
            if conn is None:
                return

            def read():
                received.append(bytes(conn.readAll()))

            conn.readyRead.connect(read)
            if conn.bytesAvailable():
                read()

        server.newConnection.connect(on_conn)
        try:
            # acquire_single_instance blocks in waitForConnected, which needs the server's event loop to run:
            # drive it from a timer so the first instance can accept while the second waits.
            from PyQt6.QtCore import QTimer

            pump = QTimer()
            pump.timeout.connect(app.processEvents)
            pump.start(5)
            second = main_mod.acquire_single_instance(name, notify_existing=True)
            pump.stop()
            self.assertIsNone(second, "a running instance must be detected")
            self.assertTrue(wait_until(app, lambda: any(b"show" in r for r in received), 2000), received)
        finally:
            server.close()
            from PyQt6.QtNetwork import QLocalServer

            QLocalServer.removeServer(name)


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
