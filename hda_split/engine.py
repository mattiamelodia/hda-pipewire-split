import subprocess
from typing import Any, Dict, List, Optional

from .config import (
    PW_DROPIN_FILE,
    clean_val,
    load_config,
    load_pinned_apps,
    run_cmd,
    save_pinned_apps,
    write_pipewire_dropin,
)
from .mpris import get_mpris_tab_titles


class AudioEngine:
    """Manages ALSA hardware states, PipeWire loopback links, sinks, and stream routing."""

    INTERNAL_SINKS = ("Headphones", "Speakers", "Both")
    IGNORED_STREAM_KEYWORDS = (
        "loopback",
        "combine",
        "simultaneous",
        "filter",
        "playback.headphones",
        "playback.speakers",
        "output.headphones",
        "output.speakers",
    )

    def __init__(self) -> None:
        self.pinned_apps: Dict[str, str] = load_pinned_apps()
        self.cfg: Dict[str, str] = {}
        self.card_id: str = "0"
        self.card_name: str = ""
        self.target_node: str = ""
        self.jack_numid: Optional[str] = None

        self.reload_configuration()
        self.unload_combine_modules()

    # --- Internal Subprocess Helpers ---
    @staticmethod
    def _run_silent(cmd: List[str]) -> None:
        subprocess.run(cmd, stderr=subprocess.DEVNULL)

    @staticmethod
    def _popen_silent(cmd: List[str]) -> None:
        subprocess.Popen(cmd, stderr=subprocess.DEVNULL)

    # --- Configuration & Hardware Initialization ---
    def reload_configuration(self) -> None:
        self.cfg = load_config()
        self.card_id = str(self.cfg.get("ALSA_CARD_ID", "0"))
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
        if not self.jack_numid:
            out = run_cmd(["amixer", "-c", self.card_id, "cget", "name=Headphone Jack"])
            return "values=on" in out.lower()
        out = run_cmd(["amixer", "-c", self.card_id, "cget", f"numid={self.jack_numid}"])
        return "values=on" in out.lower()

    def unload_combine_modules(self) -> None:
        modules = run_cmd(["pactl", "list", "modules", "short"])
        for line in modules.splitlines():
            if "module-combine-sink" in line:
                mod_id = line.split()[0]
                self._run_silent(["pactl", "unload-module", mod_id])

    def ensure_hardware_ready(self) -> None:
        if self.card_name:
            self._run_silent(["pactl", "set-card-profile", self.card_name, "pro-audio"])

        for am_opt in ("Disabled", "off"):
            self._run_silent(["amixer", "-c", self.card_id, "set", "Auto-Mute Mode", am_opt])

        for ctrl in ("Master", "Speaker", "Headphone", "Bass Speaker"):
            self._run_silent(["amixer", "-c", self.card_id, "set", ctrl, "unmute"])
            self._run_silent(["amixer", "-c", self.card_id, "set", ctrl, "100%"])

        # Always reconnect AUX2/AUX3 ports to hardware and detach any bridges on Headphones!
        self.enforce_hardware_links()

    def ensure_sinks_exist(self, force_reload: bool = False) -> None:
        if not PW_DROPIN_FILE.exists() or "Both" not in PW_DROPIN_FILE.read_text():
            write_pipewire_dropin(self.cfg)
            force_reload = True

        sinks_out = run_cmd(["pactl", "list", "sinks", "short"])
        if force_reload or any(s not in sinks_out for s in self.INTERNAL_SINKS):
            self._run_silent(["systemctl", "--user", "restart", "pipewire"])
            self.ensure_hardware_ready()

        # Ensure physical ports are always connected
        self.enforce_hardware_links()

    # --- PipeWire Link Management ---
    def _get_configured_ports(self) -> tuple[str, str, str, str]:
        return (
            self.cfg.get("HP_PORT_L", "playback_AUX0"),
            self.cfg.get("HP_PORT_R", "playback_AUX1"),
            self.cfg.get("SPK_PORT_L", "playback_AUX2"),
            self.cfg.get("SPK_PORT_R", "playback_AUX3"),
        )

    def enforce_loopback_links(self) -> None:
        target = self.target_node
        hp_l, hp_r, spk_l, spk_r = self._get_configured_ports()

        # Physical connections for Headphones
        self._run_silent(["pw-link", "playback.Headphones:output_FL", f"{target}:{hp_l}"])
        self._run_silent(["pw-link", "playback.Headphones:output_FR", f"{target}:{hp_r}"])

        # Physical connections for Speakers
        self._run_silent(["pw-link", "playback.Speakers:output_FL", f"{target}:{spk_l}"])
        self._run_silent(["pw-link", "playback.Speakers:output_FR", f"{target}:{spk_r}"])

    def enforce_hardware_links(self) -> None:
        """Unlinks parasitic loops (e.g. Speakers -> Headphones) and forces links toward physical AUX ports."""
        out_ports = run_cmd(["pw-link", "-o"]).splitlines()
        target = self.target_node
        hp_l, hp_r, spk_l, spk_r = self._get_configured_ports()

        hp_l_pos = hp_l.replace("playback_", "")    # e.g. AUX0
        hp_r_pos = hp_r.replace("playback_", "")    # e.g. AUX1
        spk_l_pos = spk_l.replace("playback_", "")  # e.g. AUX2
        spk_r_pos = spk_r.replace("playback_", "")  # e.g. AUX3

        for raw_port in out_ports:
            port = raw_port.strip()
            is_hp_node = "Headphones" in port and "Both" not in port
            is_spk_node = "Speakers" in port and "Both" not in port

            # 1. HEADPHONES loopback ports (AUX0 / AUX1)
            if port.endswith(f":output_{hp_l_pos}") or (is_hp_node and port.endswith(":output_FL")):
                self._run_silent(["pw-link", "-d", port, "Speakers:playback_FL"])
                self._run_silent(["pw-link", "-d", port, f"{target}:{spk_l}"])
                self._run_silent(["pw-link", port, f"{target}:{hp_l}"])

            elif port.endswith(f":output_{hp_r_pos}") or (is_hp_node and port.endswith(":output_FR")):
                self._run_silent(["pw-link", "-d", port, "Speakers:playback_FR"])
                self._run_silent(["pw-link", "-d", port, f"{target}:{spk_r}"])
                self._run_silent(["pw-link", port, f"{target}:{hp_r}"])

            # 2. SPEAKERS loopback ports (AUX2 / AUX3) -> Fixed bug where they ended up in Headphones:playback_FL/FR
            elif port.endswith(f":output_{spk_l_pos}") or (is_spk_node and port.endswith(":output_FL")):
                self._run_silent(["pw-link", "-d", port, "Headphones:playback_FL"])
                self._run_silent(["pw-link", "-d", port, "Headphones:playback_FR"])
                self._run_silent(["pw-link", "-d", port, f"{target}:{hp_l}"])
                self._run_silent(["pw-link", port, f"{target}:{spk_l}"])

            elif port.endswith(f":output_{spk_r_pos}") or (is_spk_node and port.endswith(":output_FR")):
                self._run_silent(["pw-link", "-d", port, "Headphones:playback_FL"])
                self._run_silent(["pw-link", "-d", port, "Headphones:playback_FR"])
                self._run_silent(["pw-link", "-d", port, f"{target}:{hp_r}"])
                self._run_silent(["pw-link", port, f"{target}:{spk_r}"])

    # --- Routing & Sink Controls ---
    def apply_hardware_routing_mode(self, target_sink: str) -> None:
        """Synchronizes ALSA controls and PipeWire links with the chosen routing mode."""
        self.ensure_hardware_ready()
        self.enforce_hardware_links()

        if target_sink == "Speakers":
            # Mute the headphone amplifier in ALSA so audio only comes out of the speakers
            self._run_silent(["amixer", "-c", self.card_id, "set", "Headphone", "mute"])
            self._run_silent(["amixer", "-c", self.card_id, "set", "Speaker", "unmute", "100%"])
        elif target_sink == "Headphones":
            self._run_silent(["amixer", "-c", self.card_id, "set", "Headphone", "unmute", "100%"])
            self._run_silent(["amixer", "-c", self.card_id, "set", "Speaker", "mute"])
        else:  # "Both" or mixed routing per individual app
            self._run_silent(["amixer", "-c", self.card_id, "set", "Headphone", "unmute", "100%"])
            self._run_silent(["amixer", "-c", self.card_id, "set", "Speaker", "unmute", "100%"])

    def route_stream(self, stream_id: str, target: str) -> None:
        """Moves the specific stream to the target sink."""
        self._run_silent(["pactl", "move-sink-input", str(stream_id), target])

    def set_default_target(self, target: str) -> None:
        """Sets the system default sink without touching ALSA hardware."""
        self._run_silent(["pactl", "set-default-sink", target])

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
                    "id": line.split("#")[1].strip(),
                    "name": "",
                    "description": "",
                    "volume": 100,
                    "muted": False,
                }
            elif curr is not None:
                if line.startswith("Name: "):
                    curr["name"] = clean_val(line.split("Name: ", 1)[1])
                elif line.startswith("Description: "):
                    curr["description"] = clean_val(line.split("Description: ", 1)[1])
                elif line.startswith("Mute: "):
                    curr["muted"] = clean_val(line.split("Mute: ", 1)[1]).lower() == "yes"
                elif line.startswith("Volume: ") and "%" in line and "Base Volume" not in line:
                    try:
                        curr["volume"] = int(line.split("/")[1].strip().replace("%", ""))
                    except Exception:
                        pass

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

    def set_sink_volume(self, sink_name: str, vol_percent: int) -> None:
        self._popen_silent(["pactl", "set-sink-volume", sink_name, f"{int(vol_percent)}%"])

    def set_sink_mute(self, sink_name: str, is_muted: bool) -> None:
        self._popen_silent(["pactl", "set-sink-mute", sink_name, "1" if is_muted else "0"])

    def set_stream_volume(self, stream_id: str, vol_percent: int) -> None:
        self._popen_silent(["pactl", "set-sink-input-volume", str(stream_id), f"{int(vol_percent)}%"])

    def set_stream_mute(self, stream_id: str, is_muted: bool) -> None:
        self._popen_silent(["pactl", "set-sink-input-mute", str(stream_id), "1" if is_muted else "0"])

    def toggle_all_sinks_mute(self) -> bool:
        info = self.get_sink_info()
        hp_muted = info.get("Headphones", {}).get("muted", False)
        new_mute = not hp_muted
        for s in ("Headphones", "Speakers"):
            self.set_sink_mute(s, new_mute)
        return new_mute

    # --- Application Pinning ---
    @staticmethod
    def get_app_key(stream_data: Dict[str, Any]) -> str:
        return (stream_data.get("binary") or stream_data.get("app") or "").strip().lower()

    def pin_app(self, app_key: str, target_sink: str) -> None:
        if app_key:
            self.pinned_apps[app_key] = target_sink
            save_pinned_apps(self.pinned_apps)

    def unpin_app(self, app_key: str) -> None:
        if app_key and app_key in self.pinned_apps:
            del self.pinned_apps[app_key]
            save_pinned_apps(self.pinned_apps)

    def clear_all_pins(self) -> None:
        self.pinned_apps.clear()
        save_pinned_apps(self.pinned_apps)

    # --- Stream Discovery ---
    def get_streams(self) -> List[Dict[str, Any]]:
        out = run_cmd(["pactl", "list", "sink-inputs"])
        if not out:
            return []

        mpris_titles = get_mpris_tab_titles()
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
        }

        for raw_line in out.splitlines():
            line = raw_line.strip()
            if line.startswith("Sink Input #"):
                if curr and curr.get("id"):
                    streams.append(curr)
                curr = {
                    "id": line.split("#")[1].strip(),
                    "sink_id": "",
                    "app": "",
                    "binary": "",
                    "media_name": "",
                    "media_title": "",
                    "window_name": "",
                    "media_role": "",
                    "pid": "",
                    "volume": 100,
                    "muted": False,
                }
            elif curr is not None:
                if line.startswith("Sink: "):
                    curr["sink_id"] = clean_val(line.split("Sink: ")[1])
                elif line.startswith("Mute: "):
                    curr["muted"] = clean_val(line.split("Mute: ")[1]).lower() == "yes"
                elif line.startswith("Volume: ") and "%" in line:
                    try:
                        curr["volume"] = int(line.split("/")[1].strip().replace("%", ""))
                    except Exception:
                        pass
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
            text_ident = f"{s['app']} {s['media_name']} {s['binary']} {s['media_role']}".lower()
            if any(k in text_ident for k in self.IGNORED_STREAM_KEYWORDS):
                continue
            if not s["app"] and not s["binary"] and not s["pid"]:
                continue

            app_lower = (s["app"] or s["binary"]).lower()
            for m_key, m_title in mpris_titles.items():
                if any(x in m_key for x in (app_lower, "chrom", "brave", "firefox", s["binary"].lower())):
                    s["media_title"] = m_title
                    break

            s["sink_name"] = sink_map.get(s["sink_id"], "")
            s["app_key"] = self.get_app_key(s)
            valid_streams.append(s)

        return valid_streams

    def cleanup(self) -> None:
        self.unload_combine_modules()