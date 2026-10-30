"""GUI tests on the offscreen platform with a fake engine and fake monitor threads.

Nothing here touches PipeWire/ALSA: gui.AudioEngine, JackMonitorThread, PulseEventThread and send_notification
are replaced, and the subprocess guard of HermeticTestCase is active as well.
"""

import os
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401
from hermetic import HermeticTestCase, get_qapp, settle, wait_until  # noqa: E402

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QMessageBox, QSystemTrayIcon  # noqa: E402

import hda_split.gui as gui  # noqa: E402

MAIN_THREAD = threading.get_ident()


def stream(sid, app, binary, key, sink, volume=50, pid="10", muted=False):
    return {"id": sid, "app": app, "binary": binary, "app_key": key, "pid": pid, "volume": volume,
            "muted": muted, "sink_name": sink, "media_title": "", "window_name": "", "media_name": ""}


class FakeEngine:
    """Records every call together with the thread it ran on."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.pinned_apps = {}
        self.sinks_ok = True
        self.force_ret = 3
        self.snapshots = 0
        self.streams = [
            stream("1", "Firefox", "firefox", "firefox", "Headphones", volume=120),
            stream("2", "Firefox", "firefox", "firefox", "Speakers"),
            stream("3", "BT Player", "btplayer", "btplayer", "bluez_output.xx", pid="11"),
            stream("4", "Mystery", "", "", "Headphones", pid="12"),
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

    def get_sink_info(self):
        self._rec("get_sink_info")
        return {"Headphones": {"volume": 130, "muted": False}, "Speakers": {"volume": 40, "muted": True}}

    def get_external_sinks(self):
        self._rec("get_external_sinks")
        return [{"name": "bt"}]

    def get_streams(self):
        self._rec("get_streams")
        with self.lock:
            self.snapshots += 1
        time.sleep(0.01)
        return [dict(s) for s in self.streams]

    def apply_pins(self, streams):
        self._rec("apply_pins")

    def route_stream(self, sid, target):
        self._rec("route_stream", sid, target)
        return True

    def force_all_streams(self, target, respect_pins=False):
        self._rec("force_all_streams", target, respect_pins=respect_pins)
        return self.force_ret

    def set_sink_volume(self, name, vol):
        self._rec("set_sink_volume", name, vol)

    def set_sink_mute(self, name, muted):
        self._rec("set_sink_mute", name, muted)

    def set_stream_volume(self, sid, vol):
        self._rec("set_stream_volume", sid, vol)

    def set_stream_mute(self, sid, muted):
        self._rec("set_stream_mute", sid, muted)

    def pin_app(self, key, target):
        self.pinned_apps[key] = target
        self._rec("pin_app", key, target)

    def unpin_app(self, key):
        self.pinned_apps.pop(key, None)
        self._rec("unpin_app", key)

    def clear_all_pins(self):
        self.pinned_apps.clear()
        self._rec("clear_all_pins")


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

    def window(self, start_in_tray=False, wait_rows=True):
        win = gui.AppWindow(start_in_tray=start_in_tray)
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
        self.assertTrue({"ensure_hardware_ready", "ensure_sinks_exist", "get_streams", "apply_pins"} <= set(eng.names()))
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
        row.update_data(stream("1", "Firefox", "firefox", "firefox", "Headphones", volume=77))
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


class PinAndRouteTests(GuiBase):
    def test_route_click_does_not_pin_an_unpinned_app(self):
        win = self.window()
        eng = win.engine
        eng.clear()
        row = win.stream_rows["3"]
        row.btn_hp.click()
        self.assertTrue(wait_until(self.app, lambda: eng.find("route_stream")))
        self.assertEqual(eng.find("route_stream")[0][1], ("3", "Headphones"))
        self.assertEqual(eng.find("pin_app"), [])
        self.assertEqual(eng.pinned_apps, {})
        self.assertFalse(row.btn_pin.isChecked())

    def test_route_click_moves_only_that_stream(self):
        win = self.window()
        win.engine.clear()
        win.stream_rows["2"].btn_both.click()
        self.assertTrue(wait_until(self.app, lambda: win.engine.find("route_stream")))
        self.settle(50)
        self.assertEqual([c[1] for c in win.engine.find("route_stream")], [("2", "Both")])
        self.assertEqual(win.engine.find("force_all_streams"), [])

    def test_route_click_on_a_pinned_app_moves_the_pin_with_it(self):
        win = self.window()
        win.engine.pinned_apps["firefox"] = "Headphones"
        win.sync_pin_buttons()
        win.stream_rows["2"].btn_spk.click()
        self.assertEqual(win.engine.pinned_apps["firefox"], "Speakers")

    def test_pin_is_refused_on_an_external_sink(self):
        win = self.window()
        row = win.stream_rows["3"]
        row.btn_pin.click()
        self.assertNotIn("btplayer", win.engine.pinned_apps)
        self.assertFalse(row.btn_pin.isChecked())
        self.assertIn("Headphones, Speakers or Both", win.status.currentMessage())
        self.assertEqual(win.engine.find("pin_app"), [])

    def test_pin_uses_the_current_internal_sink_and_syncs_sibling_rows(self):
        win = self.window()
        win.stream_rows["1"].btn_pin.click()
        self.assertEqual(win.engine.pinned_apps, {"firefox": "Headphones"})
        self.assertTrue(win.stream_rows["2"].btn_pin.isChecked(), "same app, other stream")
        win.stream_rows["1"].btn_pin.click()
        self.assertEqual(win.engine.pinned_apps, {})
        self.assertFalse(win.stream_rows["2"].btn_pin.isChecked())

    def test_unidentified_app_cannot_be_pinned(self):
        win = self.window()
        row = win.stream_rows["4"]
        self.assertFalse(row.btn_pin.isEnabled())
        self.assertIn("Cannot pin", row.btn_pin.toolTip())

    def test_external_sink_shows_no_selected_segment(self):
        win = self.window()
        row = win.stream_rows["3"]
        self.assertFalse(any(b.isChecked() for b in (row.btn_hp, row.btn_spk, row.btn_both)))
        self.assertTrue(win.stream_rows["1"].btn_hp.isChecked())

    def test_reset_pins_clears_everything(self):
        win = self.window()
        win.engine.pinned_apps["firefox"] = "Both"
        win.sync_pin_buttons()
        self.assertTrue(win.stream_rows["1"].btn_pin.isChecked())
        win.unpin_all_streams()
        self.assertEqual(win.engine.pinned_apps, {})
        self.assertFalse(win.stream_rows["1"].btn_pin.isChecked())


class PlugDialogTests(GuiBase):
    def plug(self, win):
        win.jack_monitor.jack_state_changed.emit(True)
        self.assertTrue(wait_until(self.app, lambda: len(self.dialogs()) >= 1))
        return self.dialogs()[0]

    def test_escape_on_the_plug_dialog_routes_nothing(self):
        win = self.window()
        dlg = self.plug(win)
        self.assertEqual(dlg.escapeButton().text(), "Keep Current")
        win.engine.clear()
        QTest.keyClick(dlg, Qt.Key.Key_Escape)
        self.assertTrue(wait_until(self.app, lambda: not self.dialogs()))
        self.settle(100)
        self.assertEqual(win.engine.find("force_all_streams"), [])
        self.assertIsNone(win._plug_dialog)

    def test_closing_the_plug_dialog_window_routes_nothing(self):
        win = self.window()
        dlg = self.plug(win)
        win.engine.clear()
        dlg.close()
        self.settle(100)
        self.assertEqual(win.engine.find("force_all_streams"), [])
        self.assertEqual(self.dialogs(), [])

    def test_repeated_plug_events_do_not_stack_dialogs(self):
        win = self.window()
        for _ in range(3):
            win.jack_monitor.jack_state_changed.emit(True)
            self.settle(30)
        self.assertEqual(len(self.dialogs()), 1)
        self.assertEqual(self.dialogs()[0].windowModality(), Qt.WindowModality.NonModal)

    def test_dialog_offers_the_four_choices(self):
        win = self.window()
        dlg = self.plug(win)
        self.assertEqual({b.text() for b in dlg.buttons()},
                         {"Headphones Only", "Speakers Only", "Both (Split)", "Keep Current"})

    def test_choosing_a_destination_routes_with_pins_respected(self):
        for label, target in (("Headphones Only", "Headphones"), ("Speakers Only", "Speakers"), ("Both (Split)", "Both")):
            with self.subTest(label=label):
                win = self.window()
                dlg = self.plug(win)
                win.engine.clear()
                {b.text(): b for b in dlg.buttons()}[label].click()
                self.assertTrue(wait_until(self.app, lambda: win.engine.find("force_all_streams")))
                call = win.engine.find("force_all_streams")[0]
                self.assertEqual((call[1], call[2]), ((target,), {"respect_pins": True}))
                self.assertNotEqual(call[3], MAIN_THREAD)
                self.assertTrue(wait_until(self.app, lambda: not self.dialogs()))
                self.assertIsNone(win._plug_dialog)

    def test_keep_current_routes_nothing(self):
        win = self.window()
        dlg = self.plug(win)
        win.engine.clear()
        {b.text(): b for b in dlg.buttons()}["Keep Current"].click()
        self.settle(100)
        self.assertEqual(win.engine.find("force_all_streams"), [])
        self.assertIn("routing unchanged", win.status.currentMessage())

    def test_unplugging_closes_an_open_dialog(self):
        win = self.window()
        self.plug(win)
        win.jack_monitor.jack_state_changed.emit(False)
        self.assertTrue(wait_until(self.app, lambda: not self.dialogs()))
        self.assertIsNone(win._plug_dialog)

    def test_plugging_re_applies_hardware_state_off_thread(self):
        win = self.window()
        win.engine.clear()
        self.plug(win)
        self.assertTrue(wait_until(self.app, lambda: win.engine.find("enforce_hardware_links")))
        self.assertTrue(all(c[3] != MAIN_THREAD for c in win.engine.calls))


class UnplugAndTrayTests(GuiBase):
    def test_unplug_moves_unpinned_streams_to_speakers_and_respects_pins(self):
        win = self.window()
        win.engine.clear()
        win.jack_monitor.jack_state_changed.emit(False)
        self.assertTrue(wait_until(self.app, lambda: win.engine.find("force_all_streams")))
        call = win.engine.find("force_all_streams")[0]
        self.assertEqual((call[1], call[2]), (("Speakers",), {"respect_pins": True}))
        self.assertNotEqual(call[3], MAIN_THREAD)
        self.assertEqual(win.tray_jack_action.text(), "Jack Unplugged")
        self.assertTrue(wait_until(self.app, lambda: self.notifications))
        self.assertIn("3 stream(s)", self.notifications[-1][1])
        self.assertNotEqual(self.notifications[-1][2], MAIN_THREAD)

    def test_tray_force_overrides_pins(self):
        win = self.window()
        win.engine.clear()
        win.force_all_streams("Headphones")
        self.assertTrue(wait_until(self.app, lambda: win.engine.find("force_all_streams")))
        call = win.engine.find("force_all_streams")[0]
        self.assertEqual((call[1], call[2]), (("Headphones",), {"respect_pins": False}))

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


class RefreshTests(GuiBase):
    def test_event_flood_produces_a_bounded_number_of_snapshots(self):
        win = self.window()
        self.settle(300)
        win.engine.snapshots = 0
        for _ in range(40):
            win.pulse_monitor.changed.emit()
            QTest.qWait(3)
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

    def test_shutdown_closes_an_open_plug_dialog(self):
        win = self.window()
        win.jack_monitor.jack_state_changed.emit(True)
        self.assertTrue(wait_until(self.app, lambda: self.dialogs()))
        win.shutdown()
        self.assertTrue(wait_until(self.app, lambda: not self.dialogs()))


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
