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

# 2. Python dependencies (aiohttp = scraper, jeepney = MPRIS media-tab integration)
python_deps=()
python3 -c "import aiohttp" 2>/dev/null || python_deps+=("aiohttp")
python3 -c "import jeepney" 2>/dev/null || python_deps+=("jeepney")
if [ ${#python_deps[@]} -gt 0 ]; then
    echo "==> Installing Python dependencies: ${python_deps[*]}"
    pip3 install --break-system-packages "${python_deps[@]}" 2>/dev/null \
        || pip3 install "${python_deps[@]}" 2>/dev/null \
        || echo "WARNING: could not pip install ${python_deps[*]} — install them manually"
fi

# 3. mpv (audio playback)
if ! command -v mpv >/dev/null 2>&1; then
    echo "WARNING: mpv not found. Install it, e.g.:  sudo apt install mpv"
fi

# 4. Enable the plugin in DMS (rescan so DMS discovers it, then enable)
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
