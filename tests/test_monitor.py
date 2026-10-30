"""Tests for the background monitor threads (hda_split.monitor). No process is ever spawned."""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401
from hermetic import FakeClock, FakeProc, HermeticTestCase, get_qapp, wait_until  # noqa: E402

from unittest import mock  # noqa: E402

import hda_split.monitor as mon  # noqa: E402
from hda_split.monitor import JackMonitorThread, PulseEventThread  # noqa: E402


class MonitorBase(HermeticTestCase):
    def setUp(self):
        super().setUp()
        get_qapp()
        self.clock = FakeClock()
        p = mock.patch.object(mon, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def virtual(self, thread):
        """Makes the thread's sleeping use the fake clock."""
        thread.msleep = lambda ms: self.clock.sleep(ms / 1000.0)
        return thread

    def engine(self, states=(False,), card="2"):
        it = iter(states)
        last = [states[-1]]

        def plugged():
            try:
                last[0] = next(it)
            except StopIteration:
                pass
            return last[0]

        return SimpleNamespace(card_id=card, is_headphone_plugged=plugged)


class JackCommandTests(MonitorBase):
    def test_jack_command_targets_hw_card_not_a_bare_number(self):
        thread = JackMonitorThread(self.engine(card="2"))
        self.assertEqual(thread._command(), ["alsactl", "monitor", "hw:2"])
        thread = JackMonitorThread(self.engine(card="11"))
        self.assertEqual(thread._command(), ["alsactl", "monitor", "hw:11"])

    def test_jack_command_is_none_without_alsactl(self):
        self.missing_cmds.add("alsactl")
        self.assertIsNone(JackMonitorThread(self.engine())._command())

    def test_pulse_command_is_pactl_subscribe_or_none(self):
        self.assertEqual(PulseEventThread()._command(), ["pactl", "subscribe"])
        self.missing_cmds.add("pactl")
        self.assertIsNone(PulseEventThread()._command())

    def test_stdbuf_wraps_the_command_only_when_available_and_env_is_forced(self):
        thread = self.virtual(PulseEventThread())
        thread._run_once(["pactl", "subscribe"])
        call = self.sp.calls[-1]
        self.assertEqual(call["argv"], ["stdbuf", "-oL", "pactl", "subscribe"])
        self.assertEqual(call["kwargs"]["env"]["LC_ALL"], "C.UTF-8")

        self.missing_cmds.add("stdbuf")
        thread._run_once(["pactl", "subscribe"])
        self.assertEqual(self.sp.calls[-1]["argv"], ["pactl", "subscribe"])


class FailureLoopTests(MonitorBase):
    def run_failing(self, thread, launches_wanted, returncode=1):
        stamps = []

        def factory(argv):
            stamps.append(self.clock.now)
            if len(stamps) >= launches_wanted:
                thread._running = False
            return FakeProc(argv, lines=(), returncode=returncode)

        self.sp.stub(["stdbuf"], proc=factory)
        thread._loop()
        return stamps

    def test_failing_monitor_backs_off_exponentially_and_never_emits_hardware_resumed(self):
        thread = self.virtual(JackMonitorThread(self.engine()))
        resumed, events = [], []
        thread.hardware_resumed.connect(lambda: resumed.append(1))
        thread.jack_state_changed.connect(events.append)
        stamps = self.run_failing(thread, 8)

        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        expected = [1, 2, 4, 8, 16, 30, 30]
        self.assertEqual(len(gaps), len(expected))
        for got, want in zip(gaps, expected):
            self.assertAlmostEqual(got, want, delta=0.11)
        self.assertEqual(resumed, [], "a failing command is not a suspend/resume")
        self.assertEqual(events, [])

    def test_failing_pulse_monitor_does_not_emit_changed(self):
        thread = self.virtual(PulseEventThread())
        changed = []
        thread.changed.connect(lambda: changed.append(1))
        self.run_failing(thread, 4)
        self.assertEqual(changed, [])

    def test_a_stable_run_resets_the_backoff_and_then_reports_resume_once(self):
        thread = self.virtual(JackMonitorThread(self.engine(states=(False,))))
        resumed = []
        thread.hardware_resumed.connect(lambda: resumed.append(1))
        launches = []

        def factory(argv):
            launches.append(self.clock.now)
            n = len(launches)
            if n >= 4:
                thread._running = False
            if n == 3:
                self.clock.advance(mon._ProcessMonitorThread.STABLE_SECS + 1)
            return FakeProc(argv, lines=(), returncode=0)

        self.sp.stub(["stdbuf"], proc=factory)
        thread._loop()
        self.assertEqual(resumed, [1], "only the stable run counts as a resume")
        self.assertAlmostEqual(thread._delay, mon._ProcessMonitorThread.BACKOFF_MIN)

    def test_popen_raising_is_logged_and_retried(self):
        thread = self.virtual(PulseEventThread())
        calls = []

        def factory(argv):
            calls.append(argv)
            if len(calls) >= 3:
                thread._running = False
            raise FileNotFoundError("stdbuf")

        self.sp.stub(["stdbuf"], proc=factory)
        with self.assertLogs("hda_split", "WARNING"):
            thread._loop()
        self.assertEqual(len(calls), 3)

    def test_idle_without_alsactl_still_polls_the_jack(self):
        self.missing_cmds.add("alsactl")
        eng = self.engine(states=(False, False, True, True))
        thread = self.virtual(JackMonitorThread(eng))
        events = []
        thread.jack_state_changed.connect(events.append)
        ticks = []
        orig_idle = thread._idle

        def idle():
            orig_idle()
            ticks.append(1)
            if len(ticks) >= 3:
                thread._running = False

        thread._idle = idle
        thread.run()
        self.assertEqual(events, [True])
        self.assertEqual(self.sp.find("alsactl"), [])


class PulseEventTests(MonitorBase):
    def test_pulse_thread_emits_for_sink_and_card_lines_but_not_for_client_lines(self):
        thread = PulseEventThread()
        changed = []
        thread.changed.connect(lambda: changed.append(1))
        relevant = [
            "Event 'new' on sink-input #12\n",
            "Event 'remove' on sink-input #12\n",
            "Event 'change' on sink #5\n",
            "Event 'new' on card #2\n",
            "Event 'change' on card #2\n",
        ]
        ignored = [
            "Event 'new' on client #3\n",
            "Event 'change' on client #3\n",
            "Event 'change' on server #0\n",
            "Event 'change' on source #4\n",
            "Event 'new' on module #9\n",
            "\n",
        ]
        for line in ignored:
            thread._on_line(line)
        self.assertEqual(changed, [])
        for line in relevant:
            thread._on_line(line)
        self.assertEqual(len(changed), len(relevant))

    def test_pulse_thread_signals_a_refresh_when_the_server_comes_back(self):
        thread = PulseEventThread()
        changed = []
        thread.changed.connect(lambda: changed.append(1))
        thread._on_stable_exit()
        self.assertEqual(changed, [1])

    def test_jack_line_triggers_a_state_check_and_only_changes_are_emitted(self):
        thread = JackMonitorThread(self.engine(states=(False, True, True, False)))
        thread._last_state = thread.engine.is_headphone_plugged()  # False
        events = []
        thread.jack_state_changed.connect(events.append)
        for _ in range(3):
            thread._on_line("node hw:2, #3 (0,0,0,Headphone Jack,0) VALUE\n")
        self.assertEqual(events, [True, False])


class LifecycleTests(MonitorBase):
    def test_stop_terminates_a_running_process_and_ends_the_loop(self):
        thread = JackMonitorThread(self.engine())
        proc = FakeProc(["x"], returncode=None)
        thread._proc = proc
        thread.stop()
        self.assertFalse(thread._running)
        self.assertTrue(proc.terminated)

    def test_a_process_that_outlives_the_read_loop_is_terminated(self):
        thread = self.virtual(PulseEventThread())
        proc = FakeProc(["pactl"], returncode=None)
        self.sp.stub(["stdbuf"], proc=lambda argv: proc)
        thread._run_once(["pactl", "subscribe"])
        self.assertTrue(proc.terminated)

    def test_real_thread_delivers_events_and_stops_quickly(self):
        app = get_qapp()
        import time as real_time

        with mock.patch.object(mon, "time", real_time):  # this test runs a real QThread on real time
            self.sp.stub(["stdbuf"], proc=lambda argv: FakeProc(
                argv, lines=["Event 'new' on sink-input #1\n", "Event 'change' on client #2\n"]))
            thread = PulseEventThread()
            got = []
            thread.changed.connect(lambda: got.append(1))
            thread.start()
            try:
                self.assertTrue(wait_until(app, lambda: len(got) >= 1, 3000))
            finally:
                thread.stop()
            self.assertFalse(thread.isRunning())
        self.assertEqual(len(got), 1)


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
