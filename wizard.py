#!/usr/bin/env python3
"""Hardware discovery and channel calibration wizard for the HDA PipeWire splitter."""

import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hda_split.config import (
    CONFIG_FILE,
    discover_hda_cards,
    load_config,
    port_channels,
    restore_silenced_levels,
    run_cmd,
    save_config,
)

SAMPLE_RATE = 48000
REQUIRED_PORTS = [f"playback_AUX{i}" for i in range(4)]
EXPECTED_SINKS = ("Headphones", "Speakers", "Both")


class WizardAbort(Exception):
    """Raised to stop the wizard cleanly; the profile/Auto-Mute snapshot is restored."""


# --- Small system helpers ---
def run_c(cmd: List[str]) -> str:
    """Like run_cmd, but forces the C locale so that output can be parsed reliably."""
    try:
        return subprocess.check_output(
            cmd,
            text=True,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        ).strip()
    except Exception:
        return ""


def get_card_profile(card_name: str) -> str:
    """Returns the active PipeWire profile of the card, or '' if it cannot be determined."""
    in_card = False
    for line in run_c(["pactl", "list", "cards"]).splitlines():
        stripped = line.strip()
        if stripped.startswith("Card #"):
            in_card = False
        elif stripped.startswith("Name:"):
            in_card = stripped.split(":", 1)[1].strip() == card_name
        elif in_card and stripped.startswith("Active Profile:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def get_automute(card_id: str) -> str:
    """Returns the current value of the ALSA 'Auto-Mute Mode' enum, or '' if absent."""
    out = run_c(["amixer", "-c", card_id, "sget", "Auto-Mute Mode"])
    match = re.search(r"Item0:\s*'([^']*)'", out)
    return match.group(1) if match else ""


def set_profile(card_name: str, profile: str) -> None:
    subprocess.run(
        ["pactl", "set-card-profile", card_name, profile],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def set_automute(card_id: str, value: str) -> None:
    subprocess.run(
        ["amixer", "-c", card_id, "set", "Auto-Mute Mode", value],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def restore_state(card: Dict[str, str]) -> None:
    """Puts the profile and Auto-Mute back to what they were when the wizard started."""
    if not card:
        return
    print("[*] Restoring the previous audio state...")
    before_profile = card.get("profile_before", "")
    if before_profile and before_profile != "pro-audio":
        set_profile(card["card_name"], before_profile)
        print(f"    Profile restored: {before_profile}")
    before_automute = card.get("automute_before", "")
    if before_automute:
        set_automute(card["id"], before_automute)
        print(f"    Auto-Mute Mode restored: {before_automute}")
    print("    (Mute switches and volumes were not touched back; the wizard never sets a volume.)")


# --- Calibration tone ---
def generate_probe_tone(
    filepath: Path,
    active_channels: List[int],
    duration_sec: float = 0.9,
    gap_sec: float = 0.4,
    freq: float = 440.0,
) -> None:
    """
    Writes a 4-channel WAV: a soft pulse on each channel in `active_channels`
    in turn (one after the other), with silence on all other channels.
    """
    pulse_len = int(duration_sec * SAMPLE_RATE)
    gap_len = int(gap_sec * SAMPLE_RATE)
    fade_len = SAMPLE_RATE * 0.08

    pulse: List[int] = []
    for i in range(pulse_len):
        if i < fade_len:
            envelope = i / fade_len
        elif i > pulse_len - fade_len:
            envelope = (pulse_len - i) / fade_len
        else:
            envelope = 1.0
        # Soft amplitude (0.20 of full scale) to keep headphones comfortable.
        pulse.append(int(32767.0 * 0.20 * envelope * math.sin(2.0 * math.pi * freq * i / SAMPLE_RATE)))

    data = bytearray()
    for n, ch in enumerate(active_channels):
        for val in pulse:
            frame = [0, 0, 0, 0]
            frame[ch] = val
            data.extend(struct.pack("<4h", *frame))
        if n < len(active_channels) - 1:
            data.extend(b"\x00" * (gap_len * 8))

    with wave.open(str(filepath), "wb") as wf:
        wf.setnchannels(4)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(data))


def build_pw_play_cmd(node_target: str, wav_path: Path) -> List[str]:
    """
    Builds the pw-play command line. The stream is a 4-channel stream whose channel map
    is AUX0..AUX3 and which is aimed at the pro-audio node itself (--target takes only a
    node name or serial, not node:port). With stream.dont-remix the channels are linked
    1:1 by name to the node's playback_AUXn ports; node.dont-fallback stops WirePlumber
    from re-routing the stream to the default sink if the target is not available.
    """
    return [
        "pw-play",
        "--target", node_target,
        "--channels", "4",
        "--channel-map", "AUX0,AUX1,AUX2,AUX3",
        "--properties",
        "{ stream.dont-remix=true node.dont-fallback=true node.dont-reconnect=true "
        "node.name=hda-split-probe }",
        str(wav_path),
    ]


def play_test_tone(node_target: str, ch_a: int, ch_b: int) -> bool:
    """Plays a soft pulse on AUX<ch_a>, then on AUX<ch_b>. Returns False if playback failed."""
    with tempfile.TemporaryDirectory(prefix="hda_probe_") as tmp:
        wav_path = Path(tmp) / "probe_tone.wav"
        generate_probe_tone(wav_path, [ch_a, ch_b])
        try:
            result = subprocess.run(
                build_pw_play_cmd(node_target, wav_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
        except FileNotFoundError:
            print("[-] 'pw-play' not found (it ships with the pipewire tools).")
            return False
        except subprocess.TimeoutExpired:
            print("[-] pw-play did not finish (the stream was probably never linked to the node).")
            return False
    if result.returncode != 0:
        print(f"[-] pw-play failed: {result.stderr.strip() or 'unknown error'}")
        return False
    return True


# --- Interactive steps ---
def select_analog_card(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Selects the analog HDA card, asking the user only if several are present."""
    analog_cards = [c for c in cards if c["is_analog_hda"]]
    if not analog_cards:
        print("[-] Error: no analog HDA codec detected. Sound cards found:")
        for c in cards:
            print(f"    Card {c['id']}: {c['long']} ({c['codecs']})")
        sys.exit(1)

    if len(analog_cards) == 1:
        selected = analog_cards[0]
    else:
        print("\nSeveral analog HDA cards detected:")
        for idx, c in enumerate(analog_cards, start=1):
            print(f"  [{idx}] Card {c['id']}: {c['long']} ({c['codecs']})")
        while True:
            choice = input(f"Select a card [1-{len(analog_cards)}] (ENTER = 1): ").strip()
            if not choice:
                selected = analog_cards[0]
                break
            if choice.isdigit() and 1 <= int(choice) <= len(analog_cards):
                selected = analog_cards[int(choice) - 1]
                break
            print(f"    Please enter a number between 1 and {len(analog_cards)}.")

    print(
        f"[+] Using card {selected['id']} - {selected['long']} ({selected['codecs']})"
    )
    return selected


def snapshot_original_state(selected: Dict[str, Any]) -> Dict[str, str]:
    """
    Records the profile and Auto-Mute value to restore on uninstall, before any change.
    If the card is already in pro-audio and a previous config for the same card holds
    values, those are kept (pro-audio is never recorded as the 'original').
    """
    card_name = selected["card_name"]
    card_id = selected["id"]
    current_profile = get_card_profile(card_name)
    current_automute = get_automute(card_id)

    previous: Dict[str, str] = {}
    if CONFIG_FILE.exists():
        old = load_config()
        if old.get("PCI_ID") == selected["pci_id"]:
            previous = old

    already_pro = current_profile == "pro-audio"
    if already_pro:
        orig_profile = previous.get("ORIGINAL_PROFILE", "")
        orig_automute = previous.get("ORIGINAL_AUTOMUTE", "")
        if not orig_profile or not orig_automute:
            print(
                "[!] The card is already in 'pro-audio' and no earlier values are stored: "
                "the original profile/Auto-Mute cannot be recorded."
            )
    else:
        orig_profile = current_profile
        orig_automute = current_automute

    return {
        "ORIGINAL_PROFILE": orig_profile,
        "ORIGINAL_AUTOMUTE": orig_automute,
        # What to put back if this wizard run is aborted:
        "profile_before": current_profile,
        "automute_before": current_automute,
    }


def prepare_hardware_profile(card_id: str, card_name: str, default_target: str) -> Optional[str]:
    """
    Switches the card to pro-audio, unmutes the ALSA switches, rescues levels stuck at 0% and
    resolves the PipeWire node name, restricted to this card's own pro-output nodes.
    Returns None if no node for this card shows up.
    """
    print(f"[*] Activating 'pro-audio' profile on {card_name}...")
    set_profile(card_name, "pro-audio")

    node_prefix = default_target.rsplit(".pro-output-", 1)[0] + ".pro-output-"
    node_target: Optional[str] = None
    for _ in range(10):
        time.sleep(0.5)
        nodes: List[str] = []
        for line in run_cmd(["pw-link", "-i"]).splitlines():
            name = line.split(":")[0].strip()
            if name.startswith(node_prefix) and name not in nodes:
                nodes.append(name)
        if default_target in nodes:
            node_target = default_target
            break
        if nodes:
            node_target = nodes[0]
            break

    for ctrl in ("Master", "Speaker", "Headphone", "Bass Speaker"):
        subprocess.run(
            ["amixer", "-c", card_id, "set", ctrl, "unmute"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    set_automute(card_id, "Disabled")
    if node_target is not None:
        restore_silenced_levels(card_id, node_target)
    return node_target


def verify_four_channels(node_target: str) -> bool:
    """Checks that the node exposes playback_AUX0..AUX3 (poll briefly: ports may appear late)."""
    ports: List[str] = []
    for _ in range(6):
        ports = port_channels(node_target)
        if all(p in ports for p in REQUIRED_PORTS):
            return True
        time.sleep(0.5)
    print(
        f"\n[-] The node '{node_target}' does not expose four independent playback channels "
        f"(found: {', '.join(ports) if ports else 'none'})."
    )
    print("    This codec/PCM cannot drive headphones and speakers independently in")
    print("    'pro-audio', so the split is not possible on this machine. Nothing was saved.")
    return False


def ask_pair(node_target: str, ch_a: int, ch_b: int, label: str) -> str:
    """Plays the pair until the user answers; returns 'h', 's' or 'n'."""
    print(f"\n--- Test {label}: AUX{ch_a} then AUX{ch_b} ---")
    input("Press [ENTER] to play two short, soft pulses... ")
    while True:
        play_test_tone(node_target, ch_a, ch_b)
        ans = input(
            "Where did you hear them?  [h]eadphones / [s]peakers / [n]othing / [r]eplay: "
        ).strip().lower()
        if ans in ("h", "headphones"):
            return "h"
        if ans in ("s", "speakers"):
            return "s"
        if ans in ("n", "nothing"):
            return "n"
        if ans not in ("r", "replay"):
            print("    Please answer h, s, n or r.")
        # 'r' (or an invalid answer): ask/play again


def calibrate_aux_channels(node_target: str) -> Tuple[str, str, str, str]:
    """Tests AUX0/1 and AUX2/3 and returns (hp_l, hp_r, spk_l, spk_r); raises WizardAbort."""
    print("\n" + "-" * 66)
    print("  AUX CHANNEL CALIBRATION - plug your headphones into the 3.5mm jack")
    print("-" * 66)
    print("  Each test plays two soft pulses on one pair of channels only.")
    print("  Hardware (ALSA) gain is not changed by this tool: if your headphones are")
    print("  on your ears, lower the volume first.")

    while True:
        first = ask_pair(node_target, 0, 1, "1/2")
        second = ask_pair(node_target, 2, 3, "2/2")

        if first == "h" and second == "s":
            return "playback_AUX0", "playback_AUX1", "playback_AUX2", "playback_AUX3"
        if first == "s" and second == "h":
            return "playback_AUX2", "playback_AUX3", "playback_AUX0", "playback_AUX1"

        print("\n[!] Those answers do not give a usable mapping:")
        if first == "n" and second == "n":
            print("    nothing was heard on either pair.")
        elif first == second:
            print("    both pairs were reported on the same output.")
        else:
            print("    one pair was silent, so the other pair cannot be told apart reliably.")
        print("    Check that the headphones are plugged in and the speakers are not muted.")
        retry = input("Try again? [Y/n]: ").strip().lower()
        if retry in ("n", "no"):
            raise WizardAbort("Calibration was not completed; no mapping saved.")


def restart_and_verify_sinks() -> bool:
    """Restarts PipeWire so the drop-in loads, then polls for the virtual sinks (up to 5 s)."""
    print("[*] Restarting PipeWire to load 99-hda-split.conf...")
    subprocess.run(
        ["systemctl", "--user", "restart", "pipewire"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5.0
    names: List[str] = []
    while True:
        time.sleep(0.5)
        names = [
            fields[1]
            for fields in (
                line.split("\t") for line in run_c(["pactl", "list", "sinks", "short"]).splitlines()
            )
            if len(fields) > 1
        ]
        if all(s in names for s in EXPECTED_SINKS) or time.monotonic() >= deadline:
            break
    missing = [s for s in EXPECTED_SINKS if s not in names]
    if missing:
        print(f"[-] After restarting PipeWire these sinks did not appear: {', '.join(missing)}")
        print("    The configuration is saved. Try:")
        print("      systemctl --user restart pipewire pipewire-pulse wireplumber")
        print("      pactl list sinks short")
        print("      journalctl --user -u pipewire -n 50")
        return False
    print("[+] Virtual sinks are present: " + ", ".join(EXPECTED_SINKS))
    return True


def run_wizard() -> None:
    print("=" * 66)
    print("   HDA PipeWire Splitter - Discovery & Calibration Wizard")
    print("=" * 66)

    cards = discover_hda_cards()
    if not cards:
        print("[-] Error: No sound cards detected in /proc/asound.")
        sys.exit(1)

    card: Dict[str, str] = {}
    try:
        selected = select_analog_card(cards)
        card_id = selected["id"]
        card_name = selected["card_name"]

        # Snapshot BEFORE changing anything.
        snap = snapshot_original_state(selected)
        card = {"id": card_id, "card_name": card_name,
                "profile_before": snap["profile_before"], "automute_before": snap["automute_before"]}

        node_target = prepare_hardware_profile(card_id, card_name, selected["node_target"])
        if node_target is None:
            print(f"[-] Error: no pro-audio output node for card {card_id} ({card_name}) appeared.")
            print("    Check `pactl list cards` and that this card supports the 'pro-audio' profile.")
            raise WizardAbort("No pro-audio node found.")

        if not verify_four_channels(node_target):
            raise WizardAbort("Four independent channels are not available.")

        hp_l, hp_r, spk_l, spk_r = calibrate_aux_channels(node_target)

        save_config({
            "ALSA_CARD_ID": card_id,
            "PCI_ID": selected["pci_id"],
            "CARD_NAME": card_name,
            "NODE_TARGET": node_target,
            "HP_PORT_L": hp_l,
            "HP_PORT_R": hp_r,
            "SPK_PORT_L": spk_l,
            "SPK_PORT_R": spk_r,
            "ORIGINAL_PROFILE": snap["ORIGINAL_PROFILE"],
            "ORIGINAL_AUTOMUTE": snap["ORIGINAL_AUTOMUTE"],
        })
    except WizardAbort as e:
        print(f"\n[-] {e}")
        restore_state(card)
        sys.exit(1)
    except (EOFError, KeyboardInterrupt):
        print("\n[-] Wizard interrupted; nothing was saved.")
        restore_state(card)
        sys.exit(1)

    print(f"\n[+] Configuration saved to: {CONFIG_FILE}")
    if not restart_and_verify_sinks():
        sys.exit(1)


if __name__ == "__main__":
    run_wizard()
