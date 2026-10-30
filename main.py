#!/usr/bin/env python3
"""Unified CLI and GUI entry point for HDA PipeWire Audio Splitter."""

import argparse
import logging
import os
import shutil
import sys
from typing import Any, Optional

from hda_split.config import CONFIG_FILE, send_notification

WIZARD_HINT = "HDA Audio Control is not configured yet. Run `hda-audio-control --wizard` in a terminal."


def setup_logging() -> None:
    level = logging.DEBUG if os.environ.get("HDA_SPLIT_DEBUG") == "1" else logging.WARNING
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Universal HDA PipeWire Audio Splitter & Tab Router"
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Start minimized in the system tray",
    )
    parser.add_argument(
        "--wizard",
        action="store_true",
        help="Run the interactive hardware calibration wizard",
    )
    parser.add_argument(
        "--force-hp",
        action="store_true",
        help="Route all active streams to Headphones and exit",
    )
    parser.add_argument(
        "--force-spk",
        action="store_true",
        help="Route all active streams to Speakers and exit",
    )
    parser.add_argument(
        "--force-both",
        action="store_true",
        help="Route all active streams to Both and exit",
    )
    parser.add_argument(
        "--toggle-mute",
        action="store_true",
        help="Toggle mute on both master outputs and exit",
    )
    return parser.parse_args()


def handle_cli_actions(args: argparse.Namespace) -> bool:
    """Executes instant CLI actions (ideal for WM keybindings) and returns True if handled."""
    if not any((args.force_hp, args.force_spk, args.force_both, args.toggle_mute)):
        return False

    # Imported here so keybinding invocations never load Qt
    from hda_split.engine import AudioEngine

    engine = AudioEngine()

    if args.toggle_mute:
        muted = engine.toggle_all_sinks_mute()
        print(f"Outputs {'muted' if muted else 'unmuted'}")
        return True

    target = (
        "Headphones"
        if args.force_hp
        else ("Speakers" if args.force_spk else "Both")
    )
    moved = engine.force_all_streams(target)
    print(f"Routed {moved} active stream(s) to {target}")
    return True


def ensure_configured() -> None:
    """Runs the wizard on first launch from a terminal; otherwise tells the user how to configure."""
    if CONFIG_FILE.exists():
        return

    if sys.stdin.isatty():
        from wizard import run_wizard

        run_wizard()
        return

    # Desktop launcher or keybinding: there is no terminal to answer wizard prompts
    send_notification("Audio Router needs setup", WIZARD_HINT, urgency="critical")
    print(WIZARD_HINT, file=sys.stderr)
    sys.exit(1)


def daemonize_process() -> None:
    """Detaches the process into the background when starting in tray mode."""
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        print(f"Fork failed: {e}", file=sys.stderr)
        sys.exit(1)

    os.setsid()


def acquire_single_instance(server_name: str, notify_existing: bool) -> Optional[Any]:
    """Returns a listening QLocalServer, or None if another instance already runs.

    When another instance answers and notify_existing is set, it is asked to show its window.
    If the socket cannot be created at all, the server is returned unbound so this instance
    still runs (just without single-instance protection).
    """
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket

    log = logging.getLogger("hda_split.main")

    def ping_existing() -> bool:
        sock = QLocalSocket()
        sock.connectToServer(server_name)
        if not sock.waitForConnected(500):
            return False
        if notify_existing:
            sock.write(b"show\n")
            sock.flush()
            sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return True

    if ping_existing():
        return None

    # Nobody answered: clear any stale socket file left by a crashed instance, then listen
    QLocalServer.removeServer(server_name)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(server_name):
        if ping_existing():  # lost a startup race with another launch
            return None
        log.warning("Single-instance socket unavailable: %s", server.errorString())
    return server


def main() -> None:
    args = parse_arguments()
    setup_logging()

    if not shutil.which("pactl"):
        print(
            "Error: 'pactl' is required. Install pipewire-pulse or pulseaudio-utils.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.wizard:
        from wizard import run_wizard

        run_wizard()
        return

    ensure_configured()

    if handle_cli_actions(args):
        return

    if args.tray:
        daemonize_process()

    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    server = acquire_single_instance(f"hda-audio-control-{os.getuid()}", notify_existing=not args.tray)
    if server is None:
        sys.exit(0)

    from hda_split.gui import AppWindow

    window = AppWindow(start_in_tray=args.tray)

    def on_new_connection() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            if conn is None:
                return

            def read_request(c=conn) -> None:
                if b"show" in bytes(c.readAll()):
                    window.show_and_raise()

            conn.readyRead.connect(read_request)
            conn.disconnected.connect(conn.deleteLater)
            if conn.bytesAvailable():
                read_request()

    server.newConnection.connect(on_new_connection)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
