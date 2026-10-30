"""GUI tests on the offscreen platform with a fake engine and fake monitor threads.

Nothing here touches PipeWire/ALSA: gui.AudioEngine, JackMonitorThread, PulseEventThread and send_notification
are replaced, and the subprocess guard of HermeticTestCase is active as well.
"""

import os
import sys
import threading
import time
from typing import Any
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401
from hermetic import HermeticTestCase, get_qapp, settle, wait_until  # noqa: E402

from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QMessageBox, QPushButton  # noqa: E402

import hda_split.gui as gui  # noqa: E402

MAIN_THREAD = threading.get_ident()


def stream(sid, app, binary, sink, volume=50, pid="10", muted=False):
    return {"id": sid, "app": app, "binary": binary, "pid": pid, "volume": volume,
            "muted": muted, "sink_name": sink, "media_title": "", "window_name": "", "media_name": ""}


class FakeEngine:
    """Records every call together with the thread it ran on."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.target_node = "alsa_output.fake.pro-output-0"
        self.recovery_exhausted = False
        self.missing_sinks = set()
        self.sinks_ok = True
        self.force_ret = 3
        self.snapshots = 0
        self.streams = [
            stream("1", "Firefox", "firefox", "Headphones", volume=120),
            stream("2", "Firefox", "firefox", "Speakers"),
            stream("3", "BT Player", "btplayer", "bluez_output.xx", pid="11"),
            stream("4", "Mystery", "", "Headphones", pid="12"),
        ]

    def _rec(self, name, *a, **kw):
        with self.lock:
            self.calls.append((name, a, kw, threading.get_ident()))
        time.sleep(0.002)

    def names(self):
        with self.lock:
            return [c[0] for c in self.calls]

    def find(self, name):
        with self.lock:
            return [c for c in self.calls if c[0] == name]

    def clear(self):
        with self.lock:
            self.calls.clear()

    def ensure_hardware_ready(self):
        self._rec("ensure_hardware_ready")

    def enforce_hardware_links(self):
        self._rec("enforce_hardware_links")

    def ensure_sinks_exist(self, force_reload=False):
        self._rec("ensure_sinks_exist", force_reload=force_reload)
        return self.sinks_ok

    def is_headphone_plugged(self):
        self._rec("is_headphone_plugged")
        return True

    def recover_missing_sinks(self):
        self._rec("recover_missing_sinks")
        return self.sinks_ok

    def get_sink_info(self):
        self._rec("get_sink_info")
        sinks = {
            "Headphones": {"volume": 130, "muted": False},
            "Speakers": {"volume": 40, "muted": True},
            "Both": {"volume": 100, "muted": False},
            self.target_node: {"volume": 55, "muted": False},
        }
        return {name: data for name, data in sinks.items() if name not in self.missing_sinks}

    def get_external_sinks(self):
        self._rec("get_external_sinks")
        return [{"name": "bt"}]

    def get_streams(self):
        self._rec("get_streams")
        with self.lock:
            self.snapshots += 1
        time.sleep(0.01)
        return [dict(s) for s in self.streams]


    def route_stream(self, sid, target):
        self._rec("route_stream", sid, target)
        return True

    def force_all_streams(self, target):
        self._rec("force_all_streams", target)
        return self.force_ret

    def follow_jack(self, plugged):
        self._rec("follow_jack", plugged)
        return self.force_ret

    def choose_output(self, stream_id, target):
        self._rec("choose_output", stream_id, target)
        return True

    def set_sink_volume(self, name, vol):
        self._rec("set_sink_volume", name, vol)

    def set_sink_mute(self, name, muted):
        self._rec("set_sink_mute", name, muted)

    def set_stream_volume(self, sid, vol):
        self._rec("set_stream_volume", sid, vol)

    def set_stream_mute(self, sid, muted):
        self._rec("set_stream_mute", sid, muted)





class FakeJack(QObject):
    jack_state_changed = pyqtSignal(bool)
    hardware_resumed = pyqtSignal()

    def __init__(self, engine=None, parent=None):
        super().__init__(parent)
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakePulse(QObject):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class GuiBase(HermeticTestCase):
    engine_cls = FakeEngine

    def setUp(self):
        super().setUp()
        self.app = get_qapp()
        self.tray_available = False
        self.notifications = []
        self.quit = mock.Mock()
        for p in (
            mock.patch.object(gui, "AudioEngine", lambda: self.engine_cls()),
            mock.patch.object(gui, "JackMonitorThread", FakeJack),
            mock.patch.object(gui, "PulseEventThread", FakePulse),
            mock.patch.object(gui, "send_notification",
                              lambda t, m, urgency="normal": self.notifications.append((t, m, threading.get_ident()))),
            mock.patch.object(gui.QSystemTrayIcon, "isSystemTrayAvailable",
                              staticmethod(lambda: self.tray_available)),
            mock.patch.object(gui.QApplication, "quit", staticmethod(self.quit)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def window(self, start_in_tray=False, wait_rows=True) -> Any:
        """Typed Any: the window holds the fakes patched in above, which static analysis cannot know."""
        win: Any = gui.AppWindow(start_in_tray=start_in_tray)
        self.addCleanup(self._dispose, win)
        self.assertTrue(wait_until(self.app, lambda: win._started), "startup did not finish")
        if wait_rows:
            self.assertTrue(wait_until(self.app, lambda: len(win.stream_rows) == len(win.engine.streams)),
                            f"rows: {list(win.stream_rows)}")
        return win

    def _dispose(self, win):
        win.shutdown()
        win.deleteLater()
        self.app.processEvents()

    def dialogs(self):
        return [w for w in self.app.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()]

    def settle(self, ms=100):
        settle(self.app, ms)


class StartupTests(GuiBase):
    def test_startup_and_snapshots_run_off_the_gui_thread(self):
        win = self.window()
        eng = win.engine
        self.assertTrue(all(c[3] != MAIN_THREAD for c in eng.calls), set(eng.names()))
        self.assertTrue({"ensure_hardware_ready", "ensure_sinks_exist", "get_streams", "recover_missing_sinks"} <= set(eng.names()))
        self.assertTrue(win.jack_monitor.started and win.pulse_monitor.started)
        self.assertEqual(win.tray_jack_action.text(), "Jack Connected")

    def test_start_in_tray_shows_the_window_when_no_tray_exists(self):
        self.assertTrue(self.window(start_in_tray=True).isVisible())

    def test_start_in_tray_stays_hidden_when_a_tray_exists(self):
        self.tray_available = True
        win = self.window(start_in_tray=True)
        self.assertFalse(win.isVisible())
        win.show_and_raise()
        self.assertTrue(win.isVisible())

    def test_missing_sinks_are_reported_in_the_status_bar(self):
        class Bad(FakeEngine):
            def __init__(self):
                super().__init__()
                self.sinks_ok = False

        self.engine_cls = Bad
        win = self.window()
        self.assertTrue(wait_until(self.app, lambda: "Virtual sinks unavailable" in win.status.currentMessage()))

    def test_startup_failure_is_reported_and_the_gui_keeps_working(self):
        class Boom(FakeEngine):
            def ensure_sinks_exist(self, force_reload=False):
                raise RuntimeError("pipewire exploded")

            def recover_missing_sinks(self):
                return False

        self.engine_cls = Boom
        win = self.window()
        self.assertIn("startup failed", win.status.currentMessage())
        self.assertFalse(win._sinks_ok)


class SliderTests(GuiBase):
    def test_slider_range_is_0_to_100_and_labels_show_the_real_value(self):
        win = self.window()
        row = win.stream_rows["1"]
        self.assertEqual((row.slider.minimum(), row.slider.maximum()), (0, 100))
        self.assertEqual((win.master_hp.slider.minimum(), win.master_hp.slider.maximum()), (0, 100))
        self.assertEqual(row.slider.value(), 100)
        self.assertEqual(row.vol_lbl.text(), "120%")
        self.assertEqual(win.master_hp.slider.value(), 100)
        self.assertEqual(win.master_hp.val_lbl.text(), "130%")
        self.assertEqual(win.master_spk.slider.value(), 40)

    def test_hardware_output_level_is_visible_and_never_raised_automatically(self):
        # WirePlumber can leave the pro-audio node at 40%: every other slider then reads 100% but sounds quiet
        win = self.window()
        self.assertTrue(wait_until(self.app, lambda: win.master_hw.slider.value() == 55))
        self.assertEqual(win.master_hw.val_lbl.text(), "55%")
        self.assertEqual(win.engine.find("set_sink_volume"), [])

        win.master_hw.slider.setValue(60)
        self.assertTrue(wait_until(self.app, lambda: win.engine.find("set_sink_volume")))
        self.assertEqual(win.engine.find("set_sink_volume")[-1][1], (win.engine.target_node, 60))

    def test_slider_cannot_be_dragged_past_100(self):
        win = self.window()
        win.stream_rows["2"].slider.setValue(500)
        self.assertEqual(win.stream_rows["2"].slider.value(), 100)

    def test_slider_burst_is_coalesced_and_the_last_value_wins(self):
        win = self.window()
        eng = win.engine
        self.settle(200)
        eng.clear()
        row = win.stream_rows["1"]
        for v in range(10, 60):
            row.slider.setValue(v)
        self.assertTrue(wait_until(self.app, lambda: eng.find("set_stream_volume") and not row._vol_sender.busy()))
        vols = eng.find("set_stream_volume")
        self.assertLessEqual(len(vols), 3, f"{len(vols)} commands for 50 slider ticks")
        self.assertEqual(vols[-1][1], ("1", 59))
        self.assertTrue(all(c[3] != MAIN_THREAD for c in vols))

    def test_snapshot_does_not_overwrite_a_pending_slider_change(self):
        win = self.window()
        row = win.stream_rows["1"]
        row.slider.setValue(30)
        self.assertTrue(row._vol_sender.busy())
        row.update_data(stream("1", "Firefox", "firefox", "Headphones", volume=77))
        self.assertEqual(row.slider.value(), 30)

    def test_master_volume_and_mute_go_through_the_worker_in_order(self):
        win = self.window()
        eng = win.engine
        self.settle(150)
        eng.clear()
        win.master_spk.slider.setValue(20)
        win.master_spk.mute_btn.setChecked(False)
        win.master_spk.mute_btn.setChecked(True)
        self.assertTrue(wait_until(self.app, lambda: len(eng.find("set_sink_mute")) == 2 and eng.find("set_sink_volume")))
        self.assertEqual([c[1][1] for c in eng.find("set_sink_mute")], [False, True])
        self.assertEqual(eng.find("set_sink_volume")[-1][1], ("Speakers", 20))
        self.assertTrue(all(c[3] != MAIN_THREAD for c in eng.find("set_sink_mute")))

    def test_stream_mute_goes_through_the_worker(self):
        win = self.window()
        win.engine.clear()
        win.stream_rows["1"].mute_btn.click()
        self.assertTrue(wait_until(self.app, lambda: win.engine.find("set_stream_mute")))
        call = win.engine.find("set_stream_mute")[0]
        self.assertEqual(call[1], ("1", True))
        self.assertNotEqual(call[3], MAIN_THREAD)

    def test_coalesced_sender_sends_only_the_latest_value(self):
        win = self.window()
        sent = []
        sender = gui.CoalescedSender(win, sent.append, win)
        for v in (1, 2, 3, 4):
            sender.push(v)
        self.assertTrue(sender.busy())
        self.assertTrue(wait_until(self.app, lambda: sent and not sender.busy()))
        self.assertEqual(sent, [4])


class RouteTests(GuiBase):
    def test_route_click_records_a_choice_for_that_stream_only(self):
        win = self.window()
        win.engine.clear()
        win.stream_rows["2"].btn_both.click()
        self.assertTrue(wait_until(self.app, lambda: win.engine.find("choose_output")))
        self.settle(50)
        self.assertEqual([c[1] for c in win.engine.find("choose_output")], [("2", "Both")])
        self.assertEqual(win.engine.find("force_all_streams"), [])
        self.assertNotEqual(win.engine.find("choose_output")[0][3], MAIN_THREAD)

    def test_there_is_no_pin_control(self):
        win = self.window()
        self.assertFalse(hasattr(win.stream_rows["1"], "btn_pin"))
        self.assertNotIn("Reset Pins", [b.text() for b in win.findChildren(QPushButton)])

    def test_status_bar_does_not_mention_pins(self):
        win = self.window()
        win.update_status()
        self.assertNotIn("pinned", win.status.currentMessage())

    def test_external_sink_shows_no_selected_segment(self):
        win = self.window()
        row = win.stream_rows["3"]
        self.assertFalse(any(b.isChecked() for b in (row.btn_hp, row.btn_spk, row.btn_both)))
        self.assertTrue(win.stream_rows["1"].btn_hp.isChecked())


class JackPlugTests(GuiBase):
    def test_plugging_follows_the_jack_without_asking(self):
        win = self.window()
        win.engine.clear()
        win.jack_monitor.jack_state_changed.emit(True)
        self.assertTrue(wait_until(self.app, lambda: win.engine.find("follow_jack")))
        call = win.engine.find("follow_jack")[0]
        self.assertEqual(call[1], (True,))
        self.assertNotEqual(call[3], MAIN_THREAD)
        self.assertEqual(self.dialogs(), [])
        self.assertEqual(win.tray_jack_action.text(), "Jack Connected")
        self.assertTrue(wait_until(self.app, lambda: self.notifications))
        self.assertIn("3 stream(s)", self.notifications[-1][1])

    def test_replugging_applies_the_same_rule_every_time(self):
        win = self.window()
        win.engine.clear()
        for plugged in (True, False, True):
            win.jack_monitor.jack_state_changed.emit(plugged)
            self.settle(50)
        self.assertTrue(wait_until(self.app, lambda: len(win.engine.find("follow_jack")) == 3))
        self.assertEqual([c[1][0] for c in win.engine.find("follow_jack")], [True, False, True])
        self.assertEqual(self.dialogs(), [])


class UnplugAndTrayTests(GuiBase):
    def test_unplug_follows_the_jack(self):
        win = self.window()
        win.engine.clear()
        win.jack_monitor.jack_state_changed.emit(False)
        self.assertTrue(wait_until(self.app, lambda: win.engine.find("follow_jack")))
        call = win.engine.find("follow_jack")[0]
        self.assertEqual(call[1], (False,))
        self.assertNotEqual(call[3], MAIN_THREAD)
        self.assertEqual(win.tray_jack_action.text(), "Jack Unplugged")
        self.assertTrue(wait_until(self.app, lambda: self.notifications))
        self.assertIn("3 stream(s)", self.notifications[-1][1])
        self.assertNotEqual(self.notifications[-1][2], MAIN_THREAD)

    def test_tray_force_moves_everything(self):
        win = self.window()
        win.engine.clear()
        win.force_all_streams("Headphones")
        self.assertTrue(wait_until(self.app, lambda: win.engine.find("force_all_streams")))
        self.assertEqual(win.engine.find("force_all_streams")[0][1], ("Headphones",))

    def test_hardware_resume_restores_alsa_state_off_thread(self):
        win = self.window()
        win.engine.clear()
        win.jack_monitor.hardware_resumed.emit()
        self.assertTrue(wait_until(self.app, lambda: win.engine.find("enforce_hardware_links")))
        self.assertTrue({"ensure_hardware_ready", "enforce_hardware_links"} <= set(win.engine.names()))
        self.assertTrue(all(c[3] != MAIN_THREAD for c in win.engine.calls))

    def test_repair_forces_a_reload_and_reports_the_outcome(self):
        win = self.window()
        win.engine.sinks_ok = False
        win.fix_btn.click()
        self.assertFalse(win.fix_btn.isEnabled())
        self.assertTrue(wait_until(self.app, lambda: win.fix_btn.isEnabled()))
        self.assertIn("Repair failed", win.status.currentMessage())
        self.assertEqual(win.engine.find("ensure_sinks_exist")[-1][2], {"force_reload": True})
        win.engine.sinks_ok = True
        win.fix_btn.click()
        self.assertTrue(wait_until(self.app, lambda: win.status.currentMessage() == "Audio pipeline reloaded"))


class MissingSinkTests(GuiBase):
    def _lose_headphones(self, win):
        win.engine.missing_sinks = {"Headphones"}
        win.engine.sinks_ok = False
        win.engine.recovery_exhausted = True  # no retry timer: the test drives each refresh
        win.request_refresh()
        self.assertTrue(wait_until(self.app, lambda: not win.master_hp.isEnabled()))

    def test_controls_of_a_missing_output_are_disabled_with_an_explanation(self):
        # The button used to look clickable while the stream silently stayed where it was
        win = self.window()
        self._lose_headphones(win)
        row = win.stream_rows["2"]
        self.assertFalse(row.btn_hp.isEnabled())
        self.assertIn("not available", row.btn_hp.toolTip())
        self.assertTrue(row.btn_spk.isEnabled())
        self.assertTrue(row.btn_both.isEnabled())
        self.assertTrue(win.master_spk.isEnabled())
        self.assertFalse(win.tray_force_actions["Headphones"].isEnabled())
        self.assertTrue(win.tray_force_actions["Speakers"].isEnabled())

        win.engine.clear()
        row.btn_hp.click()
        self.settle(100)
        self.assertEqual(win.engine.find("route_stream"), [])

    def test_controls_come_back_with_the_output(self):
        win = self.window()
        self._lose_headphones(win)
        win.engine.missing_sinks = set()
        win.engine.sinks_ok = True
        win.request_refresh()
        self.assertTrue(wait_until(self.app, lambda: win.master_hp.isEnabled()))
        row = win.stream_rows["2"]
        self.assertTrue(row.btn_hp.isEnabled())
        self.assertEqual(row.btn_hp.toolTip(), "")
        self.assertTrue(win.tray_force_actions["Headphones"].isEnabled())

    def test_a_new_stream_also_gets_the_missing_output_disabled(self):
        win = self.window()
        self._lose_headphones(win)
        win.engine.streams = win.engine.streams + [stream("9", "Mpv", "mpv", "Speakers")]
        win.request_refresh()
        self.assertTrue(wait_until(self.app, lambda: "9" in win.stream_rows))
        self.assertFalse(win.stream_rows["9"].btn_hp.isEnabled())



class RefreshTests(GuiBase):
    def test_missing_sinks_are_rechecked_off_the_gui_thread_until_they_return(self):
        win = self.window()
        eng = win.engine
        self.settle(150)
        win._recovery_timer.setInterval(20)
        eng.sinks_ok = False
        eng.clear()
        win.request_refresh()
        self.assertTrue(wait_until(self.app, lambda: len(eng.find("recover_missing_sinks")) >= 3))
        self.assertTrue(all(c[3] != MAIN_THREAD for c in eng.find("recover_missing_sinks")))
        self.assertIn("Virtual sinks unavailable", win.statusBar().currentMessage())

        eng.sinks_ok = True
        self.assertTrue(wait_until(self.app, lambda: win._sinks_ok))
        self.settle(100)
        eng.clear()
        self.settle(100)
        self.assertEqual(eng.find("recover_missing_sinks"), [], "no more retries once the sinks are back")

    def test_sink_rechecks_stop_when_recovery_gives_up(self):
        win = self.window()
        eng = win.engine
        self.settle(150)
        win._recovery_timer.setInterval(20)
        eng.sinks_ok = False
        eng.recovery_exhausted = True
        win.request_refresh()
        self.assertTrue(wait_until(self.app, lambda: not win._sinks_ok))
        self.settle(100)
        eng.clear()
        self.settle(100)
        self.assertEqual(eng.find("recover_missing_sinks"), [])

    def test_event_flood_produces_a_bounded_number_of_snapshots(self):
        win = self.window()
        self.settle(300)
        win.engine.snapshots = 0
        for _ in range(40):
            win.pulse_monitor.changed.emit()
            self.settle(3)
        self.settle(500)
        self.assertGreaterEqual(win.engine.snapshots, 1)
        self.assertLessEqual(win.engine.snapshots, 8)

    def test_requests_during_an_inflight_snapshot_collapse_into_one_more(self):
        win = self.window()
        self.settle(300)
        win.engine.snapshots = 0
        for _ in range(3):
            win.request_refresh()
        self.assertTrue(wait_until(self.app, lambda: win.engine.snapshots >= 2))
        self.settle(200)
        self.assertEqual(win.engine.snapshots, 2)

    def test_slow_refresh_timer_runs_only_while_visible(self):
        win = self.window()
        self.assertTrue(win._slow_timer.isActive())
        win.hide()
        self.assertFalse(win._slow_timer.isActive())
        win.show()
        self.assertTrue(win._slow_timer.isActive())

    def test_rows_disappear_with_their_streams_and_filter_applies(self):
        win = self.window()
        win.engine.streams = [s for s in win.engine.streams if s["id"] != "2"]
        win.request_refresh()
        self.assertTrue(wait_until(self.app, lambda: "2" not in win.stream_rows))
        win.search.setText("bt player")
        self.assertTrue(win.stream_rows["3"].isVisibleTo(win))
        self.assertFalse(win.stream_rows["1"].isVisibleTo(win))


class LifecycleTests(GuiBase):
    def test_close_without_a_tray_shuts_down_and_calls_quit(self):
        win = self.window()
        self.tray_available = False
        win.close()
        self.quit.assert_called_once_with()
        self.assertTrue(win._closing)
        self.assertTrue(win.jack_monitor.stopped and win.pulse_monitor.stopped)

    def test_close_with_a_tray_only_hides_the_window(self):
        self.tray_available = True
        win = self.window()
        win.show()
        win.close()
        self.settle(30)
        self.assertFalse(win.isVisible())
        self.quit.assert_not_called()
        self.assertFalse(win._closing)
        self.assertFalse(win.jack_monitor.stopped)

    def test_quit_from_the_tray_menu_really_quits_even_with_a_tray(self):
        self.tray_available = True
        win = self.window()
        win.quit_application()
        self.quit.assert_called_once_with()
        self.assertTrue(win._closing)

    def test_forced_close_with_a_tray_quits(self):
        self.tray_available = True
        win = self.window()
        win._force_quit = True
        win.close()
        self.quit.assert_called_once_with()

    def test_nothing_runs_after_shutdown(self):
        win = self.window()
        win.shutdown()
        done = []
        win.run_async(lambda: 1, on_done=done.append)
        win.schedule_refresh()
        win.pulse_monitor.changed.emit()
        self.settle(300)
        self.assertEqual(done, [])
        self.assertFalse(win._refresh_timer.isActive())

