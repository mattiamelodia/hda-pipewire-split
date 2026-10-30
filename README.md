# HDA PipeWire Splitter & Per-Application Router

A Linux tool for laptops with a **Realtek HDA** codec. It splits the card's 4-channel `pro-audio` PipeWire node into three virtual sinks, **Headphones**, **Speakers** and **Both**, so the 3.5mm jack and the internal speakers can play at the same time, and a small PyQt6 app routes each application's audio to one of them.

It only works if your codec/PCM exposes four independent output channels in `pro-audio` (see [Hardware compatibility](#hardware-compatibility)).

---

## The Problem

On many laptops with a shared HDA codec:

1. **Mutually exclusive outputs**: plugging in headphones triggers ALSA `Auto-Mute`, which silences the internal speakers.
2. **One combined sink**: PipeWire/WirePlumber exposes a single analog output, so you cannot play different streams on the jack and the speakers. The raw `pro-audio` profile exposes the channels separately (`AUX0`-`AUX3`), but not as usable sinks.
3. **No per-application routing** in common mixers for this use case (e.g. a call on the headphones while music plays on the speakers).

---

## How It Works

Two independent parts:

* **The sinks** are a native PipeWire drop-in, `~/.config/pipewire/pipewire.conf.d/99-hda-split.conf` (two `libpipewire-module-loopback` instances plus one `libpipewire-module-combine-stream`). PipeWire loads it by itself, so the sinks keep existing and working **without the app running**.
* **The app** (`hda-audio-control`) is a normal GUI/tray process. It calls `pactl`, `pw-link` and `amixer`, listens to `pactl subscribe` and `alsactl monitor` for stream and jack events, and moves streams between the sinks.

```text
 [ 3.5mm jack ]
       │ alsactl monitor
       ▼
┌────────────────────────────────────────────┐
│          hda-audio-control (Python)        │
│  - jack state, Auto-Mute handling          │
│  - stream discovery (pactl subscribe)      │
│  - per-stream routing                      │
└───────┬──────────────────────────┬─────────┘
        │ pactl move-sink-input    │
        ▼                          ▼
[ PipeWire drop-in sinks ]
 ├── Headphones (AUX0/1)
 ├── Speakers   (AUX2/3)
 └── Both       (combine-stream)
```

---

## Features

* **Wizard**: scans `/proc/asound`, picks the analog HDA codec (ignoring GPU HDMI), switches it to `pro-audio`, checks that `AUX0`-`AUX3` all exist, then plays a soft test tone on each pair and asks which output you heard. It saves the mapping only if the answers are consistent.
* **Per-stream routing**: each PipeWire playback stream gets its own row. Clicking *Headphones*, *Speakers* or *Both* moves that stream immediately, and it stays there until it closes. The choice is not saved: when an app closes or recreates its stream, the stream starts over.
* **Browser tabs**: routing is per PipeWire stream. Some browsers expose one stream per playing tab, others share one stream between tabs, so per-tab control depends on the browser. Tab titles come from MPRIS (`playerctl` or D-Bus) when available.
* **Jack handling**: audio follows the jack. Plugging in moves streams to Headphones and unplugging moves them to Speakers. Streams you moved by hand stay where you put them; the exception is a stream on Headphones when you unplug, which plays on Speakers until you plug back in.
* **Tray and CLI**: tray icon with jack status, and CLI flags for window-manager keybindings (`--force-hp`, `--force-spk`, `--force-both`, `--toggle-mute`). Launching a second instance raises the existing window.

---

## Hardware compatibility

**Tested on:** the author's laptop only: Realtek ALC285 (PCI `0000:65:00.6`), PipeWire 1.6 + WirePlumber on Arch Linux. `AUX0/AUX1` drive the headphone jack and `AUX2/AUX3` the internal speakers.

**Requirement:** a codec/PCM that exposes **four independent playback channels** in the `pro-audio` profile, with two of them wired to the jack and two to the speakers. The wizard checks that `AUX0`-`AUX3` exist and refuses to write a configuration otherwise. Having four ports does not prove the wiring is independent, which is why the wizard also asks you to listen to both pairs.

Machines that may **not** work: laptops with Intel SOF audio, and codecs where the jack and speakers share a DAC (they cannot be driven separately, whatever PipeWire exposes).

If it works (or fails) on your hardware, please open an issue with your codec (`cat /proc/asound/card*/codec#* | grep Codec`), laptop model and what the wizard reported.

---

## Requirements

* **Audio stack**: `pipewire`, `wireplumber`, `pipewire-pulse` (or `pulseaudio-utils` for `pactl`), `alsa-utils` (`amixer`, `alsactl`); `pw-play` (from the PipeWire tools) for the wizard
* **Runtime**: `python3`, `python-pyqt6`, `libnotify` (`notify-send`)
* **Optional**: `playerctl` (media titles for browser streams)

```bash
# Arch Linux
sudo pacman -S python python-pyqt6 pipewire pipewire-pulse wireplumber alsa-utils libnotify playerctl
# Ubuntu / Debian
sudo apt install python3 python3-pyqt6 pipewire pipewire-pulse pulseaudio-utils alsa-utils libnotify-bin playerctl
# Fedora
sudo dnf install python3 python3-pyqt6 pipewire pipewire-pulseaudio pulseaudio-utils alsa-utils libnotify playerctl
```

---

## Installation

```bash
git clone https://github.com/mattiamelodia/hda-pipewire-split.git
cd hda-pipewire-split
./install.sh
```

Or on Arch, build the package from the repository's `PKGBUILD` (`makepkg -si`).

If `~/.config/pipewire-split/config.env` is missing, `install.sh` runs the wizard:

1. It switches the card to `pro-audio` and disables ALSA Auto-Mute (the previous profile and Auto-Mute value are recorded for the uninstaller).
2. Plug in headphones, then confirm for each pair of channels whether you heard the tone on the headphones, the speakers or nothing. The tone is soft, but the wizard does not change any volume, so lower your headphone volume first.
3. It writes the config and the drop-in, restarts PipeWire and checks that the `Headphones`, `Speakers` and `Both` sinks appear.

---

## Usage

```bash
hda-audio-control            # open the mixer window
hda-audio-control --tray     # start in the system tray
hda-audio-control --wizard   # re-run discovery and calibration
```

```bash
hda-audio-control --force-hp     # route all active streams to Headphones
hda-audio-control --force-spk    # route all active streams to Speakers
hda-audio-control --force-both   # route all active streams to Both
hda-audio-control --toggle-mute  # toggle mute on the internal outputs
```

**Force All** (tray and CLI flags) is an explicit command: it moves every stream and replaces the choices made by hand.

**Both** mirrors a stream to the headphones and speakers through the `Both` combine sink.

### Volume

While the app runs it keeps the card on `pro-audio`: if the profile changes, it switches back and restarts PipeWire. To use another profile, quit the app first.

Audio moves to the headphones as soon as you plug them in, at the level of the **Headphones** slider, so keep that slider at a comfortable level.

Sliders stop at 100%. The app leaves ALSA hardware levels alone with one exception: switching away from an analog profile can leave `Master`, `Headphone` or `Speaker` at exactly 0%, which silences `pro-audio`. Only in that case it first lowers the **Hardware** slider to at most 40%, then sets those controls to 0 dB. Because `pro-audio` routing bypasses the codec's normal mixer, the hardware gain may already be high: **start with the volume low when using headphones**.

If everything reads 100% but sounds quiet, check the **Hardware** slider. It is the `pro-audio` output feeding both sinks, and WirePlumber may start it at 40% (about -24 dB). The app never raises it by itself. Lower the Headphones and Speakers sliders before raising it, because it can make the sound much louder.

The `Both` sink has no slider: the app keeps it at 100% because it only duplicates the signal, so each output plays at the same level as when selected alone. Use the Headphones and Speakers sliders to set the level.

---

## Troubleshooting

```bash
wpctl status | grep -E "Headphones|Speakers|Both"   # are the sinks loaded?
python3 -m unittest discover -s tests -v            # unit tests
```

## Uninstallation

```bash
./uninstall.sh
```

It stops running instances, removes the app, launcher and PipeWire drop-in, restarts PipeWire and, when recorded in the config, restores the card's original profile and Auto-Mute value. Configs written by older versions have no recorded values; in that case it tries a standard analog profile and prints the manual `amixer` command. Volumes are not changed. Your configuration is kept unless you answer `y` (or when there is no terminal).

---

## Project layout

```text
hda-pipewire-split/
├── hda_split/
│   ├── config.py       # discovery, config file, PipeWire drop-in generator
│   ├── engine.py       # ALSA/PipeWire commands, routing
│   ├── monitor.py      # jack and PulseAudio event threads
│   ├── mpris.py        # media title lookup (playerctl / D-Bus)
│   └── gui.py          # PyQt6 window and tray
├── main.py             # entry point (--tray, --wizard, --force-*)
├── wizard.py           # discovery and channel calibration
├── hda-audio-control.desktop
├── install.sh / uninstall.sh
├── PKGBUILD
└── tests/
```

## License

MIT, see [LICENSE](LICENSE).
