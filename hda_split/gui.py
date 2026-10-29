import subprocess
from typing import Any, Dict, Optional, Set

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QCloseEvent, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QStatusBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .config import ICON_PATH, notify_jack_plugged, send_notification
from .engine import AudioEngine
from .monitor import JackMonitorThread

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

/* Pin Button */
QPushButton#PinBtn {
    background-color: #1e1e22;
    border: 1px solid #27272a;
    border-radius: 7px;
    color: #71717a;
    font-size: 13px;
    padding: 4px;
}
QPushButton#PinBtn:hover {
    background-color: #27272a;
    color: #a1a1aa;
}
QPushButton#PinBtn:checked {
    background-color: #1e3a5f;
    border: 1px solid #2563eb;
    color: #60a5fa;
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


# --- Custom Widgets ---
class MasterSinkControl(QWidget):
    """Quick volume/mute control for an entire Sink (Headphones or Speakers)."""

    def __init__(self, sink_name: str, label_text: str, engine: AudioEngine) -> None:
        super().__init__()
        self.sink_name = sink_name
        self.engine = engine

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.lbl = QLabel(label_text)
        self.lbl.setStyleSheet("font-size: 12px; font-weight: 600; color: #d4d4d8;")
        self.lbl.setFixedWidth(92)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 150)
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

        self.slider.valueChanged.connect(self.on_vol)
        self.mute_btn.toggled.connect(self.on_mute)

        layout.addWidget(self.lbl)
        layout.addWidget(self.slider, stretch=1)
        layout.addWidget(self.val_lbl)
        layout.addWidget(self.mute_btn)

    def on_vol(self, val: int) -> None:
        self.val_lbl.setText(f"{val}%")
        self.engine.set_sink_volume(self.sink_name, val)

    def on_mute(self, muted: bool) -> None:
        self.mute_btn.setText("Muted" if muted else "Mute")
        self.engine.set_sink_mute(self.sink_name, muted)

    def update_state(self, sink_data: Optional[Dict[str, Any]]) -> None:
        if not sink_data:
            return
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(sink_data["volume"])
            self.val_lbl.setText(f"{sink_data['volume']}%")
            self.slider.blockSignals(False)

        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(sink_data["muted"])
        self.mute_btn.setText("Muted" if sink_data["muted"] else "Mute")
        self.mute_btn.blockSignals(False)


class StreamRow(QFrame):
    """Represents an individual application audio stream with routing, pinning, and volume controls."""

    def __init__(self, stream_data: Dict[str, Any], parent_gui: "AppWindow", is_pinned: bool = False) -> None:
        super().__init__()
        self.stream_id: str = stream_data["id"]
        self.app_key: str = stream_data.get("app_key", "")
        self.gui = parent_gui
        self.setProperty("class", "StreamCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        # Top row: Metadata + Routing Buttons + Pin Button
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

        self.btn_pin = QPushButton("📌")
        self.btn_pin.setObjectName("PinBtn")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setChecked(is_pinned)
        self.btn_pin.setFixedSize(28, 28)
        self.btn_pin.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pin.setToolTip("Persistent Pin: remembers output for this app across restarts and jack events")
        self.btn_pin.toggled.connect(self.on_pin_toggled)
        top_row.addWidget(self.btn_pin, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.btn_hp.clicked.connect(lambda: self.manual_route("Headphones"))
        self.btn_spk.clicked.connect(lambda: self.manual_route("Speakers"))
        self.btn_both.clicked.connect(lambda: self.manual_route("Both"))

        layout.addLayout(top_row)

        # Bottom row: Stream Volume Slider + Mute Button
        bot_row = QHBoxLayout()
        bot_row.setSpacing(10)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 150)
        self.slider.setValue(stream_data["volume"])

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

        self.slider.valueChanged.connect(self.on_vol_change)
        self.mute_btn.toggled.connect(self.on_mute_toggle)

        bot_row.addWidget(self.slider)
        bot_row.addWidget(self.vol_lbl)
        bot_row.addWidget(self.mute_btn)
        layout.addLayout(bot_row)

        self.update_data(stream_data)

    def current_selected_target(self) -> str:
        if self.btn_spk.isChecked():
            return "Speakers"
        if self.btn_both.isChecked():
            return "Both"
        return "Headphones"

    def on_pin_toggled(self, checked: bool) -> None:
        if checked:
            self.gui.pinned_streams.add(self.stream_id)
            self.gui.engine.pin_app(self.app_key, self.current_selected_target())
        else:
            self.gui.pinned_streams.discard(self.stream_id)
            self.gui.engine.unpin_app(self.app_key)

    def manual_route(self, target: str) -> None:
        self.gui.engine.route_stream(self.stream_id, target)
        self.btn_pin.blockSignals(True)
        self.btn_pin.setChecked(True)
        self.btn_pin.blockSignals(False)
        self.gui.pinned_streams.add(self.stream_id)
        self.gui.engine.pin_app(self.app_key, target)
        self.gui.poll_streams()

    def on_vol_change(self, val: int) -> None:
        self.vol_lbl.setText(f"{val}%")
        self.gui.engine.set_stream_volume(self.stream_id, val)

    def on_mute_toggle(self, is_muted: bool) -> None:
        self.mute_btn.setText("Muted" if is_muted else "Mute")
        self.gui.engine.set_stream_mute(self.stream_id, is_muted)

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

        self.btn_hp.setChecked(current_sink == "headphones")
        self.btn_spk.setChecked(current_sink == "speakers")
        self.btn_both.setChecked(current_sink == "both")

        for b in (self.btn_hp, self.btn_spk, self.btn_both):
            b.blockSignals(False)

        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(data["volume"])
            self.vol_lbl.setText(f"{data['volume']}%")
            self.slider.blockSignals(False)

        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(data["muted"])
        self.mute_btn.setText("Muted" if data["muted"] else "Mute")
        self.mute_btn.blockSignals(False)


# --- Main Application Window ---
class AppWindow(QMainWindow):
    def __init__(self, start_in_tray: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("PipeWire Audio Router")
        self.resize(620, 680)
        self.setStyleSheet(MODERN_STYLESHEET)

        self.engine = AudioEngine()
        self.engine.ensure_hardware_ready()
        self.engine.ensure_sinks_exist()

        self.stream_rows: Dict[str, StreamRow] = {}
        self.pinned_streams: Set[str] = set()
        self._force_quit: bool = False

        self.init_ui()
        self.init_tray()

        self.jack_monitor = JackMonitorThread(self.engine, self)
        self.jack_monitor.jack_state_changed.connect(self.on_jack_event)
        self.jack_monitor.hardware_resumed.connect(self.engine.ensure_hardware_ready)
        self.jack_monitor.start()

        self.timer = QTimer(self)
        self.timer.setInterval(1200)
        self.timer.timeout.connect(self.poll_streams)
        self.timer.start()

        self.poll_streams()
        self.update_tray_jack_status(self.engine.is_headphone_plugged())

        if not start_in_tray:
            self.show()

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

        unpin_btn = QPushButton("Reset Pins")
        unpin_btn.setProperty("class", "HeaderBtn")
        unpin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        unpin_btn.clicked.connect(self.unpin_all_streams)

        fix_btn = QPushButton("Repair")
        fix_btn.setProperty("class", "HeaderBtn")
        fix_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fix_btn.clicked.connect(self.on_manual_fix)

        header.addWidget(unpin_btn)
        header.addWidget(fix_btn)
        layout.addLayout(header)

        # Master Outputs Volume Panel
        master_frame = QFrame()
        master_frame.setObjectName("MasterBar")
        master_layout = QVBoxLayout(master_frame)
        master_layout.setContentsMargins(14, 10, 14, 10)
        master_layout.setSpacing(8)

        self.master_hp = MasterSinkControl("Headphones", "Headphones", self.engine)
        self.master_spk = MasterSinkControl("Speakers", "Speakers", self.engine)

        master_layout.addWidget(self.master_hp)
        master_layout.addWidget(self.master_spk)
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

        menu = QMenu()
        menu.setStyleSheet(TRAY_MENU_STYLESHEET)

        self.tray_jack_action = QAction("Checking jack...", self)
        self.tray_jack_action.setEnabled(False)
        menu.addAction(self.tray_jack_action)
        menu.addSeparator()

        act_show = QAction("Toggle Window", self)
        act_show.triggered.connect(self.toggle_window)
        menu.addAction(act_show)

        for label, target in (
            ("Force All to Headphones", "Headphones"),
            ("Force All to Speakers", "Speakers"),
            ("Force All to Both", "Both"),
        ):
            action = QAction(label, self)
            action.triggered.connect(lambda checked=False, t=target: self.force_all_streams(t))
            menu.addAction(action)

        menu.addSeparator()
        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(self.quit_application)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    # --- Tray & Window Actions ---
    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_window()

    def toggle_window(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.activateWindow()

    def update_tray_jack_status(self, plugged: bool) -> None:
        state_txt = "Jack Connected" if plugged else "Jack Unplugged"
        self.tray_jack_action.setText(state_txt)
        self.tray.setToolTip(f"PipeWire Audio Router ({state_txt})")

    # --- Routing & Hardware Events ---
    def handle_jack_action(self, action: str) -> None:
        mapping = {
            "headphones": "Headphones",
            "speakers": "Speakers",
            "both": "Both",
        }
        target_sink = mapping.get(action)
        if target_sink:
            # 1. Immediately tell PipeWire/WirePlumber which sink should take command
            self.engine.set_default_target(target_sink)
            # 2. Force all existing streams
            self.force_all_streams(target_sink)

    def force_all_streams(self, target_sink: str) -> None:
        # 1. Set global default sink
        self.engine.set_default_target(target_sink)

        # 2. Apply PipeWire links and correct ALSA state for the chosen target
        self.engine.apply_hardware_routing_mode(target_sink)

        # 3. Re-route all active streams
        for sid in list(self.stream_rows.keys()):
            self.engine.route_stream(sid, target_sink)

        self.poll_streams()
        self.status.showMessage(f"All streams routed to {target_sink}", 2500)
        send_notification(
            "Audio Routed",
            f"All active streams forced to {target_sink}",
            urgency="low",
        )

    def on_jack_event(self, plugged: bool) -> None:
        self.update_tray_jack_status(plugged)

        # Immediately re-activate ALSA hardware before routing
        self.engine.ensure_hardware_ready()

        if not plugged:
            target_sink = "Speakers"
            self.engine.set_default_target(target_sink)
            moved_count = 0

            for sid, row in self.stream_rows.items():
                if sid in self.pinned_streams or row.app_key in self.engine.pinned_apps:
                    continue
                self.engine.route_stream(sid, target_sink)
                moved_count += 1

            self.engine.ensure_hardware_ready()
            self.poll_streams()

            send_notification(
                "Jack Unplugged",
                f"Switched {moved_count} stream(s) to {target_sink}",
                urgency="normal",
            )
            self.status.showMessage(f"Jack unplugged: switched {moved_count} stream(s) to {target_sink}", 3500)
        else:
            self.poll_streams()

            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Headphones Plugged In")
            msg_box.setText("Where do you want to route the audio?")
            msg_box.setIcon(QMessageBox.Icon.Question)

            btn_headphones = msg_box.addButton("Headphones Only", QMessageBox.ButtonRole.AcceptRole)
            btn_speakers = msg_box.addButton("Speakers Only", QMessageBox.ButtonRole.RejectRole)
            btn_both = msg_box.addButton("Both (Split)", QMessageBox.ButtonRole.ActionRole)

            msg_box.exec()

            clicked_button = msg_box.clickedButton()
            if clicked_button == btn_headphones:
                self.force_all_streams("Headphones")
            elif clicked_button == btn_speakers:
                self.force_all_streams("Speakers")
            elif clicked_button == btn_both:
                self.force_all_streams("Both")

            self.status.showMessage("Headphones connected: routing updated", 3500)

    def unpin_all_streams(self) -> None:
        self.pinned_streams.clear()
        self.engine.clear_all_pins()
        for row in self.stream_rows.values():
            row.btn_pin.blockSignals(True)
            row.btn_pin.setChecked(False)
            row.btn_pin.blockSignals(False)
        self.status.showMessage("Persistent pins cleared: automatic jack switching active", 2500)

    def on_manual_fix(self) -> None:
        self.engine.ensure_sinks_exist(force_reload=True)
        self.engine.ensure_hardware_ready()
        self.poll_streams()
        self.status.showMessage("Audio pipeline reloaded", 2500)

    # --- Stream List Management ---
    def filter_streams(self) -> None:
        query = self.search.text().lower()
        for r in self.stream_rows.values():
            match = query in r.title_lbl.text().lower() or query in r.app_badge.text().lower()
            r.setVisible(match)

    def poll_streams(self) -> None:
        # 1. Update Master Sink volumes
        sink_info = self.engine.get_sink_info()
        self.master_hp.update_state(sink_info.get("Headphones"))
        self.master_spk.update_state(sink_info.get("Speakers"))

        # 2. Update application streams and apply any saved Pins from disk
        streams = self.engine.get_streams()
        active_ids = {s["id"] for s in streams}

        for sid in list(self.stream_rows.keys()):
            if sid not in active_ids:
                self.stream_rows[sid].deleteLater()
                del self.stream_rows[sid]
                self.pinned_streams.discard(sid)

        for s in streams:
            sid = s["id"]
            app_key = s.get("app_key", "")
            saved_target = self.engine.pinned_apps.get(app_key)

            if sid not in self.stream_rows:
                is_pinned = (sid in self.pinned_streams) or (saved_target is not None)
                if saved_target and s.get("sink_name", "").lower() != saved_target.lower():
                    self.engine.route_stream(sid, saved_target)
                    s["sink_name"] = saved_target
                if is_pinned:
                    self.pinned_streams.add(sid)

                row = StreamRow(s, self, is_pinned=is_pinned)
                self.rows_layout.addWidget(row)
                self.stream_rows[sid] = row
            else:
                self.stream_rows[sid].update_data(s)

        self.filter_streams()
        active_cnt = len(self.stream_rows)
        pinned_cnt = len(self.pinned_streams)
        ext_sinks = self.engine.get_external_sinks()
        ext_txt = f" • {len(ext_sinks)} external USB/BT" if ext_sinks else ""

        if active_cnt:
            plural = "s" if active_cnt > 1 else ""
            self.status.showMessage(f"{active_cnt} active stream{plural} • {pinned_cnt} pinned{ext_txt}")
        else:
            self.status.showMessage(f"No audio playing{ext_txt}")

    # --- Lifecycle & Shutdown ---
    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._force_quit and self.tray.isSystemTrayAvailable():
            event.ignore()
            self.hide()
        else:
            self.shutdown()
            event.accept()

    def shutdown(self) -> None:
        self.timer.stop()
        self.jack_monitor.stop()
        self.engine.cleanup()

    def quit_application(self) -> None:
        self._force_quit = True
        self.shutdown()
        QApplication.quit()