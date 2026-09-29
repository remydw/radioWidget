#!/usr/bin/env bash
# RTL2 Radio widget for Dank Material Shell — installer.
# Usage: ./install.sh   (from the repo root)
set -euo pipefail

PLUGIN_ID="rtl2RadioWidget"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${DMS_CONFIG_DIR:-$HOME/.config}"
PLUGIN_DIR="$CONFIG_DIR/DankMaterialShell/plugins/$PLUGIN_ID"
FILES=(RadioWidget.qml BackendDaemon.qml manager.py plugin.json qmldir)

echo "==> RTL2 Radio widget installer"

# 1. Copy plugin files (flat layout — plugin.json paths are repo-root-relative)
mkdir -p "$PLUGIN_DIR"
for f in "${FILES[@]}"; do
    if [ ! -f "$SCRIPT_DIR/$f" ]; then
        echo "ERROR: $f missing — run this script from the repository root" >&2
        exit 1
    fi
    cp "$SCRIPT_DIR/$f" "$PLUGIN_DIR/$f"
done
echo "    Installed to: $PLUGIN_DIR"

# 2. Dependencies: mpv (audio), aiohttp (scraper), jeepney (MPRIS media-tab integration)
apt_deps=()
command -v mpv >/dev/null 2>&1 || apt_deps+=("mpv")
python3 -c "import aiohttp" 2>/dev/null || apt_deps+=("python3-aiohttp")
python3 -c "import jeepney" 2>/dev/null || apt_deps+=("python3-jeepney")
if [ ${#apt_deps[@]} -gt 0 ]; then
    if command -v apt >/dev/null 2>&1; then
        echo "==> Installing dependencies: ${apt_deps[*]}"
        sudo apt install -y "${apt_deps[@]}"
    else
        echo "WARNING: missing ${apt_deps[*]} — install mpv, aiohttp and jeepney with your package manager"
    fi
fi

# 3. Enable the plugin in DMS (rescan so DMS discovers it, then enable)
if command -v dms >/dev/null 2>&1; then
    dms ipc call plugin-scan rescan "$PLUGIN_ID" >/dev/null 2>&1 || true
    if dms ipc call plugins enable "$PLUGIN_ID" >/dev/null 2>&1; then
        echo "==> Plugin enabled in DMS"
    else
        echo "NOTE: could not enable via dms (is DMS running?) — later run:"
        echo "      dms ipc call plugins enable $PLUGIN_ID"
    fi
else
    echo "NOTE: 'dms' CLI not found. Start DMS once, then enable the plugin with:"
    echo "      dms ipc call plugins enable $PLUGIN_ID"
fi

echo
echo "Done. Restart DMS to load the widget:"
echo "    dms restart"
