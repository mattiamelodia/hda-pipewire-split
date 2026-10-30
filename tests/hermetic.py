"""Hermetic test base: nothing in the suite may touch the real audio system or the real home directory.

Importing this module (first, before any Qt or hda_split import) sets up the environment:
  * Qt runs on the offscreen platform (forced, not setdefault: some desktops export QT_QPA_PLATFORM=wayland
    and the tests would then open real windows on the user's session),
  * HOME points to a throw-away directory, so even an un-redirected path constant cannot reach ~/.config,
  * no bytecode is written next to the sources.

HermeticTestCase then, for every test:
  * replaces subprocess.run / Popen / check_output with a recorder that returns empty output for known
    read-only commands and raises HermeticViolation (a BaseException, so production `except Exception`
    cannot swallow it) for anything else that could change state and has not been stubbed,
  * makes subprocess.call / check_call / os.system / os.popen / os.fork unusable,
  * redirects CONFIG_DIR / CONFIG_FILE / PINS_FILE / PW_CONF_DIR / PW_DROPIN_FILE in every loaded module
    that holds a copy of them,
  * patches shutil.which so every tool "exists" unless the test lists it in self.missing_cmds,
  * stubs discover_hda_cards (it reads the real /proc/asound) to return no cards.
"""

import atexit
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("QT_QPA_PLATFORMTHEME", None)
sys.dont_write_bytecode = True

_FAKE_HOME = tempfile.mkdtemp(prefix="hda_test_home_")
atexit.register(shutil.rmtree, _FAKE_HOME, ignore_errors=True)
os.environ["HOME"] = _FAKE_HOME
for _var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
    os.environ.pop(_var, None)

# Production code logs warnings for every stubbed failure; keep the test output readable.
# (A handler on the logger disables logging.lastResort; assertLogs still works.)
logging.getLogger("hda_split").addHandler(logging.NullHandler())

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Captured before anything is patched; only reachable through HermeticTestCase.run_real_bash_syntax_check.
_REAL_RUN = subprocess.run
_REAL_POPEN = subprocess.Popen

import hda_split.config as cfg_mod  # noqa: E402
import hda_split.engine as eng_mod  # noqa: E402
import main as main_mod  # noqa: E402
import wizard as wizard_mod  # noqa: E402

PATH_NAMES = ("CONFIG_DIR", "CONFIG_FILE", "PINS_FILE", "PW_CONF_DIR", "PW_DROPIN_FILE")


class HermeticViolation(BaseException):
    """A test tried to run a state-changing command that nobody stubbed."""


# --- Command classification ---------------------------------------------------------------------
def _strip_wrappers(argv):
    argv = list(argv)
    if argv and os.path.basename(argv[0]) == "stdbuf":
        argv = argv[1:]
        while argv and argv[0].startswith("-"):
            argv = argv[1:]
    return argv


def is_read_only(argv):
    """True for commands that can only read state (or are purely cosmetic like notify-send)."""
    argv = _strip_wrappers(argv)
    if not argv or not all(isinstance(a, (str, os.PathLike)) for a in argv):
        return False
    argv = [os.fspath(a) for a in argv]
    exe, args = os.path.basename(argv[0]), argv[1:]
    if exe == "pactl":
        return bool(args) and (args[0] in ("list", "info", "stat", "subscribe") or args[0].startswith("get-"))
    if exe == "amixer":
        rest = list(args)
        while rest and rest[0] in ("-c", "-D", "-M"):
            rest = rest[2:] if rest[0] in ("-c", "-D") else rest[1:]
        return bool(rest) and rest[0] in ("controls", "scontrols", "cget", "sget", "get", "contents", "scontents")
    if exe == "pw-link":
        return bool(args) and all(a in ("-i", "-o", "-l", "-I", "-t", "-p", "-n") for a in args)
    if exe == "playerctl":
        return "metadata" in args or "status" in args
    if exe == "dbus-send":
        return any("ListNames" in a or "Properties.Get" in a for a in args)
    if exe == "alsactl":
        return bool(args) and args[0] == "monitor"
    if exe == "notify-send":
        return True
    return False


class FakeProc:
    """Stand-in for subprocess.Popen: yields given stdout lines, never runs anything."""

    def __init__(self, argv, lines=(), returncode=0):
        self.args = list(argv)
        self._lines = lines
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.waited = False

    @property
    def stdout(self):
        return self._lines() if callable(self._lines) else iter(list(self._lines))

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class _Stub:
    def __init__(self, match, stdout="", stderr="", rc=0, exc=None, proc=None):
        self.match, self.stdout, self.stderr, self.rc, self.exc, self.proc = match, stdout, stderr, rc, exc, proc

    def matches(self, argv):
        if callable(self.match):
            return bool(self.match(argv))
        m = list(self.match)
        return argv[: len(m)] == m


class SubprocessRecorder:
    """Records every spawn attempt and answers from stubs; see module docstring."""

    def __init__(self):
        self.calls = []  # [{"kind": "run"|"Popen"|"check_output", "argv": [...], "kwargs": {...}}]
        self.violations = []
        self._stubs = []

    # --- configuration
    def stub(self, match, stdout="", stderr="", rc=0, exc=None, proc=None):
        """Answers commands whose argv starts with `match` (or for which match(argv) is true).
        stdout may be a callable(argv) -> str. Later stubs win."""
        self._stubs.insert(0, _Stub(match, stdout, stderr, rc, exc, proc))

    def allow(self, *prefixes):
        """Declares state-changing commands as acceptable in this test (they still are not executed)."""
        for p in prefixes:  # lowest priority: specific stubs always win, whatever the call order
            self._stubs.append(_Stub(p))

    def allow_audio_mutations(self):
        """The normal runtime mutations. load-module / unload-module are deliberately NOT included."""
        self.allow(
            ["pactl", "set-card-profile"], ["pactl", "set-default-sink"],
            ["pactl", "set-sink-volume"], ["pactl", "set-sink-mute"],
            ["pactl", "set-sink-input-volume"], ["pactl", "set-sink-input-mute"],
            ["pactl", "move-sink-input"],
            ["amixer"],  # any amixer command; tests assert on the recorded arguments
            ["pw-link"], ["systemctl", "--user", "restart"],
        )

    # --- inspection
    def argvs(self, kind=None):
        return [c["argv"] for c in self.calls if kind is None or c["kind"] == kind]

    def find(self, *prefix):
        prefix = list(prefix)
        return [a for a in self.argvs() if a[: len(prefix)] == prefix]

    def joined(self):
        return [" ".join(a) for a in self.argvs()]

    def reset(self):
        self.calls.clear()

    # --- dispatch
    def _dispatch(self, kind, args, kwargs):
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, (str, bytes)) or kwargs.get("shell"):
            self._violate(f"shell/str command: {argv!r}")
        argv = [os.fspath(a) for a in argv]
        self.calls.append({"kind": kind, "argv": argv, "kwargs": dict(kwargs)})
        for stub in self._stubs:
            if stub.matches(argv):
                return argv, stub
        if is_read_only(argv):
            return argv, _Stub(argv)
        self._violate(f"un-stubbed state-changing command: {argv}")

    def _violate(self, msg):
        self.violations.append(msg)
        raise HermeticViolation(msg)

    def _result_text(self, stub, argv, which):
        value = stub.stdout if which == "stdout" else stub.stderr
        return value(argv) if callable(value) else value

    def run(self, *args, **kwargs):
        argv, stub = self._dispatch("run", args, kwargs)
        if stub.exc is not None:
            raise stub.exc
        return subprocess.CompletedProcess(
            argv, stub.rc, self._result_text(stub, argv, "stdout"), self._result_text(stub, argv, "stderr")
        )

    def check_output(self, *args, **kwargs):
        argv, stub = self._dispatch("check_output", args, kwargs)
        if stub.exc is not None:
            raise stub.exc
        if stub.rc != 0:
            raise subprocess.CalledProcessError(stub.rc, argv, self._result_text(stub, argv, "stdout"))
        return self._result_text(stub, argv, "stdout")

    def Popen(self, *args, **kwargs):  # noqa: N802 - mirrors subprocess.Popen
        argv, stub = self._dispatch("Popen", args, kwargs)
        if stub.exc is not None:
            raise stub.exc
        if stub.proc is not None:
            return stub.proc(argv)
        text = self._result_text(stub, argv, "stdout")
        return FakeProc(argv, lines=text.splitlines(keepends=True), returncode=stub.rc)


class FakeClock:
    """Deterministic time: sleep() advances monotonic() instead of blocking."""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


DEFAULT_CFG = {
    "ALSA_CARD_ID": "2",
    "PCI_ID": "0000_65_00.6",
    "CARD_NAME": "alsa_card.pci-0000_65_00.6",
    "NODE_TARGET": "alsa_output.pci-0000_65_00.6.pro-output-0",
    "HP_PORT_L": "playback_AUX0",
    "HP_PORT_R": "playback_AUX1",
    "SPK_PORT_L": "playback_AUX2",
    "SPK_PORT_R": "playback_AUX3",
}


class HermeticTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.missing_cmds = set()
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="hda_test_")
        self.addCleanup(self.tmp_dir.cleanup)
        self.base = Path(self.tmp_dir.name)

        self.sp = SubprocessRecorder()
        self.addCleanup(self._assert_no_violations)

        def violating(name):
            def _f(*a, **k):
                self.sp._violate(f"{name} must not be used: {a!r}")
            return _f

        patches = [
            mock.patch.object(subprocess, "run", self.sp.run),
            mock.patch.object(subprocess, "Popen", self.sp.Popen),
            mock.patch.object(subprocess, "check_output", self.sp.check_output),
            mock.patch.object(subprocess, "call", violating("subprocess.call")),
            mock.patch.object(subprocess, "check_call", violating("subprocess.check_call")),
            mock.patch.object(subprocess, "getoutput", violating("subprocess.getoutput")),
            mock.patch.object(subprocess, "getstatusoutput", violating("subprocess.getstatusoutput")),
            mock.patch.object(os, "system", violating("os.system")),
            mock.patch.object(os, "popen", violating("os.popen")),
            mock.patch.object(os, "fork", violating("os.fork")),
            mock.patch.object(os, "posix_spawn", violating("os.posix_spawn")),
            mock.patch.object(shutil, "which", self._which),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        # Redirect every copy of the path constants.
        self.config_dir = self.base / "config"
        self.pw_conf_dir = self.base / "pipewire.conf.d"
        values = {
            "CONFIG_DIR": self.config_dir,
            "CONFIG_FILE": self.config_dir / "config.env",
            "PINS_FILE": self.config_dir / "pinned_apps.json",
            "PW_CONF_DIR": self.pw_conf_dir,
            "PW_DROPIN_FILE": self.pw_conf_dir / "99-hda-split.conf",
        }
        self.paths = SimpleNamespace(**values)
        for name, module in list(sys.modules.items()):
            if module is None or not (name.startswith("hda_split") or name in ("main", "wizard")):
                continue
            for const, value in values.items():
                if hasattr(module, const):
                    p = mock.patch.object(module, const, value)
                    p.start()
                    self.addCleanup(p.stop)
            if hasattr(module, "discover_hda_cards"):
                p = mock.patch.object(module, "discover_hda_cards", lambda: [])
                p.start()
                self.addCleanup(p.stop)

        cfg_mod._last_failure.clear()

    # --- helpers
    def _which(self, name, *a, **k):
        return None if name in self.missing_cmds else f"/fake/bin/{name}"

    def _assert_no_violations(self):
        self.assertEqual(self.sp.violations, [], "test attempted un-stubbed state-changing commands")

    def write_config(self, **overrides):
        """Writes config.env + drop-in into the temp dir (via the real save_config) and returns the dict."""
        data = {**DEFAULT_CFG, **overrides}
        cfg_mod.save_config(data)
        return data

    def make_engine(self, **overrides):
        self.write_config(**overrides)
        return eng_mod.AudioEngine()

    def run_real_bash_syntax_check(self, script):
        """The only place that may use the real subprocess: `bash -n <repo script>` (parses, never runs it)."""
        script = Path(script).resolve()
        if script.parent != ROOT or script.suffix != ".sh":
            raise HermeticViolation(f"refusing to run real bash on {script}")
        # subprocess.run builds on the module-level Popen, which is patched; restore it for this one call.
        with mock.patch.object(subprocess, "Popen", _REAL_POPEN):
            return _REAL_RUN(["bash", "-n", str(script)], capture_output=True, text=True, timeout=20)


# --- Fixture builders ---------------------------------------------------------------------------
def sink_input(sid, sink, props=None, volume=80, muted=False):
    body = "\n".join(f'\t\t{k} = "{v}"' for k, v in (props or {}).items())
    return (
        f"Sink Input #{sid}\n\tDriver: PipeWire\n\tOwner Module: n/a\n\tSink: {sink}\n"
        f"\tMute: {'yes' if muted else 'no'}\n"
        f"\tVolume: front-left: 52428 / {volume:3d}% / -5.81 dB,   front-right: 52428 / {volume:3d}% / -5.81 dB\n"
        f"\tProperties:\n{body}\n\n"
    )


def sinks_full(entries):
    """entries: (id, name, description, node.group or '', volume, muted)."""
    out = []
    for sid, name, desc, group, vol, muted in entries:
        group_line = f'\t\tnode.group = "{group}"\n' if group else ""
        out.append(
            f"Sink #{sid}\n\tState: RUNNING\n\tName: {name}\n\tDescription: {desc}\n"
            f"\tMute: {'yes' if muted else 'no'}\n"
            f"\tVolume: front-left: 65536 / {vol:3d}% / 0.00 dB,   front-right: 65536 / {vol:3d}% / 0.00 dB\n"
            f"\tBase Volume: 65536 / 100% / 0.00 dB\n\tProperties:\n{group_line}\t\tnode.name = \"{name}\"\n\n"
        )
    return "".join(out)


def sinks_short(names, start_id=40):
    return "\n".join(f"{start_id + i}\t{n}\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING" for i, n in enumerate(names))


def wait_until(app, cond, timeout_ms=3000):
    """Pumps Qt events until cond() is true or the (bounded) timeout expires."""
    import time

    from PyQt6.QtTest import QTest

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        if cond():
            return True
        QTest.qWait(2)
    app.processEvents()
    return bool(cond())


def settle(app, ms=100):
    import time

    from PyQt6.QtTest import QTest

    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(2)


_APP = None


def get_qapp():
    """One shared QApplication for the whole suite (Qt allows only one per process)."""
    global _APP
    from PyQt6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication(["hda-tests"])
    _APP.setQuitOnLastWindowClosed(False)
    return _APP
