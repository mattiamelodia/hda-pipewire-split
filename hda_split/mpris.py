import re
import shutil
from typing import Dict

from .config import clean_val, run_cmd

MPRIS_PLAYER_REGEX = re.compile(r'string "org\.mpris\.MediaPlayer2\.([^"]+)"')
MPRIS_TITLE_REGEX = re.compile(
    r'dict entry\(\s*string "xesam:title"\s*variant\s*string "([^"]+)"'
)


def get_mpris_tab_titles() -> Dict[str, str]:
    """
    Retrieves the real titles of playing tabs/videos via MPRIS D-Bus.
    Works with Chrome, Brave, Firefox, Spotify, VLC, etc.
    """
    titles: Dict[str, str] = {}

    if shutil.which("playerctl"):
        out = run_cmd(["playerctl", "-a", "metadata", "--format", "{{playerName}}: {{title}}"])
        for raw_line in out.splitlines():
            line = clean_val(raw_line)
            if ":" in line:
                player, title = line.split(":", 1)
                title = title.strip()
                if title:
                    titles[player.lower()] = title

    if not titles:
        raw_services = run_cmd([
            "dbus-send",
            "--session",
            "--dest=org.freedesktop.DBus",
            "--type=method_call",
            "--print-reply",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus.ListNames",
        ])
        for match in MPRIS_PLAYER_REGEX.finditer(raw_services):
            player_id = match.group(1)
            bus_name = f"org.mpris.MediaPlayer2.{player_id}"
            meta_raw = run_cmd([
                "dbus-send",
                "--session",
                f"--dest={bus_name}",
                "--type=method_call",
                "--print-reply",
                "/org/mpris/MediaPlayer2",
                "org.freedesktop.DBus.Properties.Get",
                "string:org.mpris.MediaPlayer2.Player",
                "string:Metadata",
            ])
            title_match = MPRIS_TITLE_REGEX.search(meta_raw)
            if title_match:
                titles[player_id.lower()] = title_match.group(1).strip()

    return titles