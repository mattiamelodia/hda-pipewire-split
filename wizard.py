#!/usr/bin/env python3
"""Universal Hardware Auto-Discovery and Channel Calibration Wizard."""

import math
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Tuple

from hda_split.config import CONFIG_FILE, discover_hda_cards, run_cmd, save_config

PROBE_WAV_PATH = Path("/tmp/hda_probe_tone.wav")


def generate_soft_tone(
    filepath: Path,
    duration_sec: float = 0.9,
    freq: float = 440.0,
    sample_rate: int = 48000,
) -> None:
    """Generates a soft sinusoidal pulse (20% volume) with fade-in/out."""
    num_samples = int(duration_sec * sample_rate)
    fade_len = sample_rate * 0.08

    with wave.open(str(filepath), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)

        data = bytearray()
        for i in range(num_samples):
            if i < fade_len:
                envelope = i / fade_len
            elif i > num_samples - fade_len:
                envelope = (num_samples - i) / fade_len
            else:
                envelope = 1.0

            # Reduced amplitude (0.20) to avoid discomfort in headphones
            val = int(
                32767.0
                * 0.20
                * envelope
                * math.sin(2.0 * math.pi * freq * (i / sample_rate))
            )
            data.extend(struct.pack("<hh", val, val))

        wf.writeframes(data)


def play_test_tone(target_sink: str, ch_left: str, ch_right: str) -> None:
    """Plays the generated test tone sequentially on the specified left and right AUX ports."""
    try:
        generate_soft_tone(PROBE_WAV_PATH)
        for ch in (ch_left, ch_right):
            subprocess.run(
                ["pw-play", "--target", f"{target_sink}:{ch}", str(PROBE_WAV_PATH)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    finally:
        PROBE_WAV_PATH.unlink(missing_ok=True)


def select_analog_card(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Prompts the user to select an analog HDA card if multiple are available."""
    analog_cards = [c for c in cards if c["is_analog_hda"]]
    selected = analog_cards[0] if analog_cards else cards[0]

    print(
        f"[+] Analog HDA card detected: Card {selected['id']} - "
        f"{selected['long']} ({selected['codecs']})"
    )

    if len(analog_cards) > 1:
        print("\nOther analog cards detected:")
        for idx, c in enumerate(analog_cards, start=1):
            print(f"  [{idx}] Card {c['id']}: {c['long']} ({c['codecs']})")
        choice = input("Press [ENTER] to confirm #1 or enter the number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(analog_cards):
            selected = analog_cards[int(choice) - 1]

    return selected


def prepare_hardware_profile(card_id: str, card_name: str, default_target: str) -> str:
    """Switches the card to pro-audio, unmutes ALSA channels, and resolves the PipeWire node name."""
    print(f"[*] Activating 'pro-audio' profile on {card_name}...")
    subprocess.run(
        ["pactl", "set-card-profile", card_name, "pro-audio"],
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.8)

    node_target = default_target
    pw_ports = run_cmd(["pw-link", "-i"])
    if node_target not in pw_ports:
        for line in pw_ports.splitlines():
            if "alsa_output." in line and ".pro-output-" in line and ":" in line:
                node_target = line.split(":")[0].strip()
                break

    for ctrl in ("Master", "Speaker", "Headphone"):
        subprocess.run(
            ["amixer", "-c", card_id, "set", ctrl, "unmute", "100%"],
            stderr=subprocess.DEVNULL,
        )
    subprocess.run(
        ["amixer", "-c", card_id, "set", "Auto-Mute Mode", "Disabled"],
        stderr=subprocess.DEVNULL,
    )
    return node_target


def calibrate_aux_channels(node_target: str) -> Tuple[str, str, str, str]:
    """Interactively plays a test tone on AUX0/AUX1 and returns mapped port names."""
    print("\n" + "-" * 66)
    print("  AUX CHANNELS QUICK CALIBRATION (Plug your headphones into the 3.5mm jack)")
    print("-" * 66)
    input("Press [ENTER] to play a short low-volume pulse on AUX0/AUX1...")

    while True:
        play_test_tone(node_target, "playback_AUX0", "playback_AUX1")
        ans = (
            input("\nDo you hear the sound coming from the HEADPHONES? [Y/n/r to replay]: ")
            .strip()
            .lower()
        )
        if ans == "r":
            continue
        if ans in ("n", "no"):
            return "playback_AUX2", "playback_AUX3", "playback_AUX0", "playback_AUX1"
        return "playback_AUX0", "playback_AUX1", "playback_AUX2", "playback_AUX3"


def run_wizard() -> None:
    print("=" * 66)
    print("   Universal HDA PipeWire Splitter - Auto-Discovery Wizard (v1.0)")
    print("=" * 66)

    cards = discover_hda_cards()
    if not cards:
        print("[-] Error: No sound cards detected in /proc/asound.")
        sys.exit(1)

    selected = select_analog_card(cards)
    card_id = selected["id"]
    card_name = selected["card_name"]

    node_target = prepare_hardware_profile(card_id, card_name, selected["node_target"])
    hp_l, hp_r, spk_l, spk_r = calibrate_aux_channels(node_target)

    scontrols = run_cmd(["amixer", "-c", card_id, "scontrols"])
    has_automute = "yes" if "Auto-Mute Mode" in scontrols else "no"

    save_config({
        "ALSA_CARD_ID": card_id,
        "PCI_ID": selected["pci_id"],
        "CARD_NAME": card_name,
        "NODE_TARGET": node_target,
        "HP_PORT_L": hp_l,
        "HP_PORT_R": hp_r,
        "SPK_PORT_L": spk_l,
        "SPK_PORT_R": spk_r,
        "HAS_AUTOMUTE": has_automute,
    })

    print(f"\n[+] Configuration saved to: {CONFIG_FILE}")
    print("[*] Loading native modules into PipeWire (99-hda-split.conf)...")
    subprocess.run(["systemctl", "--user", "restart", "pipewire"], stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    run_wizard()