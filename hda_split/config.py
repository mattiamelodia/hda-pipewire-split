import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("hda_split")

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
CMD_TIMEOUT = 5.0
_PCI_RE = re.compile(r"pci-([0-9a-fA-F]{4}_[0-9a-fA-F]{2}_[0-9a-fA-F]{2}\.[0-9a-fA-F])")
_last_failure: Dict[str, str] = {}


def command_env() -> Dict[str, str]:
    """Environment for subprocesses: forces English output, since pactl text is parsed."""
    return {**os.environ, "LC_ALL": "C.UTF-8"}


def _log_failure(cmd_args: List[str], reason: str) -> None:
    """Logs a command failure once per distinct reason, so polling does not flood the log."""
    key = cmd_args[0] if cmd_args else ""
    msg = f"{' '.join(cmd_args)}: {reason}"
    if _last_failure.get(key) != msg:
        _last_failure[key] = msg
        log.warning("command failed: %s", msg)


def run_cmd(cmd_args: List[str]) -> str:
    """Executes a command and returns its stripped stdout, or empty string on failure."""
    try:
        proc = subprocess.run(
            cmd_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=command_env(),
            timeout=CMD_TIMEOUT,
        )
    except Exception as e:
        _log_failure(cmd_args, f"{type(e).__name__}: {e}")
        return ""
    if proc.returncode != 0:
        _log_failure(cmd_args, f"exit {proc.returncode}: {proc.stderr.strip()[:200]}")
        return ""
    return proc.stdout.strip()


def atomic_write_text(path: Path, text: str) -> None:
    """Writes a file via a temp file in the same directory and os.replace, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
            timeout=CMD_TIMEOUT,
        )
    except Exception as e:
        log.warning("Notification error: %s", e)


# --- Hardware Discovery ---
def _pactl_card_names() -> List[str]:
    """Names of the cards PipeWire currently exposes (column 2 of `pactl list cards short`)."""
    return [ln.split()[1] for ln in run_cmd(["pactl", "list", "cards", "short"]).splitlines() if len(ln.split()) >= 2]


def resolve_pipewire_names(pci_id: str, pw_cards: Optional[List[str]] = None) -> Tuple[str, str]:
    """
    Returns (card_name, node_target) for a PCI device as PipeWire names them.
    SOF laptops expose e.g. alsa_card.pci-0000_00_1f.3-platform-skl_hda_dsp_generic, so the
    live card list is consulted first; the plain computed names are the fallback.
    """
    card_name = f"alsa_card.pci-{pci_id}"
    node_target = f"alsa_output.pci-{pci_id}.pro-output-0"
    if pw_cards is None:
        pw_cards = _pactl_card_names()
    for name in pw_cards:
        if name == card_name or name.startswith(card_name + "-"):
            return name, name.replace("alsa_card.", "alsa_output.", 1) + ".pro-output-0"
    return card_name, node_target


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
    pw_cards = _pactl_card_names()

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

        if pci_id:
            card_name, node_target = resolve_pipewire_names(pci_id, pw_cards)
        else:
            card_name = f"alsa_card.hw_{c_id}"
            node_target = f"alsa_output.hw_{c_id}.pro-output-0"

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


def resolve_card_index(cfg: Dict[str, str]) -> str:
    """Finds the current ALSA card index (it can change between boots) via the configured PCI device."""
    pci_id = cfg.get("PCI_ID", "")
    if not pci_id:
        m = _PCI_RE.search(cfg.get("CARD_NAME", ""))
        pci_id = m.group(1) if m else ""
    if pci_id:
        for card in discover_hda_cards():
            if card["pci_id"] == pci_id:
                return str(card["id"])
    return str(cfg.get("ALSA_CARD_ID") or "0")


def port_channels(node_target: str) -> List[str]:
    """Lists the input port names (e.g. playback_AUX0) of exactly the given PipeWire node."""
    prefix = node_target + ":"
    return [
        line.strip()[len(prefix):]
        for line in run_cmd(["pw-link", "-i"]).splitlines()
        if line.strip().startswith(prefix)
    ]


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


def render_pipewire_dropin(cfg: Dict[str, str]) -> str:
    """
    Renders 99-hda-split.conf with 3 independent sinks:
    - Headphones (AUX0/AUX1)
    - Internal Speakers (AUX2/AUX3)
    - Both (Isolated combined sink via module-combine-stream)
    """
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
    return dropin_content


def write_pipewire_dropin(cfg: Dict[str, str]) -> None:
    """Atomically writes the PipeWire drop-in that defines the Headphones, Speakers and Both sinks."""
    atomic_write_text(PW_DROPIN_FILE, render_pipewire_dropin(cfg))


def save_config(cfg_dict: Dict[str, str]) -> None:
    """Saves configuration variables to disk and updates the PipeWire drop-in file."""
    lines = ["# Auto-generated by HDA PipeWire Split Wizard"]
    for k, v in cfg_dict.items():
        lines.append(f'{k}="{v}"')
    atomic_write_text(CONFIG_FILE, "\n".join(lines) + "\n")
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
    atomic_write_text(PINS_FILE, json.dumps(pins_dict, indent=2) + "\n")