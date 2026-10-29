import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List

# --- Paths & Constants ---
CONFIG_DIR = Path.home() / ".config" / "pipewire-split"
CONFIG_FILE = CONFIG_DIR / "config.env"
PINS_FILE = CONFIG_DIR / "pinned_apps.json"

PW_CONF_DIR = Path.home() / ".config" / "pipewire" / "pipewire.conf.d"
PW_DROPIN_FILE = PW_CONF_DIR / "99-hda-split.conf"

ICON_PATH = Path.home() / ".local" / "share" / "hda-pipewire-split" / "icon.png"
if not ICON_PATH.exists():
    ICON_PATH = Path(__file__).resolve().parent.parent / "icon.png"

DEFAULT_CONFIG: Dict[str, str] = {
    "ALSA_CARD_ID": "0",
    "CARD_NAME": "alsa_card.pci-0000_00_1f.3",
    "NODE_TARGET": "alsa_output.pci-0000_00_1f.3.pro-output-0",
    "HP_PORT_L": "playback_AUX0",
    "HP_PORT_R": "playback_AUX1",
    "SPK_PORT_L": "playback_AUX2",
    "SPK_PORT_R": "playback_AUX3",
}


# --- Shell & String Utilities ---
def run_cmd(cmd_args: List[str]) -> str:
    """Executes a shell command and returns its stripped stdout, or empty string on failure."""
    try:
        return subprocess.check_output(cmd_args, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def clean_val(val: str) -> str:
    """Strips whitespace and surrounding quotes from a string value."""
    if not val:
        return ""
    return val.strip().strip('"').strip("'").strip()


# --- System Notifications ---
def send_notification(title: str, message: str, urgency: str = "normal") -> None:
    """Sends a system notification with an icon."""
    try:
        subprocess.run(
            [
                "notify-send",
                "-a", "Audio Router",
                "-i", str(ICON_PATH),
                "-u", urgency,
                title,
                message,
            ],
            check=True,
        )
    except Exception as e:
        print(f"Notification error: {e}")


def notify_jack_plugged(callback_action: Callable[[str], None]) -> None:
    """Sends the interactive headphone plugged notification and returns the chosen action via callback."""
    def run_notification() -> None:
        try:
            result = subprocess.run(
                [
                    "notify-send",
                    "-a", "Audio Router",
                    "🎧 Headphones Plugged In",
                    "Where do you want to route the audio?",
                    f"--icon={ICON_PATH}",
                    "--action=headphones=Headphones Only",
                    "--action=speakers=Speakers Only",
                    "--action=both=Both (Split)",
                    "--wait",
                ],
                capture_output=True,
                text=True,
            )
            action = result.stdout.strip()
            if action in ("headphones", "speakers", "both"):
                callback_action(action)
        except Exception as e:
            print(f"Interactive notification error: {e}")

    threading.Thread(target=run_notification, daemon=True).start()


# --- Hardware Discovery ---
def discover_hda_cards() -> List[Dict[str, Any]]:
    """Discovers available ALSA sound cards and identifies analog HDA devices."""
    cards: List[Dict[str, Any]] = []
    asound = Path("/proc/asound")
    if not asound.exists():
        return cards

    card_dirs = sorted(
        asound.glob("card[0-9]*"),
        key=lambda p: int(p.name.replace("card", "")),
    )
    cards_txt = (asound / "cards").read_text() if (asound / "cards").exists() else ""

    for c_dir in card_dirs:
        c_id = c_dir.name.replace("card", "")
        id_file = c_dir / "id"
        c_short = id_file.read_text().strip() if id_file.exists() else f"card{c_id}"

        c_long = c_short
        for line in cards_txt.splitlines():
            if line.strip().startswith(f"{c_id} ["):
                parts = line.split(":", 1)
                if len(parts) > 1:
                    c_long = parts[1].strip()
                break

        codecs: List[str] = []
        is_analog_hda = False
        for cf in c_dir.glob("codec#*"):
            try:
                content = cf.read_text()
                first_line = content.splitlines()[0].replace("Codec: ", "").strip()
                codecs.append(first_line)
                if not any(
                    gpu_tag in first_line.lower()
                    for gpu_tag in ("hdmi", "displayport", "nvidia", "atihdmi")
                ):
                    is_analog_hda = True
            except Exception:
                pass

        if not is_analog_hda:
            scontrols = run_cmd(["amixer", "-c", c_id, "scontrols"]).lower()
            if "headphone" in scontrols or "speaker" in scontrols:
                is_analog_hda = True

        device_link = Path(f"/sys/class/sound/card{c_id}/device")
        pci_raw = os.path.basename(os.path.realpath(device_link)) if device_link.exists() else ""
        pci_id = pci_raw.replace(":", "_") if pci_raw else ""

        card_name = f"alsa_card.pci-{pci_id}" if pci_id else f"alsa_card.hw_{c_id}"
        node_target = (
            f"alsa_output.pci-{pci_id}.pro-output-0"
            if pci_id
            else f"alsa_output.hw_{c_id}.pro-output-0"
        )

        cards.append({
            "id": c_id,
            "short": c_short,
            "long": c_long,
            "codecs": ", ".join(codecs) if codecs else "Generic ALSA",
            "is_analog_hda": is_analog_hda,
            "pci_id": pci_id,
            "card_name": card_name,
            "node_target": node_target,
        })

    cards.sort(key=lambda x: (not x["is_analog_hda"], int(x["id"])))
    return cards


# --- Configuration & Persistence ---
def load_config() -> Dict[str, str]:
    """Loads configuration from disk or falls back to hardware discovery and defaults."""
    cfg: Dict[str, str] = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = clean_val(v)

    if not cfg.get("ALSA_CARD_ID") or not cfg.get("NODE_TARGET"):
        cards = discover_hda_cards()
        if cards:
            best = cards[0]
            cfg.setdefault("ALSA_CARD_ID", best["id"])
            cfg.setdefault("PCI_ID", best["pci_id"])
            cfg.setdefault("CARD_NAME", best["card_name"])
            cfg.setdefault("NODE_TARGET", best["node_target"])

    for key, default_val in DEFAULT_CONFIG.items():
        cfg.setdefault(key, default_val)

    return cfg


def write_pipewire_dropin(cfg: Dict[str, str]) -> None:
    """
    Generates 99-hda-split.conf with 3 independent sinks:
    - Headphones (AUX0/AUX1)
    - Internal Speakers (AUX2/AUX3)
    - Both (Isolated combined sink via module-combine-stream)
    """
    PW_CONF_DIR.mkdir(parents=True, exist_ok=True)

    target_node = cfg["NODE_TARGET"]
    hp_l = cfg.get("HP_PORT_L", "playback_AUX0").replace("playback_", "")
    hp_r = cfg.get("HP_PORT_R", "playback_AUX1").replace("playback_", "")
    spk_l = cfg.get("SPK_PORT_L", "playback_AUX2").replace("playback_", "")
    spk_r = cfg.get("SPK_PORT_R", "playback_AUX3").replace("playback_", "")

    dropin_content = f"""# Auto-generated by HDA PipeWire Split
    context.modules = [
        {{
            name = libpipewire-module-loopback
            args = {{
                node.description = "Headphones (Jack)"
                capture.props = {{
                    media.class = "Audio/Sink"
                    node.name = "Headphones"
                    audio.position = [ FL FR ]
                }}
                playback.props = {{
                    node.name = "playback.Headphones"
                    target.object = "{target_node}"
                    audio.position = [ {hp_l} {hp_r} ]
                    stream.dont-remix = true
                    node.passive = true
                    node.dont-reconnect = true
                    node.dont-move = true
                }}
            }}
        }}
        {{
            name = libpipewire-module-loopback
            args = {{
                node.description = "Internal Speakers"
                capture.props = {{
                    media.class = "Audio/Sink"
                    node.name = "Speakers"
                    audio.position = [ FL FR ]
                }}
                playback.props = {{
                    node.name = "playback.Speakers"
                    target.object = "{target_node}"
                    audio.position = [ {spk_l} {spk_r} ]
                    stream.dont-remix = true
                    node.passive = true
                    node.dont-reconnect = true
                    node.dont-move = true
                }}
            }}
        }}
        {{
            name = libpipewire-module-combine-stream
            args = {{
                combine.mode = sink
                node.name = "Both"
                node.description = "Simultaneous Output (Both)"
                combine.props = {{
                    audio.position = [ FL FR ]
                }}
                stream.rules = [
                    {{
                        matches = [
                            {{ media.class = "Audio/Sink" node.name = "Headphones" }}
                            {{ media.class = "Audio/Sink" node.name = "Speakers" }}
                        ]
                        actions = {{ create-stream = {{}} }}
                    }}
                ]
            }}
        }}
    ]
    """
    PW_DROPIN_FILE.write_text(dropin_content)


def save_config(cfg_dict: Dict[str, str]) -> None:
    """Saves configuration variables to disk and updates the PipeWire drop-in file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Auto-generated by HDA PipeWire Split Wizard"]
    for k, v in cfg_dict.items():
        lines.append(f'{k}="{v}"')
    CONFIG_FILE.write_text("\n".join(lines) + "\n")
    write_pipewire_dropin(cfg_dict)


def load_pinned_apps() -> Dict[str, str]:
    """Loads persistent per-application rules { 'app_key': 'Headphones' | 'Speakers' | 'Both' }."""
    if PINS_FILE.exists():
        try:
            data = json.loads(PINS_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_pinned_apps(pins_dict: Dict[str, str]) -> None:
    """Saves persistent per-application routing rules to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PINS_FILE.write_text(json.dumps(pins_dict, indent=2) + "\n")