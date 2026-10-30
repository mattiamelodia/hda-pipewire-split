# Universal HDA PipeWire Audio Splitter & Per-Tab Router (v1.0)

A standalone, zero-daemon Linux audio router and hardware splitter designed for laptops with **Realtek HDA** codecs (`ALC285`, `ALC287`, `ALC295`, `ALC897`, `ALC256`, etc.). It unlocks simultaneous, independent playback across **3.5mm Headphones** and **Internal Speakers** with granular per-tab and per-application routing.

---

## The Problem

On modern Linux laptops (ASUS ROG/Zephyrus, Lenovo ThinkPad/Yoga, Dell XPS, HP Spectre) with shared HDA audio codecs:
1. **Mutually Exclusive Outputs**: Plugging in 3.5mm headphones triggers hardware/ALSA `Auto-Mute`, cutting off the internal speakers.
2. **Single Shared PipeWire Sink**: PipeWire/WirePlumber exposes a single combined output node, preventing independent playback unless switched to raw `pro-audio` profiles (`AUX0`–`AUX3`).
3. **No Per-Tab Granularity**: Standard mixers cannot easily distinguish or pin individual browser tabs (e.g., keeping Discord or a lecture on headphones while playing YouTube or music on the laptop speakers).

---

## Architecture & How It Works

The application runs locally with zero background overhead, bridging ALSA hardware events directly to PipeWire's native loopback and combine modules.

```text
 [ Physical 3.5mm Jack ] 
        │ (Hotplug / alsactl monitor)
        ▼
┌────────────────────────────────────────────────────────┐
│                   AudioEngine (Python)                 │
│  - Jack State Detection                                │
│  - ALSA Auto-Mute Control                              │
│  - Stream & Sink Routing                               │
└───┬────────────────────────────────┬───────────────────┘
    │                                │
    ▼                                ▼
[ Native Pipewire Sinks ]    [ Per-App Pinned Rules ]
 ├── Headphones (AUX0/1)      └── pinned_apps.json
 ├── Speakers   (AUX2/3)
 └── Both       (Combine)
 ```

---

## Key Features in v1.0

* **100% Native PipeWire Drop-In (Zero Subprocesses)**: Generates `~/.config/pipewire/pipewire.conf.d/99-hda-split.conf` using `libpipewire-module-loopback` and `libpipewire-module-combine-stream`. Virtual `Headphones`, `Speakers`, and an isolated `Both` sink are loaded natively by the PipeWire daemon at boot without external background processes.
* **Event-Driven Jack Detection (0% CPU, 0ms Latency)**: Uses a non-blocking `alsactl monitor` thread with auto-recovery on suspend/resume. The app sleeps at 0% CPU and reacts instantaneously when the physical 3.5mm jack is plugged or unplugged.
* **Persistent App Pinning (`pinned_apps.json`)**: Streams or applications manually pinned are remembered across application restarts and jack disconnect events.
* **Master Volume Sliders & USB/Bluetooth Detection**: Quick control panels for master output volumes and dynamic detection of external USB DACs or Bluetooth headphones.
* **Universal HDA Auto-Discovery Wizard**: Automatically scans `/proc/asound` and `/sys/class/sound`, filters out GPU HDMI controllers, selects the analog HDA codec, and calibrates physical `AUX` channels using an in-memory soft sine wave impulse.
* **System Tray Integration & CLI Keybindings**: Minimizes cleanly to the system tray with live jack status and provides instant CLI flags (`--force-hp`, `--force-spk`, `--force-both`, `--toggle-mute`) for window manager keybindings (Hyprland, Sway, i3).

---

## Architecture & Directory Structure

```text
hda-pipewire-split/
├── hda_split/
│   ├── __init__.py                  # Package metadata
│   ├── config.py                    # HDA auto-discovery & native drop-in generator
│   ├── engine.py                    # ALSA unmuting, master volumes & stream routing
│   ├── monitor.py                   # Event-driven QThread with auto-recovery
│   ├── mpris.py                     # D-Bus / playerctl browser tab title resolver
│   └── gui.py                       # PyQt6 modern dark mixer, Master bars & System Tray
├── main.py                          # Unified CLI entry point (--tray, --wizard, --force-*)
├── wizard.py                        # Interactive soft-tone AUX channel calibration
├── hda-audio-control.desktop        # Desktop launcher entry
├── install.sh                       # Automated modular installer
├── uninstall.sh                     # Clean uninstaller (removes drop-in & configs)
├── PKGBUILD                         # Arch Linux / AUR package build script
├── tests/                           # Unit test suite
└── README.md                        # Documentation
```

---

## Requirements

* **Audio Stack**: `pipewire`, `wireplumber`, `pipewire-pulse` (or `pulseaudio-utils` for `pactl`), `alsa-utils` (`amixer`, `alsactl`)
* **Runtime**: `python3`, `python-pyqt6`
* **Optional**: `playerctl` (recommended for faster MPRIS browser tab title detection)

### Install dependencies on Arch Linux:
```bash
sudo pacman -S python python-pyqt6 pipewire pipewire-pulse wireplumber alsa-utils playerctl
```

### Install dependencies on Ubuntu / Debian / Fedora:
```bash
# Ubuntu / Debian
sudo apt install python3 python3-pyqt6 pipewire pipewire-pulse pulseaudio-utils alsa-utils playerctl

# Fedora
sudo dnf install python3 python3-pyqt6 pipewire pipewire-pulseaudio pulseaudio-utils alsa-utils playerctl
```

---

## Installation & Quick Start

```bash
git clone [https://github.com/your-username/hda-pipewire-split.git](https://github.com/your-username/hda-pipewire-split.git)
cd hda-pipewire-split
chmod +x install.sh uninstall.sh
./install.sh
```

1. **First-Time Calibration**: If `~/.config/pipewire-split/config.env` is missing, `install.sh` automatically launches the interactive wizard. Plug in your headphones, press `Enter` to hear a soft 440Hz tone, and confirm (`[Y/n]`) where the sound came from.
2. **Native Sink Creation**: The wizard generates `~/.config/pipewire/pipewire.conf.d/99-hda-split.conf` and restarts PipeWire so the virtual sinks are immediately available system-wide.

---

## Usage

Launch **HDA Audio Router** from your application launcher (Rofi, Wofi, GNOME, KDE) or via terminal:

```bash
# Open the main GUI mixer
hda-audio-control

# Start minimized in the System Tray
hda-audio-control --tray

# Re-run the hardware discovery and AUX channel wizard
hda-audio-control --wizard
```

### CLI Shortcut Flags (Ideal for Hyprland / WM Keybindings):
```bash
hda-audio-control --force-hp     # Route all active streams to Headphones
hda-audio-control --force-spk    # Route all active streams to Speakers
hda-audio-control --force-both   # Route all active streams to Both (Isolated)
hda-audio-control --toggle-mute  # Toggle master mute on internal outputs
```

### Routing Controls per Stream:
* **Headphones**: Routes stream exclusively to the 3.5mm jack.
* **Speakers**: Routes stream exclusively to internal laptop speakers.
* **Both**: Mirrors playback across both outputs simultaneously via an isolated stream-combine sink (no cross-talk with other streams).
* **📌 Pin Toggle**: Saves application routing persistently in `pinned_apps.json`.

---

## Tested Hardware Compatibility Matrix

| Laptop Model / Series | Audio Codec | AUX0 / AUX1 | AUX2 / AUX3 | Auto-Mute Control | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **ASUS ROG Zephyrus (G14 / G16 / M16)** | Realtek ALC285 / ALC289 | Headphones | Internal Speakers | Present (`Disabled`) | ✅ Verified |
| **ASUS TUF Gaming (A15 / F15)** | Realtek ALC256 / ALC285 | Headphones | Internal Speakers | Present (`Disabled`) | ✅ Verified |
| **Lenovo ThinkPad (T14 / X1 Carbon / P1)** | Realtek ALC285 / ALC287 | Variable | Variable | Present (`Disabled`) | ✅ Verified via Wizard |
| **Lenovo Legion / Yoga Slim 7** | Realtek ALC287 | Headphones | Internal Speakers | Present (`Disabled`) | ✅ Verified |
| **Dell XPS (13 / 15 / 17)** | Realtek ALC289 / ALC295 | Headphones | Internal Speakers | Present (`Disabled`) | ✅ Verified |
| **HP Spectre x360 / EliteBook** | Realtek ALC285 / ALC295 | Headphones | Internal Speakers | Present (`Disabled`) | ✅ Verified |

---

## Troubleshooting & Uninstallation

* **Verify native PipeWire virtual sinks are loaded:**
  ```bash
  wpctl status | grep -E "Headphones|Speakers|Both"
  ```
* **Run unit tests:**
  ```bash
  python3 -m unittest discover -s tests -v
  ```
* **Complete Uninstallation:**
  ```bash
  ./uninstall.sh
  ```