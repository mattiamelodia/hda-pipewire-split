import logging
import time
from typing import Any, Callable, Dict, Optional, Set

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QHideEvent, QIcon, QShowEvent
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSlider,
    QStatusBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .config import ICON_PATH, send_notification
from .engine import AudioEngine
from .monitor import JackMonitorThread, PulseEventThread

log = logging.getLogger(__name__)

# Hearing safety: sliders never go above 100% (PipeWire may report more; the label shows the real value)
MAX_SLIDER_VOLUME = 100
INTERNAL_SINK_NAMES = {"headphones": "Headphones", "speakers": "Speakers", "both": "Both"}
MISSING_OUTPUT_TOOLTIP = "This output is not available right now. Try Repair."

# --- Stylesheets ---
MODERN_STYLESHEET = """
QMainWindow, QWidget#CentralWidget {
    background-color: #121214;
    color: #e4e4e7;
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
}

/* Master Outputs Panel */
QFrame#MasterBar {
    background-color: #18181b;
    border: 1px solid #27272a;
    border-radius: 10px;
}

/* Search Bar */
QLineEdit#SearchBar {
    background-color: #1a1a1e;
    color: #f4f4f5;
    border: 1px solid #27272a;
    border-radius: 8px;
    padding: 8px 14px;
    font-size: 13px;
    selection-background-color: #3f3f46;
}
QLineEdit#SearchBar:focus {
    border: 1px solid #52525b;
    background-color: #202024;
}

/* Top Bar Buttons */
QPushButton.HeaderBtn {
    background-color: #1e1e22;
    color: #a1a1aa;
    border: 1px solid #2e2e33;
    border-radius: 7px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 500;
}
QPushButton.HeaderBtn:hover {
    background-color: #27272a;
    color: #f4f4f5;
    border-color: #3f3f46;
}

/* Scrollable Container with clean radius */
QScrollArea#StreamScrollArea {
    background-color: #151518;
    border: 1px solid #232326;
    border-radius: 12px;
}
QWidget#StreamContainer {
    background-color: transparent;
    border: none;
}

/* Stream Card */
QFrame.StreamCard {
    background-color: #1b1b1f;
    border: 1px solid #27272a;
    border-radius: 10px;
}
QFrame.StreamCard:hover {
    border-color: #36363d;
}

/* Application Badge */
QLabel#AppBadge {
    background-color: #27272a;
    color: #a1a1aa;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 11px;
    font-weight: 600;
}

/* Segmented Control for Routing */
QFrame#SegmentedGroup {
    background-color: #101012;
    border: 1px solid #27272a;
    border-radius: 7px;
    padding: 2px;
}
QPushButton.SegmentBtn {
    background-color: transparent;
    color: #8b8b93;
    border: none;
    border-radius: 5px;
    padding: 5px 11px;
    font-size: 12px;
    font-weight: 500;
}
QPushButton.SegmentBtn:hover {
    color: #d4d4d8;
}
QPushButton.SegmentBtn:checked {
    background-color: #27272a;
    color: #ffffff;
    font-weight: 600;
}

/* Volume Slider */
QSlider::groove:horizontal {
    height: 4px;
    background: #27272a;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #6366f1;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #e4e4e7;
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}
QSlider::handle:horizontal:hover {
    background: #ffffff;
}

/* Mute Button */
QPushButton.MuteBtn {
    background-color: #1e1e22;
    color: #a1a1aa;
    border: 1px solid #27272a;
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 11px;
}
QPushButton.MuteBtn:hover {
    background-color: #27272a;
    color: #f4f4f5;
}
QPushButton.MuteBtn:checked {
    background-color: #3b1818;
    color: #f87171;
    border-color: #7f1d1d;
    font-weight: 600;
}

QScrollBar:vertical {
    border: none;
    background: transparent;
    width: 6px;
    margin: 4px 0;
}
QScrollBar::handle:vertical {
    background: #27272a;
    min-height: 25px;
    border-radius: 3px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

QStatusBar {
    color: #71717a;
    font-size: 11px;
    border-top: 1px solid #1c1c20;
    background-color: #121214;
}
"""

TRAY_MENU_STYLESHEET = """
QMenu {
    background-color: #18181b;
    color: #e4e4e7;
    border: 1px solid #27272a;
    border-radius: 6px;
    padding: 4px;
}
QMenu::item {
    padding: 6px 16px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: #27272a;
    color: #ffffff;
}
"""


# --- Background Work Helpers ---
class _TaskSignals(QObject):
    """Carries a finished task's callback + payload back to the GUI thread."""

    done = pyqtSignal(object, object)


class _Task(QRunnable):
    """Runs a blocking callable on a pool thread and reports back through a signal."""

    def __init__(
        self,
        fn: Callable[[], Any],
        on_done: Optional[Callable[[Any], None]],
        on_error: Optional[Callable[[Exception], None]],
        signals: _TaskSignals,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._fn = fn
        self._on_done = on_done
        self._on_error = on_error
        self._signals = signals

    def run(self) -> None:
        callback: Optional[Callable[[Any], None]]
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            log.exception("Background task failed")
            callback, payload = self._on_error, exc
        else:
            callback, payload = self._on_done, result

        if callback is None:
            return
        try:
            self._signals.done.emit(callback, payload)
        except RuntimeError:
            pass  # GUI side already destroyed during shutdown


class CoalescedSender(QObject):
    """Forwards only the latest value of a fast-changing control (e.g. a slider) every ~60ms."""

    INTERVAL_MS = 60

    def __init__(self, gui: "AppWindow", send: Callable[[int], None], parent: QObject) -> None:
        super().__init__(parent)
        self._gui = gui
        self._send = send
        self._pending: Optional[int] = None
        self._inflight: int = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._flush)

    def push(self, value: int) -> None:
        self._pending = value
        if not self._timer.isActive():
            self._timer.start()

    def busy(self) -> bool:
        """True while a local change has not yet been delivered (snapshots must not overwrite it)."""
        return self._timer.isActive() or self._inflight > 0

    def _flush(self) -> None:
        value, self._pending = self._pending, None
        if value is None:
            return
        self._inflight += 1
        self._gui.run_async(
            lambda: self._send(value),
            on_done=self._finished,
            on_error=lambda _exc: self._finished(None),
        )

    def _finished(self, _result: Any) -> None:
        self._inflight = max(0, self._inflight - 1)


# --- Custom Widgets ---
class MasterSinkControl(QWidget):
    """Quick volume/mute control for an entire Sink (Headphones or Speakers)."""

    def __init__(self, sink_name: str, label_text: str, gui: "AppWindow") -> None:
        super().__init__()
        self.sink_name = sink_name
        self.gui = gui

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.lbl = QLabel(label_text)
        self.lbl.setStyleSheet("font-size: 12px; font-weight: 600; color: #d4d4d8;")
        self.lbl.setFixedWidth(92)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, MAX_SLIDER_VOLUME)
        self.slider.setValue(100)

        self.val_lbl = QLabel("100%")
        self.val_lbl.setFixedWidth(36)
        self.val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.val_lbl.setStyleSheet("color: #a1a1aa; font-size: 11px; font-family: monospace;")

        self.mute_btn = QPushButton("Mute")
        self.mute_btn.setProperty("class", "MuteBtn")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setFixedWidth(50)
        self.mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self._vol_sender = CoalescedSender(
            gui, lambda v: gui.engine.set_sink_volume(self.sink_name, v), self
        )
        self.slider.valueChanged.connect(self.on_vol)
        self.mute_btn.toggled.connect(self.on_mute)

        layout.addWidget(self.lbl)
        layout.addWidget(self.slider, stretch=1)
        layout.addWidget(self.val_lbl)
        layout.addWidget(self.mute_btn)

    def on_vol(self, val: int) -> None:
        self.val_lbl.setText(f"{val}%")
        self._vol_sender.push(val)

    def on_mute(self, muted: bool) -> None:
        self.mute_btn.setText("Muted" if muted else "Mute")
        self.gui.run_async(lambda: self.gui.engine.set_sink_mute(self.sink_name, muted))

    def update_state(self, sink_data: Optional[Dict[str, Any]]) -> None:
        self.setEnabled(bool(sink_data))
        if not sink_data:
            return
        if not self.slider.isSliderDown() and not self._vol_sender.busy():
            self.slider.blockSignals(True)
            self.slider.setValue(min(int(sink_data["volume"]), MAX_SLIDER_VOLUME))
            self.val_lbl.setText(f"{sink_data['volume']}%")
            self.slider.blockSignals(False)

        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(sink_data["muted"])
        self.mute_btn.setText("Muted" if sink_data["muted"] else "Mute")
        self.mute_btn.blockSignals(False)


class StreamRow(QFrame):
    """Represents an individual application audio stream with routing and volume controls."""

    def __init__(self, stream_data: Dict[str, Any], parent_gui: "AppWindow") -> None:
        super().__init__()
        self.stream_id: str = stream_data["id"]
        self.gui = parent_gui
        self.setProperty("class", "StreamCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        # Top row: Metadata + Routing Buttons
        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        info_box = QVBoxLayout()
        info_box.setSpacing(4)

        self.title_lbl = QLabel()
        self.title_lbl.setStyleSheet("font-size: 13px; font-weight: 600; color: #f4f4f5;")
        self.title_lbl.setWordWrap(True)

        meta_box = QHBoxLayout()
        meta_box.setSpacing(6)
        self.app_badge = QLabel()
        self.app_badge.setObjectName("AppBadge")

        self.pid_lbl = QLabel()
        self.pid_lbl.setStyleSheet("color: #71717a; font-size: 11px; font-family: monospace;")

        meta_box.addWidget(self.app_badge)
        meta_box.addWidget(self.pid_lbl)
        meta_box.addStretch()

        info_box.addWidget(self.title_lbl)
        info_box.addLayout(meta_box)
        top_row.addLayout(info_box, stretch=1)

        seg_frame = QFrame()
        seg_frame.setObjectName("SegmentedGroup")
        seg_layout = QHBoxLayout(seg_frame)
        seg_layout.setContentsMargins(0, 0, 0, 0)
        seg_layout.setSpacing(1)

        self.btn_group = QButtonGroup(self)
        self.btn_group.setExclusive(True)

        self.btn_hp = QPushButton("Headphones")
        self.btn_spk = QPushButton("Speakers")
        self.btn_both = QPushButton("Both")

        for b in (self.btn_hp, self.btn_spk, self.btn_both):
            b.setProperty("class", "SegmentBtn")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_group.addButton(b)
            seg_layout.addWidget(b)

        top_row.addWidget(seg_frame, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.btn_hp.clicked.connect(lambda: self.manual_route("Headphones"))
        self.btn_spk.clicked.connect(lambda: self.manual_route("Speakers"))
        self.btn_both.clicked.connect(lambda: self.manual_route("Both"))

        layout.addLayout(top_row)

        # Bottom row: Stream Volume Slider + Mute Button
        bot_row = QHBoxLayout()
        bot_row.setSpacing(10)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, MAX_SLIDER_VOLUME)
        self.slider.setValue(min(int(stream_data["volume"]), MAX_SLIDER_VOLUME))

        self.vol_lbl = QLabel(f"{stream_data['volume']}%")
        self.vol_lbl.setFixedWidth(40)
        self.vol_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.vol_lbl.setStyleSheet("color: #a1a1aa; font-size: 11px; font-family: monospace;")

        self.mute_btn = QPushButton("Mute")
        self.mute_btn.setProperty("class", "MuteBtn")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setChecked(stream_data["muted"])
        self.mute_btn.setFixedWidth(54)
        self.mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self._vol_sender = CoalescedSender(
            self.gui, lambda v: self.gui.engine.set_stream_volume(self.stream_id, v), self
        )
        self.slider.valueChanged.connect(self.on_vol_change)
        self.mute_btn.toggled.connect(self.on_mute_toggle)

        bot_row.addWidget(self.slider)
        bot_row.addWidget(self.vol_lbl)
        bot_row.addWidget(self.mute_btn)
        layout.addLayout(bot_row)

        self.update_data(stream_data)

    def manual_route(self, target: str) -> None:
        """Moves only this stream, live; it then stays there across jack events until it closes."""
        stream_id = self.stream_id
        self.gui.run_async(
            lambda: self.gui.engine.choose_output(stream_id, target),
            on_done=lambda _r: self.gui.schedule_refresh(),
        )

    def on_vol_change(self, val: int) -> None:
        self.vol_lbl.setText(f"{val}%")
        self._vol_sender.push(val)

    def on_mute_toggle(self, is_muted: bool) -> None:
        self.mute_btn.setText("Muted" if is_muted else "Mute")
        stream_id = self.stream_id
        self.gui.run_async(lambda: self.gui.engine.set_stream_mute(stream_id, is_muted))

    def set_available_sinks(self, available: Set[str]) -> None:
        """A missing output can't receive the stream: disable its button instead of letting the click do nothing."""
        for sink, button in (("Headphones", self.btn_hp), ("Speakers", self.btn_spk), ("Both", self.btn_both)):
            button.setEnabled(sink in available)
            button.setToolTip("" if sink in available else MISSING_OUTPUT_TOOLTIP)

    def update_data(self, data: Dict[str, Any]) -> None:
        app = data.get("app") or data.get("binary") or "Application"
        m_title = data.get("media_title")
        w_title = data.get("window_name")
        m_name = data.get("media_name")

        if m_title and m_title not in ("Playback", "AudioStream"):
            display = m_title
        elif w_title:
            display = w_title
        elif m_name and m_name not in ("Playback", "AudioStream", "output"):
            display = m_name
        else:
            display = f"{app} Output"

        self.title_lbl.setText(display)
        self.app_badge.setText(app.upper())
        self.pid_lbl.setText(f"PID {data['pid']}" if data.get("pid") else "")

        current_sink = data.get("sink_name", "").lower()
        for b in (self.btn_hp, self.btn_spk, self.btn_both):
            b.blockSignals(True)

        # Exclusive groups refuse to uncheck the last button; lift exclusivity so an
        # external sink (Bluetooth/USB) shows no segment selected.
        self.btn_group.setExclusive(False)
        self.btn_hp.setChecked(current_sink == "headphones")
        self.btn_spk.setChecked(current_sink == "speakers")
        self.btn_both.setChecked(current_sink == "both")
        self.btn_group.setExclusive(True)

        for b in (self.btn_hp, self.btn_spk, self.btn_both):
            b.blockSignals(False)

        if not self.slider.isSliderDown() and not self._vol_sender.busy():
            self.slider.blockSignals(True)
            self.slider.setValue(min(int(data["volume"]), MAX_SLIDER_VOLUME))
            self.vol_lbl.setText(f"{data['volume']}%")
            self.slider.blockSignals(False)

        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(data["muted"])
        self.mute_btn.setText("Muted" if data["muted"] else "Mute")
        self.mute_btn.blockSignals(False)



# --- Main Application Window ---
class AppWindow(QMainWindow):
    REFRESH_DEBOUNCE_MS = 150
    RECOVERY_RETRY_MS = 4000
    SLOW_REFRESH_MS = 5000
    SHUTDOWN_WAIT_MS = 3000

    def __init__(self, start_in_tray: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("PipeWire Audio Router")
        self.resize(620, 680)
        self.setStyleSheet(MODERN_STYLESHEET)

        self.engine = AudioEngine()

        self.stream_rows: Dict[str, StreamRow] = {}
        self._force_quit: bool = False
        self._closing: bool = False
        self._sinks_ok: bool = True
        self._available_sinks: Set[str] = set(INTERNAL_SINK_NAMES.values())
        self.tray_force_actions: Dict[str, QAction] = {}
        self._started: bool = False
        self._external_count: int = 0
        self._flash_until: float = 0.0
        self._refresh_inflight: bool = False
        self._refresh_again: bool = False

        # Serial pool for commands (keeps volume/mute/route ordering); separate pool for snapshots
        self._cmd_pool = QThreadPool(self)
        self._cmd_pool.setMaxThreadCount(1)
        self._refresh_pool = QThreadPool(self)
        self._refresh_pool.setMaxThreadCount(1)
        self._signals = _TaskSignals(self)
        self._signals.done.connect(self._dispatch)

        self.jack_monitor: Optional[JackMonitorThread] = None
        self.pulse_monitor: Optional[PulseEventThread] = None

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(self.REFRESH_DEBOUNCE_MS)
        self._refresh_timer.timeout.connect(self.request_refresh)

        self._recovery_timer = QTimer(self)
        self._recovery_timer.setSingleShot(True)
        self._recovery_timer.setInterval(self.RECOVERY_RETRY_MS)
        self._recovery_timer.timeout.connect(self.request_refresh)

        # Slow tick (MPRIS titles have no event source); only runs while the window is visible
        self._slow_timer = QTimer(self)
        self._slow_timer.setInterval(self.SLOW_REFRESH_MS)
        self._slow_timer.timeout.connect(self.request_refresh)

        self.init_ui()
        self.init_tray()

        self.status.showMessage("Starting audio engine...")
        self.run_async(self._startup_work, on_done=self._on_startup_done, on_error=self._on_startup_failed)

        if not start_in_tray:
            self.show()
        elif not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("No system tray available: showing the window instead of starting hidden")
            self.show()

    # --- Background Work ---
    def run_async(
        self,
        fn: Callable[[], Any],
        on_done: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        refresh_pool: bool = False,
    ) -> None:
        """Runs fn on a worker thread; on_done(result) / on_error(exc) run later on the GUI thread."""
        if self._closing:
            return
        pool = self._refresh_pool if refresh_pool else self._cmd_pool
        pool.start(_Task(fn, on_done, on_error, self._signals))

    def _dispatch(self, callback: Callable[[Any], None], payload: Any) -> None:
        if self._closing:
            return
        try:
            callback(payload)
        except RuntimeError:
            log.debug("Result arrived for a deleted widget; ignored", exc_info=True)
        except Exception:
            log.exception("Error in background-task callback")

    def notify(self, title: str, message: str, urgency: str = "normal") -> None:
        self.run_async(lambda: send_notification(title, message, urgency=urgency))

    # --- Startup ---
    def _startup_work(self) -> Dict[str, bool]:
        self.engine.ensure_hardware_ready()
        sinks_ok = bool(self.engine.ensure_sinks_exist())
        return {"sinks_ok": sinks_ok, "plugged": bool(self.engine.is_headphone_plugged())}

    def _on_startup_done(self, result: Dict[str, bool]) -> None:
        self._started = True
        self._sinks_ok = result["sinks_ok"]
        if not self._sinks_ok:
            log.warning("Virtual sinks (Headphones/Speakers/Both) are not available at startup")
        self.update_tray_jack_status(result["plugged"])
        self._start_monitors()
        self.request_refresh()

    def _on_startup_failed(self, exc: Exception) -> None:
        self._started = True
        self._sinks_ok = False
        self.flash(f"Audio engine startup failed: {exc}", 5000)
        self._start_monitors()
        self.request_refresh()

    def _start_monitors(self) -> None:
        if self.jack_monitor is None:
            self.jack_monitor = JackMonitorThread(self.engine, self)
            self.jack_monitor.jack_state_changed.connect(self.on_jack_event)
            self.jack_monitor.hardware_resumed.connect(self.on_hardware_resumed)
            self.jack_monitor.start()
        if self.pulse_monitor is None:
            self.pulse_monitor = PulseEventThread(self)
            self.pulse_monitor.changed.connect(self.schedule_refresh)
            self.pulse_monitor.start()

    def _restore_hardware(self) -> None:
        self.engine.ensure_hardware_ready()
        self.engine.enforce_hardware_links()

    def on_hardware_resumed(self) -> None:
        self.run_async(self._restore_hardware)

    # --- UI construction ---
    def init_ui(self) -> None:
        central = QWidget()
        central.setObjectName("CentralWidget")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.setSpacing(12)
        self.setCentralWidget(central)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(1)

        title = QLabel("Audio Router")
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #f4f4f5;")
        subtitle = QLabel("Independent jack & speaker channel control")
        subtitle.setStyleSheet("font-size: 11px; color: #71717a;")

        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, stretch=1)

        self.fix_btn = QPushButton("Repair")
        self.fix_btn.setProperty("class", "HeaderBtn")
        self.fix_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fix_btn.clicked.connect(self.on_manual_fix)

        header.addWidget(self.fix_btn)
        layout.addLayout(header)

        # Master Outputs Volume Panel
        master_frame = QFrame()
        master_frame.setObjectName("MasterBar")
        master_layout = QVBoxLayout(master_frame)
        master_layout.setContentsMargins(14, 10, 14, 10)
        master_layout.setSpacing(8)

        self.master_hp = MasterSinkControl("Headphones", "Headphones", self)
        self.master_spk = MasterSinkControl("Speakers", "Speakers", self)
        self.master_hw = MasterSinkControl(self.engine.target_node, "Hardware", self)
        self.master_hw.setToolTip("Level of the pro-audio output feeding both Headphones and Speakers")

        master_layout.addWidget(self.master_hp)
        master_layout.addWidget(self.master_spk)
        master_layout.addWidget(self.master_hw)
        layout.addWidget(master_frame)

        # Search Bar
        self.search = QLineEdit()
        self.search.setObjectName("SearchBar")
        self.search.setPlaceholderText("Filter streams or applications...")
        self.search.textChanged.connect(self.filter_streams)
        layout.addWidget(self.search)

        # Stream List Scroll Container
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("StreamScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        viewport = self.scroll_area.viewport()
        if viewport is not None:
            viewport.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.container = QWidget()
        self.container.setObjectName("StreamContainer")
        self.rows_layout = QVBoxLayout(self.container)
        self.rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.rows_layout.setContentsMargins(10, 10, 10, 10)
        self.rows_layout.setSpacing(8)
        self.scroll_area.setWidget(self.container)
        layout.addWidget(self.scroll_area)

        self.status = QStatusBar()
        self.setStatusBar(self.status)

    def init_tray(self) -> None:
        app_icon = QIcon(str(ICON_PATH))
        self.setWindowIcon(app_icon)

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(app_icon)

        self.tray_menu = QMenu()
        self.tray_menu.setStyleSheet(TRAY_MENU_STYLESHEET)

        self.tray_jack_action = QAction("Checking jack...", self)
        self.tray_jack_action.setEnabled(False)
        self.tray_menu.addAction(self.tray_jack_action)
        self.tray_menu.addSeparator()

        act_show = QAction("Toggle Window", self)
        act_show.triggered.connect(self.toggle_window)
        self.tray_menu.addAction(act_show)

        for label, target in (
            ("Force All to Headphones", "Headphones"),
            ("Force All to Speakers", "Speakers"),
            ("Force All to Both", "Both"),
        ):
            action = QAction(label, self)
            action.triggered.connect(lambda checked=False, t=target: self.force_all_streams(t))
            self.tray_menu.addAction(action)
            self.tray_force_actions[target] = action

        self.tray_menu.addSeparator()
        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(self.quit_application)
        self.tray_menu.addAction(act_quit)

        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    # --- Tray & Window Actions ---
    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_window()

    def toggle_window(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show_and_raise()

    def show_and_raise(self) -> None:
        """Brings the window to the front (also used for single-instance activation)."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._closing:
            self._slow_timer.start()
            self.schedule_refresh()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._slow_timer.stop()

    def update_tray_jack_status(self, plugged: bool) -> None:
        state_txt = "Jack Connected" if plugged else "Jack Unplugged"
        self.tray_jack_action.setText(state_txt)
        self.tray.setToolTip(f"PipeWire Audio Router ({state_txt})")

    # --- Routing & Hardware Events ---
    def force_all_streams(self, target_sink: str) -> None:
        """Explicit user command (tray): moves everything, including streams moved by hand."""
        self.flash(f"Routing all streams to {target_sink}...", 10000)

        def done(moved: Any) -> None:
            self.flash(f"{moved} stream(s) routed to {target_sink}", 2500)
            self.notify("Audio Routed", f"{moved} active stream(s) forced to {target_sink}", urgency="low")
            self.schedule_refresh()

        self.run_async(
            lambda: self.engine.force_all_streams(target_sink),
            on_done=done,
            on_error=lambda exc: self.flash(f"Routing to {target_sink} failed: {exc}", 4000),
        )

    def on_jack_event(self, plugged: bool) -> None:
        """One rule, no questions: audio follows the jack, except streams moved by hand."""
        self.update_tray_jack_status(plugged)
        target = "Headphones" if plugged else "Speakers"
        title = "Jack Connected" if plugged else "Jack Unplugged"

        def done(moved: Any) -> None:
            self.flash(f"{title}: switched {moved} stream(s) to {target}", 3500)
            self.notify(title, f"Switched {moved} stream(s) to {target}", urgency="normal")
            self.schedule_refresh()

        self.run_async(
            lambda: self.engine.follow_jack(plugged),
            on_done=done,
            on_error=lambda exc: self.flash(f"{title}: routing failed: {exc}", 4000),
        )

    def on_manual_fix(self) -> None:
        self.fix_btn.setEnabled(False)
        self.flash("Repairing audio pipeline...", 60000)

        def work() -> bool:
            ok = bool(self.engine.ensure_sinks_exist(force_reload=True))
            if ok:
                self.engine.ensure_hardware_ready()
            return ok

        def done(ok: bool) -> None:
            self.fix_btn.setEnabled(True)
            self._sinks_ok = ok
            if ok:
                self.flash("Audio pipeline reloaded", 2500)
            else:
                log.warning("Repair finished but virtual sinks are still not available")
                self.flash("Repair failed: virtual sinks not available", 5000)
            self.schedule_refresh()

        def failed(exc: Exception) -> None:
            self.fix_btn.setEnabled(True)
            self._sinks_ok = False
            self.flash(f"Repair failed: {exc}", 5000)

        self.run_async(work, on_done=done, on_error=failed)

    # --- Stream List Management ---
    def filter_streams(self) -> None:
        query = self.search.text().lower()
        for r in self.stream_rows.values():
            match = query in r.title_lbl.text().lower() or query in r.app_badge.text().lower()
            r.setVisible(match)

    def schedule_refresh(self) -> None:
        """Debounced refresh request (safe to call as often as events arrive)."""
        if self._closing or self._refresh_timer.isActive():
            return
        self._refresh_timer.start()

    def request_refresh(self) -> None:
        """Starts a background snapshot; at most one in flight, one more queued if requested meanwhile."""
        if self._closing or not self._started:
            return
        if self._refresh_inflight:
            self._refresh_again = True
            return
        self._refresh_inflight = True
        self._refresh_again = False
        self.run_async(
            self._collect_snapshot,
            on_done=self._on_snapshot,
            on_error=lambda _exc: self._refresh_finished(),
            refresh_pool=True,
        )

    def _refresh_finished(self) -> None:
        self._refresh_inflight = False
        if self._refresh_again:
            self.request_refresh()

    def _collect_snapshot(self) -> Dict[str, Any]:
        """Runs on a worker thread: all subprocess work happens here."""
        sinks_ok = bool(self.engine.recover_missing_sinks())
        sink_info = self.engine.get_sink_info()
        streams = self.engine.get_streams()
        external = self.engine.get_external_sinks()
        return {"sinks": sink_info, "streams": streams, "external": external, "sinks_ok": sinks_ok}

    def _on_snapshot(self, snap: Dict[str, Any]) -> None:
        try:
            self._apply_snapshot(snap)
        finally:
            self._refresh_finished()

    def _apply_snapshot(self, snap: Dict[str, Any]) -> None:
        self._sinks_ok = snap["sinks_ok"]
        if not self._sinks_ok and not self.engine.recovery_exhausted and not self._closing:
            self._recovery_timer.start()  # sink loss may produce no further PipeWire events

        sink_info = snap["sinks"]
        self.master_hp.update_state(sink_info.get("Headphones"))
        self.master_spk.update_state(sink_info.get("Speakers"))
        self.master_hw.update_state(sink_info.get(self.engine.target_node))

        streams = snap["streams"]
        active_ids = {s["id"] for s in streams}

        for sid in list(self.stream_rows.keys()):
            if sid not in active_ids:
                row = self.stream_rows.pop(sid)
                self.rows_layout.removeWidget(row)
                row.hide()
                row.deleteLater()

        for s in streams:
            sid = s["id"]
            if sid in self.stream_rows:
                self.stream_rows[sid].update_data(s)
            else:
                row = StreamRow(s, self)
                self.rows_layout.addWidget(row)
                self.stream_rows[sid] = row

        self._available_sinks = set(INTERNAL_SINK_NAMES.values()) & set(sink_info)
        for row in self.stream_rows.values():
            row.set_available_sinks(self._available_sinks)
        for target, action in self.tray_force_actions.items():
            action.setEnabled(target in self._available_sinks)

        self._external_count = len(snap["external"])
        self.filter_streams()
        self.update_status()

    def flash(self, text: str, timeout_ms: int = 3000) -> None:
        """Shows a transient status message that the periodic stream summary must not overwrite."""
        self._flash_until = time.monotonic() + timeout_ms / 1000
        self.status.showMessage(text, timeout_ms)
        QTimer.singleShot(timeout_ms + 50, self.update_status)

    def update_status(self) -> None:
        if self._closing or time.monotonic() < self._flash_until:
            return
        active_cnt = len(self.stream_rows)
        ext_cnt = self._external_count
        ext_txt = f" • {ext_cnt} external USB/BT" if ext_cnt else ""
        warn_txt = "" if self._sinks_ok else "Virtual sinks unavailable (try Repair) • "

        if active_cnt:
            plural = "s" if active_cnt > 1 else ""
            self.status.showMessage(f"{warn_txt}{active_cnt} active stream{plural}{ext_txt}")
        else:
            self.status.showMessage(f"{warn_txt}No audio playing{ext_txt}")

    # --- Lifecycle & Shutdown ---
    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._force_quit and QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
        else:
            self.shutdown()
            event.accept()
            QApplication.quit()

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._refresh_timer.stop()
        self._recovery_timer.stop()
        self._slow_timer.stop()
        if self.pulse_monitor is not None:
            self.pulse_monitor.stop()
        if self.jack_monitor is not None:
            self.jack_monitor.stop()
        for pool in (self._cmd_pool, self._refresh_pool):
            pool.clear()
            if not pool.waitForDone(self.SHUTDOWN_WAIT_MS):
                log.warning("Background task still running at shutdown; exiting anyway")
        self.tray.hide()

    def quit_application(self) -> None:
        self._force_quit = True
        self.shutdown()
        QApplication.quit()
