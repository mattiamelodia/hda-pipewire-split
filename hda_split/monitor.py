import logging
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, List, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .config import command_env

if TYPE_CHECKING:
    from .engine import AudioEngine

log = logging.getLogger("hda_split")


class _ProcessMonitorThread(QThread):
    """Runs a long-lived command, feeds its lines to a handler, and restarts it with exponential backoff."""

    BACKOFF_MIN = 1.0
    BACKOFF_MAX = 30.0
    STABLE_SECS = 10.0

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._running: bool = True
        self._proc: Optional[subprocess.Popen[str]] = None
        self._delay: float = self.BACKOFF_MIN

    # --- Hooks for subclasses ---
    def _command(self) -> Optional[List[str]]:
        """Command to run, or None if it is unavailable (then _idle is called instead)."""
        raise NotImplementedError

    def _on_line(self, line: str) -> None:
        raise NotImplementedError

    def _on_stable_exit(self) -> None:
        """Called after a command that ran stably has exited and the restart delay elapsed."""

    def _idle(self) -> None:
        self._sleep(5.0)

    # --- Loop ---
    def _sleep(self, seconds: float) -> None:
        """Sleeps in short steps so stop() takes effect quickly."""
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            self.msleep(100)

    def _run_once(self, cmd: List[str]) -> None:
        if shutil.which("stdbuf"):
            cmd = ["stdbuf", "-oL"] + cmd
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=command_env(),
            )
            if not self._running:
                self._proc.terminate()
            if self._proc.stdout is not None:
                for line in self._proc.stdout:
                    if not self._running:
                        break
                    self._on_line(line)
        except Exception as e:
            log.warning("%s failed: %s", " ".join(cmd), e)
        finally:
            proc = self._proc
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()

    def _loop(self) -> None:
        while self._running:
            cmd = self._command()
            if cmd is None:
                self._idle()
                continue

            started = time.monotonic()
            self._run_once(cmd)
            if not self._running:
                break

            # Exited on its own: back off exponentially unless it had been running stably.
            stable = time.monotonic() - started > self.STABLE_SECS
            if stable:
                self._delay = self.BACKOFF_MIN
            self._sleep(self._delay)
            if not stable:
                self._delay = min(self._delay * 2, self.BACKOFF_MAX)
            elif self._running:
                self._on_stable_exit()

    def stop(self) -> None:
        self._running = False
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.8)
            except Exception:
                proc.kill()
        self.wait(1000)


class JackMonitorThread(_ProcessMonitorThread):
    """Background thread that monitors ALSA jack plug/unplug events and suspend/resume cycles."""

    jack_state_changed = pyqtSignal(bool)
    hardware_resumed = pyqtSignal()

    def __init__(self, engine: "AudioEngine", parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._last_state: bool = False

    def _check_and_emit(self) -> None:
        """Checks current headphone jack state and emits signal if changed."""
        new_state = self.engine.is_headphone_plugged()
        if new_state != self._last_state:
            self.jack_state_changed.emit(new_state)
        self._last_state = new_state

    def _command(self) -> Optional[List[str]]:
        if not shutil.which("alsactl"):
            return None
        # alsactl only accepts a control device name such as hw:2, not a bare card number
        return ["alsactl", "monitor", f"hw:{self.engine.card_id}"]

    def _on_line(self, line: str) -> None:
        self._check_and_emit()

    def _on_stable_exit(self) -> None:
        # alsactl monitor ended after running for a while (e.g. kernel suspend/resume):
        # let the GUI restore the ALSA state, then re-read the jack.
        self.hardware_resumed.emit()
        self._check_and_emit()

    def _idle(self) -> None:
        self._sleep(1.5)
        self._check_and_emit()

    def run(self) -> None:
        self._last_state = self.engine.is_headphone_plugged()
        self._loop()


class PulseEventThread(_ProcessMonitorThread):
    """Background thread running `pactl subscribe`; emits `changed` when sinks, streams or cards change."""

    changed = pyqtSignal()

    def _command(self) -> Optional[List[str]]:
        return ["pactl", "subscribe"] if shutil.which("pactl") else None

    def _on_line(self, line: str) -> None:
        # e.g. "Event 'new' on sink-input #12"; the sink check also covers sink-input
        if " on sink" in line or " on card" in line:
            self.changed.emit()

    def _on_stable_exit(self) -> None:
        # The server went away (e.g. PipeWire restart) and came back: events may have been missed.
        self.changed.emit()

    def run(self) -> None:
        self._loop()
