#!/usr/bin/env python3
"""Unified CLI and GUI entry point for HDA PipeWire Audio Splitter."""

import argparse
import os
import shutil
import sys

from PyQt6.QtWidgets import QApplication

from hda_split.config import CONFIG_FILE
from hda_split.engine import AudioEngine
from hda_split.gui import AppWindow
from wizard import run_wizard


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

    engine = AudioEngine()
    engine.ensure_hardware_ready()

    if args.toggle_mute:
        muted = engine.toggle_all_sinks_mute()
        print(f"Outputs {'muted' if muted else 'unmuted'}")
        return True

    target = (
        "Headphones"
        if args.force_hp
        else ("Speakers" if args.force_spk else "Both")
    )
    for stream in engine.get_streams():
        engine.route_stream(stream["id"], target)
    print(f"Routed all active streams to {target}")
    return True


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


def main() -> None:
    args = parse_arguments()

    if args.wizard or not CONFIG_FILE.exists():
        run_wizard()
        if args.wizard:
            return

    if not shutil.which("pactl"):
        print(
            "Error: 'pactl' is required. Install pipewire-pulse or pulseaudio-utils.",
            file=sys.stderr,
        )
        sys.exit(1)

    if handle_cli_actions(args):
        return

    if args.tray:
        daemonize_process()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    _win = AppWindow(start_in_tray=args.tray)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()