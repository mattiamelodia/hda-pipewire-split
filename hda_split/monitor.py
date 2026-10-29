import shutil
import subprocess
from typing import TYPE_CHECKING, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

if TYPE_CHECKING:
    from .engine import AudioEngine


class JackMonitorThread(QThread):
    """Background thread that monitors ALSA jack plug/unplug events and suspend/resume cycles."""

    jack_state_changed = pyqtSignal(bool)
    hardware_resumed = pyqtSignal()

    def __init__(self, engine: "AudioEngine", parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._running: bool = True
        self._proc: Optional[subprocess.Popen[str]] = None

    def _check_and_emit(self, last_state: bool) -> bool:
        """Checks current headphone jack state and emits signal if changed."""
        new_state = self.engine.is_headphone_plugged()
        if new_state != last_state:
            self.jack_state_changed.emit(new_state)
        return new_state

    def run(self) -> None:
        last_state = self.engine.is_headphone_plugged()
        alsactl_bin = shutil.which("alsactl")

        while self._running:
            if alsactl_bin:
                cmd = ["alsactl", "monitor", str(self.engine.card_id)]
                if shutil.which("stdbuf"):
                    cmd = ["stdbuf", "-oL"] + cmd

                try:
                    self._proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        bufsize=1,
                    )
                    if self._proc.stdout is not None:
                        for _line in self._proc.stdout:
                            if not self._running:
                                break
                            last_state = self._check_and_emit(last_state)
                except Exception:
                    pass

                # If alsactl monitor terminates (e.g., after kernel suspend/resume),
                # restores the ALSA state and restarts the monitor after 1 second
                if self._running:
                    self.msleep(1000)
                    self.hardware_resumed.emit()
                    last_state = self._check_and_emit(last_state)
            else:
                self.msleep(1500)
                last_state = self._check_and_emit(last_state)

    def stop(self) -> None:
        self._running = False
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=0.8)
            except Exception:
                self._proc.kill()
        self.wait(1000)