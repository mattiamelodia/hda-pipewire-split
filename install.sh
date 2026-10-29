#!/usr/bin/env bash
set -euo pipefail

# --- Paths & Constants ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/pipewire-split"
CONFIG_FILE="$CONFIG_DIR/config.env"
APP_SHARE="$HOME/.local/share/hda-pipewire-split"
BIN_DIR="$HOME/.local/bin"
BIN_TARGET="$BIN_DIR/hda-audio-control"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_TARGET="$DESKTOP_DIR/hda-audio-control.desktop"

REQUIRED_CMDS=("pactl" "pw-loopback" "pw-link" "amixer" "alsactl" "python3")

echo "=========================================================="
echo "    Universal HDA PipeWire Audio Hub - Installer (v1.0)"
echo "=========================================================="

# 1. Check essential dependencies
missing_deps=()
for cmd in "${REQUIRED_CMDS[@]}"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        missing_deps+=("$cmd")
    fi
done

if [ "${#missing_deps[@]}" -gt 0 ]; then
    echo "[-] Error: Required commands not found: ${missing_deps[*]}" >&2
    echo "    Make sure you have installed: pipewire, wireplumber, pipewire-pulse, alsa-utils." >&2
    exit 1
fi

if ! python3 -c "import PyQt6" >/dev/null 2>&1; then
    echo "[-] Error: Python module 'PyQt6' not found." >&2
    echo "    Install the system package (e.g. 'sudo pacman -S python-pyqt6' or 'sudo apt install python3-pyqt6')." >&2
    exit 1
fi

# 2. Copy modules, scripts, and custom icon
echo "==> Installing modules and assets to $APP_SHARE..."
mkdir -p "$APP_SHARE" "$BIN_DIR" "$DESKTOP_DIR" "$CONFIG_DIR"
cp -rf "$SCRIPT_DIR/hda_split" "$SCRIPT_DIR/main.py" "$SCRIPT_DIR/wizard.py" "$SCRIPT_DIR/icon.png" "$APP_SHARE/"
chmod +x "$APP_SHARE/main.py" "$APP_SHARE/wizard.py"

# 3. Create binary wrapper in ~/.local/bin
cat << 'EOF' > "$BIN_TARGET"
#!/usr/bin/env bash
exec python3 "$HOME/.local/share/hda-pipewire-split/main.py" "$@"
EOF
chmod +x "$BIN_TARGET"

# 4. Calibration and PipeWire configuration if not present
if [ ! -f "$CONFIG_FILE" ]; then
    echo "==> Calibration file not found. Starting wizard automatically..."
    "$BIN_TARGET" --wizard
else
    PYTHONPATH="$APP_SHARE" python3 -c "from hda_split.config import load_config, write_pipewire_dropin; write_pipewire_dropin(load_config())"
    systemctl --user restart pipewire 2>/dev/null || true
fi

# 5. Desktop launcher configuration
echo "==> Configuring desktop launcher..."
sed -e "s|^Exec=.*|Exec=$BIN_TARGET|g" \
    -e "s|^Icon=.*|Icon=$APP_SHARE/icon.png|g" \
    "$SCRIPT_DIR/hda-audio-control.desktop" > "$DESKTOP_TARGET"
chmod +x "$DESKTOP_TARGET"

echo ""
echo "==> Installation completed successfully!"
echo "    - Open the GUI with: hda-audio-control"
echo "    - Start minimized in the Tray: hda-audio-control --tray"
echo "    - CLI shortcuts: --force-hp, --force-spk, --force-both, --toggle-mute"