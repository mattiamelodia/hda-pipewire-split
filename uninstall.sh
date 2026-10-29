#!/usr/bin/env bash
set -euo pipefail

# --- Paths & Constants ---
CONFIG_DIR="$HOME/.config/pipewire-split"
PW_DROPIN_FILE="$HOME/.config/pipewire/pipewire.conf.d/99-hda-split.conf"
APP_SHARE="$HOME/.local/share/hda-pipewire-split"
BIN_TARGET="$HOME/.local/bin/hda-audio-control"
DESKTOP_FILE="$HOME/.local/share/applications/hda-audio-control.desktop"

echo "=========================================================="
echo "    Universal HDA PipeWire Audio Hub - Uninstaller"
echo "=========================================================="

echo "==> Stopping running instances..."
pkill -f "hda-audio-control" 2>/dev/null || true

echo "==> Removing application files and directories..."
rm -rf "$APP_SHARE"
rm -f "$BIN_TARGET" "$DESKTOP_FILE" "$PW_DROPIN_FILE"

echo "==> Do you want to remove configuration and pinned apps as well? ($CONFIG_DIR)"
read -p "Remove config files? [y/N] " -n 1 -r
echo
if [[ "${REPLY:-}" =~ ^[Yy]$ ]]; then
    rm -rf "$CONFIG_DIR"
    echo "[+] Configuration directory removed."
fi

echo "==> Restarting PipeWire services..."
systemctl --user restart pipewire wireplumber pipewire-pulse 2>/dev/null || true

echo ""
echo "==> Uninstallation completed successfully!"