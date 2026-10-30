import logging
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from .config import (
    CMD_TIMEOUT,
    PW_DROPIN_FILE,
    clean_val,
    command_env,
    load_config,
    render_pipewire_dropin,
    resolve_card_index,
    restore_silenced_levels,
    run_cmd,
    write_pipewire_dropin,
)
from .mpris import get_mpris_tab_titles

log = logging.getLogger("hda_split")

PERCENT_RE = re.compile(r"(\d+)%")

# Browsers sharing the Chromium MPRIS/audio conventions; their player ids are interchangeable.
CHROMIUM_FAMILY = {
    "chrome", "chromium", "googlechrome", "chromiumbrowser", "brave", "bravebrowser", "bravebeta",
    "vivaldi", "vivaldistable", "opera", "microsoftedge", "msedge",
}


class AudioEngine:
    """Manages ALSA hardware states, PipeWire loopback links, sinks, and stream routing."""

    INTERNAL_SINKS = ("Headphones", "Speakers", "Both")
    # Playback-side loopback nodes written by the drop-in, per sink name (and their sink node name).
    LOOPBACK_NODES = {"Headphones": "playback.Headphones", "Speakers": "playback.Speakers"}
    # node.name / node.group prefixes of our own loopback and combine-stream plumbing.
    INTERNAL_NODE_PREFIXES = ("playback.headphones", "playback.speakers", "output.", "loopback-")
    INTERNAL_GROUP_PREFIXES = ("pw-loopback-", "loopback-", "combine-sink-")
    RESTART_COOLDOWN = 30.0
    RECOVERY_GRACE = 3.0
    RECOVERY_MAX_ATTEMPTS = 3
    PROFILE_SAVE_DELAY = 1.5

    def __init__(self) -> None:
        # Streams the user moved by hand (stream id -> sink); they stay put across jack events until they close.
        self.manual_routes: Dict[str, str] = {}
        self.cfg: Dict[str, str] = {}
        self.card_id: str = "0"
        self.card_name: str = ""
        self.target_node: str = ""
        self.jack_numid: Optional[str] = None
        self._last_restart: float = 0.0
        self._sinks_missing_since: Optional[float] = None
        self._recovery_attempts: int = 0

        self.reload_configuration()

    # --- Internal Subprocess Helpers ---
    @staticmethod
    def _run_silent(cmd: List[str]) -> bool:
        """Runs a command to completion (bounded by a timeout); logs failures. Returns True on exit code 0."""
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=command_env(),
                timeout=CMD_TIMEOUT,
            )
        except Exception as e:
            log.warning("command failed: %s: %s: %s", " ".join(cmd), type(e).__name__, e)
            return False
        if proc.returncode != 0:
            log.warning("command exited %d: %s: %s", proc.returncode, " ".join(cmd), proc.stderr.strip()[:200])
            return False
        return True

    @staticmethod
    def _parse_percent(line: str) -> Optional[int]:
        """Extracts the first volume percentage from a pactl 'Volume:' line."""
        m = PERCENT_RE.search(line)
        return int(m.group(1)) if m else None

    # --- Configuration & Hardware Initialization ---
    def reload_configuration(self) -> None:
        self.cfg = load_config()
        self.card_id = resolve_card_index(self.cfg)
        self.card_name = self.cfg.get("CARD_NAME", "")
        self.target_node = self.cfg.get("NODE_TARGET", "")
        self.jack_numid = self._find_jack_numid()

        if not PW_DROPIN_FILE.exists():
            write_pipewire_dropin(self.cfg)

    def _find_jack_numid(self) -> Optional[str]:
        out = run_cmd(["amixer", "-c", self.card_id, "controls"])
        for line in out.splitlines():
            if "name='Headphone Jack'" in line or ("Headphone" in line and "Jack" in line):
                for part in line.split(","):
                    if part.startswith("numid="):
                        return part.split("=")[1].strip()
        return None

    def is_headphone_plugged(self) -> bool:
        target = f"numid={self.jack_numid}" if self.jack_numid else "iface=CARD,name=Headphone Jack"
        out = run_cmd(["amixer", "-c", self.card_id, "cget", target])
        return "values=on" in out.lower()

    def ensure_hardware_ready(self) -> None:
        """Selects the pro-audio profile, disables Auto-Mute, unmutes the amps and rescues levels stuck at 0%."""
        if self.card_name:
            self._run_silent(["pactl", "set-card-profile", self.card_name, "pro-audio"])

        controls = run_cmd(["amixer", "-c", self.card_id, "controls"])
        if "name='Auto-Mute Mode'" in controls:
            if not self._run_silent(["amixer", "-c", self.card_id, "set", "Auto-Mute Mode", "Disabled"]):
                self._run_silent(["amixer", "-c", self.card_id, "set", "Auto-Mute Mode", "off"])

        scontrols = run_cmd(["amixer", "-c", self.card_id, "scontrols"])
        for ctrl in ("Master", "Speaker", "Headphone", "Bass Speaker"):
            if f"'{ctrl}'," in scontrols:
                self._run_silent(["amixer", "-c", self.card_id, "set", ctrl, "unmute"])

        restore_silenced_levels(self.card_id, self.target_node)

    def _sink_names(self) -> List[str]:
        """Exact sink names from column 2 of `pactl list sinks short`."""
        names: List[str] = []
        for line in run_cmd(["pactl", "list", "sinks", "short"]).splitlines():
            parts = line.split()
            if len(parts) >= 2:
                names.append(parts[1])
        return names

    def _internal_sinks_present(self) -> bool:
        names = set(self._sink_names())
        return all(s in names for s in self.INTERNAL_SINKS)

    def _card_is_listed(self) -> bool:
        cards = [line.split()[1] for line in run_cmd(["pactl", "list", "cards", "short"]).splitlines() if len(line.split()) >= 2]
        return not self.card_name or self.card_name in cards

    def _wait_for_sinks(self, timeout: float = 5.0, step: float = 0.25) -> bool:
        """Waits for the sinks and for the card: a profile request sent before it is listed fails."""
        deadline = time.monotonic() + timeout
        while True:
            if self._internal_sinks_present() and self._card_is_listed():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(step)

    def _wait_for_target_node(self, steps: int = 8, step: float = 0.25) -> None:
        for _ in range(steps):
            if self.target_node in self._sink_names():
                return
            time.sleep(step)

    def ensure_sinks_exist(self, force_reload: bool = False) -> bool:
        """Makes sure the Headphones/Speakers/Both sinks exist. Returns True if all three are present at the end."""
        if not run_cmd(["pactl", "info"]):
            log.warning("pactl is unreachable (PipeWire/pipewire-pulse not running?); not restarting anything")
            return False

        if not PW_DROPIN_FILE.exists() or PW_DROPIN_FILE.read_text() != render_pipewire_dropin(self.cfg):
            write_pipewire_dropin(self.cfg)
            force_reload = True

        if not force_reload and not self._internal_sinks_present():
            if time.monotonic() - self._last_restart < self.RESTART_COOLDOWN:
                log.warning("Sinks are missing but PipeWire was restarted recently; not restarting again")
            else:
                force_reload = True

        if force_reload:
            self._last_restart = time.monotonic()
            self._run_silent(["systemctl", "--user", "restart", "pipewire"])
            if not self._wait_for_sinks():
                log.warning("Headphones/Speakers/Both sinks did not appear within 5s of restarting PipeWire")
            self.ensure_hardware_ready()
            self._wait_for_target_node()

        # Ensure physical ports are always connected
        self.enforce_hardware_links()
        present = self._internal_sinks_present()
        if present:
            self._set_both_to_unity()
        return present

    @property
    def recovery_exhausted(self) -> bool:
        return self._recovery_attempts >= self.RECOVERY_MAX_ATTEMPTS

    def recover_missing_sinks(self) -> bool:
        """
        Rebuilds the audio path if it stays broken, e.g. after a card profile switch removed the hardware node.
        Bounded by a grace period, the restart cooldown and an attempt limit. Returns True if the path is complete.
        """
        names = set(self._sink_names())
        if all(s in names for s in self.INTERNAL_SINKS) and self.target_node in names:
            self._sinks_missing_since = None
            self._recovery_attempts = 0
            return True
        if not run_cmd(["pactl", "info"]):
            return False

        now = time.monotonic()
        if self._sinks_missing_since is None:
            self._sinks_missing_since = now
            return False
        if (
            now - self._sinks_missing_since < self.RECOVERY_GRACE
            or now - self._last_restart < self.RESTART_COOLDOWN
            or self.recovery_exhausted
        ):
            return False

        self._recovery_attempts += 1
        log.warning(
            "Audio path is broken; restoring pro-audio and restarting PipeWire (attempt %d of %d)",
            self._recovery_attempts,
            self.RECOVERY_MAX_ATTEMPTS,
        )
        # Select pro-audio before the restart and give WirePlumber time to save it, so the restart restores it
        self.ensure_hardware_ready()
        time.sleep(self.PROFILE_SAVE_DELAY)
        return self.ensure_sinks_exist(force_reload=True)

    def _set_both_to_unity(self) -> None:
        """Both only duplicates the signal into Headphones and Speakers, so it must not attenuate it."""
        self.set_sink_volume("Both", 100)

    # --- PipeWire Link Management ---
    def _get_configured_ports(self) -> tuple[str, str, str, str]:
        return (
            self.cfg.get("HP_PORT_L", "playback_AUX0"),
            self.cfg.get("HP_PORT_R", "playback_AUX1"),
            self.cfg.get("SPK_PORT_L", "playback_AUX2"),
            self.cfg.get("SPK_PORT_R", "playback_AUX3"),
        )

    @staticmethod
    def _parse_outgoing_links(text: str) -> Dict[str, List[str]]:
        """Parses `pw-link -l` into {output_port: [input_port, ...]}."""
        links: Dict[str, List[str]] = {}
        current = ""
        for raw in text.splitlines():
            if not raw.strip():
                continue
            if raw[0] not in " \t":
                current = raw.strip()
            elif "|->" in raw and current:
                links.setdefault(current, []).append(raw.split("|->", 1)[1].strip())
        return links

    def _loopback_node_names(self) -> Dict[str, List[str]]:
        """
        Maps each of our sinks to the candidate names of its playback-side loopback node:
        the name set in the drop-in, plus output.<node.group of the sink> which is what
        PipeWire actually calls it on versions that ignore playback.props node.name.
        """
        sinks = self.get_sink_info()
        names: Dict[str, List[str]] = {}
        for sink, node in self.LOOPBACK_NODES.items():
            names[sink] = [node]
            group = sinks.get(sink, {}).get("group", "")
            if group:
                names[sink].append(f"output.{group}")
        return names

    def enforce_hardware_links(self) -> None:
        """
        Makes the loopback nodes of the Headphones and Speakers sinks (and only those) feed the
        configured physical AUX ports: removes stray links (e.g. Speakers -> Headphones) and adds missing ones.
        """
        out_ports = [p.strip() for p in run_cmd(["pw-link", "-o"]).splitlines()]
        if not out_ports:
            return
        links = self._parse_outgoing_links(run_cmd(["pw-link", "-l"]))
        target = self.target_node
        hp_l, hp_r, spk_l, spk_r = self._get_configured_ports()
        wanted = {
            "Headphones": (hp_l, hp_r),
            "Speakers": (spk_l, spk_r),
        }
        node_names = self._loopback_node_names()

        for sink, (left, right) in wanted.items():
            # Channel names an output port of this node may carry, left first.
            sides = (
                (f"output_{left.replace('playback_', '')}", "output_FL", f"{target}:{left}"),
                (f"output_{right.replace('playback_', '')}", "output_FR", f"{target}:{right}"),
            )
            for node in node_names[sink]:
                for port in out_ports:
                    node_part, _, port_part = port.partition(":")
                    if node_part != node:
                        continue
                    for aux_name, std_name, dest in sides:
                        if port_part not in (aux_name, std_name):
                            continue
                        current = links.get(port, [])
                        for stray in current:
                            if stray != dest:
                                self._run_silent(["pw-link", "-d", port, stray])
                        if dest not in current:
                            self._run_silent(["pw-link", port, dest])
                        break

    # --- Routing & Sink Controls ---
    def route_stream(self, stream_id: str, target: str) -> bool:
        """Moves the specific stream to the target sink."""
        return self._run_silent(["pactl", "move-sink-input", str(stream_id), target])

    def set_default_target(self, target: str) -> None:
        """Sets the system default sink without touching ALSA hardware."""
        self._run_silent(["pactl", "set-default-sink", target])

    def _prepare_output(self, target: str) -> None:
        self.set_default_target(target)
        self.ensure_hardware_ready()
        self.enforce_hardware_links()
        if target == "Both":
            self._set_both_to_unity()

    def choose_output(self, stream_id: str, target: str) -> bool:
        """Moves one stream by hand. The choice survives jack events until the stream closes."""
        if not self.route_stream(stream_id, target):
            return False
        self.manual_routes[str(stream_id)] = target
        return True

    def follow_jack(self, plugged: bool) -> int:
        """Audio follows the jack, except streams moved by hand. Returns the number of streams moved."""
        jack_target = "Headphones" if plugged else "Speakers"
        self._prepare_output(jack_target)

        moved = 0
        for s in self.get_streams():
            target = self.manual_routes.get(s["id"], jack_target)
            if target == "Headphones" and not plugged:
                target = "Speakers"  # never leave a stream on an output that is not connected
            if s["sink_name"] != target and self.route_stream(s["id"], target):
                moved += 1
        return moved

    def force_all_streams(self, target: str) -> int:
        """Explicit command: moves every stream to `target` and replaces any choice made by hand."""
        self.manual_routes.clear()
        self._prepare_output(target)

        moved = 0
        for s in self.get_streams():
            if s["sink_name"] != target and self.route_stream(s["id"], target):
                moved += 1
        return moved

    def get_sink_info(self) -> Dict[str, Dict[str, Any]]:
        """Returns volume/mute state of all sinks and lists any external sinks (USB/BT)."""
        out = run_cmd(["pactl", "list", "sinks"])
        sinks: Dict[str, Dict[str, Any]] = {}
        curr: Optional[Dict[str, Any]] = None

        for raw in out.splitlines():
            line = raw.strip()
            if line.startswith("Sink #"):
                if curr and curr.get("name"):
                    sinks[curr["name"]] = curr
                curr = {
                    "id": line.split("#", 1)[1].strip(),
                    "name": "",
                    "description": "",
                    "group": "",
                    "volume": 100,
                    "muted": False,
                }
            elif curr is not None:
                if line.startswith("Name: "):
                    curr["name"] = clean_val(line.split("Name: ", 1)[1])
                elif line.startswith("Description: "):
                    curr["description"] = clean_val(line.split("Description: ", 1)[1])
                elif line.startswith("node.group = "):
                    curr["group"] = clean_val(line.split(" = ", 1)[1])
                elif line.startswith("Mute: "):
                    curr["muted"] = clean_val(line.split("Mute: ", 1)[1]).lower() == "yes"
                elif line.startswith("Volume: ") and "%" in line:
                    vol = self._parse_percent(line)
                    if vol is not None:
                        curr["volume"] = vol

        if curr and curr.get("name"):
            sinks[curr["name"]] = curr
        return sinks

    def get_external_sinks(self) -> List[Dict[str, Any]]:
        """Detects connected Bluetooth headphones or USB DACs, excluding internal pro-output nodes."""
        all_sinks = self.get_sink_info()
        internal_names = {*self.INTERNAL_SINKS, self.target_node}
        return [
            data
            for name, data in all_sinks.items()
            if name not in internal_names and ".pro-output-" not in name
        ]

    @staticmethod
    def _clamp_percent(vol_percent: int) -> int:
        return max(0, min(100, int(vol_percent)))

    def set_sink_volume(self, sink_name: str, vol_percent: int) -> None:
        self._run_silent(["pactl", "set-sink-volume", sink_name, f"{self._clamp_percent(vol_percent)}%"])

    def set_sink_mute(self, sink_name: str, is_muted: bool) -> None:
        self._run_silent(["pactl", "set-sink-mute", sink_name, "1" if is_muted else "0"])

    def set_stream_volume(self, stream_id: str, vol_percent: int) -> None:
        self._run_silent(["pactl", "set-sink-input-volume", str(stream_id), f"{self._clamp_percent(vol_percent)}%"])

    def set_stream_mute(self, stream_id: str, is_muted: bool) -> None:
        self._run_silent(["pactl", "set-sink-input-mute", str(stream_id), "1" if is_muted else "0"])

    def toggle_all_sinks_mute(self) -> bool:
        info = self.get_sink_info()
        hp_muted = info.get("Headphones", {}).get("muted", False)
        new_mute = not hp_muted
        for s in ("Headphones", "Speakers"):
            self.set_sink_mute(s, new_mute)
        return new_mute

    # --- Stream Discovery ---
    @classmethod
    def _is_internal_stream(cls, s: Dict[str, Any]) -> bool:
        """True for PipeWire plumbing (loopback/combine streams, daemon, filters), never judged by title."""
        node_name = s["node_name"].lower()
        if node_name.startswith(cls.INTERNAL_NODE_PREFIXES):
            return True
        if s["node_group"].lower().startswith(cls.INTERNAL_GROUP_PREFIXES):
            return True
        app, binary = s["app"].lower(), s["binary"].lower()
        if app == "pipewire" or binary == "pipewire" or app.startswith("playback."):
            return True
        return s["media_role"].lower() == "filter"

    @staticmethod
    def _normalize_id(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())

    @classmethod
    def _player_matches_stream(cls, player_key: str, stream: Dict[str, Any]) -> bool:
        """Whether an MPRIS player id (e.g. firefox.instance_123) belongs to the stream's own app/binary."""
        player = cls._normalize_id(re.sub(r"\.instance.*$", "", player_key.lower()))
        idents = {i for i in (cls._normalize_id(stream["app"]), cls._normalize_id(stream["binary"])) if i}
        if not player or not idents:
            return False
        if player in CHROMIUM_FAMILY and idents & CHROMIUM_FAMILY:
            return True
        return any(
            player == i or (min(len(player), len(i)) >= 4 and (player.startswith(i) or i.startswith(player)))
            for i in idents
        )

    @classmethod
    def _apply_mpris_titles(cls, streams: List[Dict[str, Any]], titles: Dict[str, str]) -> None:
        """
        Sets media_title from MPRIS only where it is unambiguous: a title is applied to a stream when
        exactly one player matches that stream's own app and that player matches no other stream.
        """
        player_streams: Dict[str, List[int]] = {p: [] for p in titles}
        stream_players: Dict[int, List[str]] = {i: [] for i in range(len(streams))}
        for i, s in enumerate(streams):
            for player in titles:
                if cls._player_matches_stream(player, s):
                    player_streams[player].append(i)
                    stream_players[i].append(player)
        for i, players in stream_players.items():
            if len(players) == 1 and len(player_streams[players[0]]) == 1:
                streams[i]["media_title"] = titles[players[0]]

    def get_streams(self) -> List[Dict[str, Any]]:
        out = run_cmd(["pactl", "list", "sink-inputs"])
        if not out:
            return []

        streams: List[Dict[str, Any]] = []
        curr: Optional[Dict[str, Any]] = None

        prop_map = {
            "application.name": "app",
            "application.process.binary": "binary",
            "media.name": "media_name",
            "media.title": "media_title",
            "window.name": "window_name",
            "media.role": "media_role",
            "application.process.id": "pid",
            "node.name": "node_name",
            "node.group": "node_group",
        }

        for raw_line in out.splitlines():
            line = raw_line.strip()
            if line.startswith("Sink Input #"):
                if curr and curr.get("id"):
                    streams.append(curr)
                curr = {
                    "id": line.split("#", 1)[1].strip(),
                    "sink_id": "",
                    "app": "",
                    "binary": "",
                    "media_name": "",
                    "media_title": "",
                    "window_name": "",
                    "media_role": "",
                    "pid": "",
                    "node_name": "",
                    "node_group": "",
                    "volume": 100,
                    "muted": False,
                }
            elif curr is not None:
                if line.startswith("Sink: "):
                    curr["sink_id"] = clean_val(line.split("Sink: ", 1)[1])
                elif line.startswith("Mute: "):
                    curr["muted"] = clean_val(line.split("Mute: ", 1)[1]).lower() == "yes"
                elif line.startswith("Volume: ") and "%" in line:
                    vol = self._parse_percent(line)
                    if vol is not None:
                        curr["volume"] = vol
                elif " = " in line:
                    k, v = line.split(" = ", 1)
                    k, v = k.strip(), clean_val(v)
                    if k in prop_map:
                        curr[prop_map[k]] = v

        if curr and curr.get("id"):
            streams.append(curr)

        sink_map: Dict[str, str] = {}
        for s_line in run_cmd(["pactl", "list", "sinks", "short"]).splitlines():
            p = s_line.split()
            if len(p) >= 2:
                sink_map[p[0]] = p[1]

        valid_streams: List[Dict[str, Any]] = []
        for s in streams:
            if self._is_internal_stream(s):
                continue
            if not s["app"] and not s["binary"] and not s["pid"]:
                continue
            s["sink_name"] = sink_map.get(s["sink_id"], "")
            valid_streams.append(s)

        open_ids = {s["id"] for s in streams}
        for stream_id in list(self.manual_routes):
            if stream_id not in open_ids:
                del self.manual_routes[stream_id]

        if valid_streams:
            self._apply_mpris_titles(valid_streams, get_mpris_tab_titles())
        return valid_streams
