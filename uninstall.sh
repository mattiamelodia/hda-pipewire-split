#!/usr/bin/env bash
set -euo pipefail

# --- Paths & Constants ---
CONFIG_DIR="$HOME/.config/pipewire-split"
CONFIG_FILE="$CONFIG_DIR/config.env"
PW_DROPIN_FILE="$HOME/.config/pipewire/pipewire.conf.d/99-hda-split.conf"
APP_SHARE="$HOME/.local/share/hda-pipewire-split"
BIN_TARGET="$HOME/.local/bin/hda-audio-control"
DESKTOP_FILE="$HOME/.local/share/applications/hda-audio-control.desktop"

warn() { echo "[!] Warning: $*" >&2; }

# Reads KEY from config.env WITHOUT sourcing it (the file is data, not code).
cfg_get() {
    [ -f "$CONFIG_FILE" ] || return 0
    grep -m1 -E "^[[:space:]]*$1[[:space:]]*=" "$CONFIG_FILE" 2>/dev/null \
        | sed -E "s/^[^=]*=//; s/^[[:space:]\"']+//; s/[[:space:]\"']+\$//" || true
}

# Finds the current ALSA card index for a PCI id (e.g. 0000_65_00.6); the index may change.
find_alsa_card() {
    local pci="$1" dev real base c
    [ -n "$pci" ] || return 0
    for dev in /sys/class/sound/card[0-9]*/device; do
        [ -e "$dev" ] || continue
        real="$(readlink -f "$dev")"
        base="$(basename "$real")"
        if [ "${base//:/_}" = "$pci" ]; then
            c="${dev#/sys/class/sound/card}"
            echo "${c%%/*}"
            return 0
        fi
    done
}

echo "=========================================================="
echo "    HDA PipeWire Audio Splitter - Uninstaller"
echo "=========================================================="

# 1. Read what must be restored, before anything is deleted.
CARD_NAME="$(cfg_get CARD_NAME)"
PCI_ID="$(cfg_get PCI_ID)"
ALSA_CARD_ID="$(cfg_get ALSA_CARD_ID)"
ORIGINAL_PROFILE="$(cfg_get ORIGINAL_PROFILE)"
ORIGINAL_AUTOMUTE="$(cfg_get ORIGINAL_AUTOMUTE)"

# Values go to commands as single arguments, but still refuse anything odd.
[[ "$CARD_NAME" =~ ^[A-Za-z0-9._:+-]*$ ]] || { warn "ignoring unexpected CARD_NAME in config"; CARD_NAME=""; }
[[ "$PCI_ID" =~ ^[A-Za-z0-9._:-]*$ ]] || { warn "ignoring unexpected PCI_ID in config"; PCI_ID=""; }
[[ "$ALSA_CARD_ID" =~ ^[0-9]*$ ]] || ALSA_CARD_ID=""
[[ "$ORIGINAL_PROFILE" =~ ^[A-Za-z0-9._:+-]*$ ]] || { warn "ignoring unexpected ORIGINAL_PROFILE in config"; ORIGINAL_PROFILE=""; }
[[ "$ORIGINAL_AUTOMUTE" =~ ^[A-Za-z0-9._+\ -]*$ ]] || { warn "ignoring unexpected ORIGINAL_AUTOMUTE in config"; ORIGINAL_AUTOMUTE=""; }
if [ -z "$CARD_NAME" ] && [ -n "$PCI_ID" ]; then
    CARD_NAME="alsa_card.pci-$PCI_ID"
fi

echo "==> Stopping running instances..."
pkill -f "python3? .*hda-pipewire-split/main\.py" 2>/dev/null || true
sleep 0.5

echo "==> Removing application files and the PipeWire drop-in..."
rm -rf "$APP_SHARE"
rm -f "$BIN_TARGET" "$DESKTOP_FILE" "$PW_DROPIN_FILE"

REMOVE_CONFIG=0
echo "==> Do you want to remove configuration and pinned apps as well? ($CONFIG_DIR)"
if [ -t 0 ]; then
    REPLY=""
    read -r -p "Remove config files? [y/N] " -n 1 REPLY || true
    echo
    if [[ "${REPLY:-}" =~ ^[Yy]$ ]]; then
        REMOVE_CONFIG=1
    fi
else
    echo "    (no terminal: keeping configuration)"
fi

# 2. Restart PipeWire without the drop-in, then restore the card.
echo "==> Restarting PipeWire services..."
systemctl --user restart pipewire wireplumber pipewire-pulse 2>/dev/null || warn "could not restart PipeWire services"

echo "==> Restoring the card profile and Auto-Mute..."
pactl_ready=0
for _ in $(seq 1 20); do
    if pactl info >/dev/null 2>&1 && { [ -z "$CARD_NAME" ] || pactl list cards short 2>/dev/null | grep -qF "$CARD_NAME"; }; then
        pactl_ready=1
        break
    fi
    sleep 0.5
done

if [ "$pactl_ready" -ne 1 ]; then
    warn "PipeWire/pactl (or the card) did not come back in time; profile not restored."
elif [ -z "$CARD_NAME" ]; then
    warn "no CARD_NAME in the config; profile not restored."
else
    restored=""
    candidates=()
    if [ -n "$ORIGINAL_PROFILE" ] && [ "$ORIGINAL_PROFILE" != "pro-audio" ]; then
        candidates+=("$ORIGINAL_PROFILE")
    else
        warn "no original profile recorded; trying common analog profiles."
        candidates+=("output:analog-stereo+input:analog-stereo" "output:analog-stereo")
    fi
    for prof in "${candidates[@]}"; do
        if pactl set-card-profile "$CARD_NAME" "$prof" >/dev/null 2>&1; then
            restored="$prof"
            break
        fi
    done
    if [ -n "$restored" ]; then
        echo "[+] Card profile restored: $restored"
    else
        warn "could not restore a profile on $CARD_NAME; choose one in your sound settings."
    fi
fi

ALSA_IDX="$(find_alsa_card "$PCI_ID")"
if [ -z "$ALSA_IDX" ] && [ -n "$ALSA_CARD_ID" ]; then
    ALSA_IDX="$ALSA_CARD_ID"
    [ -n "$PCI_ID" ] && warn "ALSA card for PCI $PCI_ID not found; falling back to recorded index $ALSA_IDX."
fi

if [ -z "$ALSA_IDX" ]; then
    warn "ALSA card not identified; Auto-Mute not restored."
elif [ -n "$ORIGINAL_AUTOMUTE" ]; then
    if amixer -c "$ALSA_IDX" set "Auto-Mute Mode" "$ORIGINAL_AUTOMUTE" >/dev/null 2>&1; then
        echo "[+] Auto-Mute Mode restored: $ORIGINAL_AUTOMUTE"
    else
        warn "could not restore Auto-Mute Mode on card $ALSA_IDX."
    fi
elif amixer -c "$ALSA_IDX" sget "Auto-Mute Mode" >/dev/null 2>&1; then
    warn "no original Auto-Mute value was recorded (older config), so it was left as is."
    echo "    The wizard disabled it; to re-enable it, run for example:" >&2
    echo "      amixer -c $ALSA_IDX set \"Auto-Mute Mode\" Enabled" >&2
    echo "      (list the choices with: amixer -c $ALSA_IDX sget \"Auto-Mute Mode\")" >&2
fi

if [ "$REMOVE_CONFIG" -eq 1 ]; then
    rm -rf "$CONFIG_DIR"
    echo "[+] Configuration directory removed."
fi

echo ""
echo "==> Uninstallation completed."
echo "    Note: volumes and mute switches (PipeWire and ALSA) were not changed."
