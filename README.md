# RTL2 Radio Widget for Dank Material Shell

A live **RTL2** (French radio) pill for the [Dank Material Shell](https://github.com/DankMaterialShell) status bar: play/pause, mouse-wheel volume, a hover popout with album art, and full MPRIS2 integration so the shell's media tab shows what's playing.

## Features

- **Pill**: play/pause icon (click to toggle) + "RTL2". Scroll the wheel over the pill to change volume (±5/notch, percentage flashes briefly).
- **Hover popout**: album art (RTL2 logo while paused), song title, artist, volume slider.
- **Media tab integration**: the backend advertises itself as an MPRIS2 player (`org.mpris.MediaPlayer2.rtl2`) — DMS's media widget shows the current track/artist/art, and play/pause + wheel volume work from there too.
- **Live metadata**: scraped from rtl2.fr every 30s with cache-busting (the page is CDN-cached) and Europe/Paris date keying (works from any timezone).
- **Self-healing**: backend daemon spawned by the plugin (no systemd unit needed), single-instance lock, mpv watchdog (reconnects after idle disconnects, restarts on crash), volume persisted across restarts.

## Requirements

- Dank Material Shell >= 1.5.0 (bar `hoverPopouts: true` for the hover popout)
- Python 3.10+ with `aiohttp` and `jeepney` (installed automatically by `install.sh`)
- `mpv` (audio output via PipeWire)
- A working audio session (PipeWire recommended)

## Install

### Standard (DMS plugin registry)

This plugin is published to the [DMS plugin registry](https://plugins.danklinux.com/). Install it the standard way:

```bash
dms plugins install rtl2RadioWidget
```

or browse it from **Settings → Plugins → Browse** (`Mod + ,`). DMS fetches the plugin from the registry and enables it; then:

```bash
dms restart
```

### Manual (clone or script)

```bash
# Option A: clone straight into the plugins folder, then restart
git clone <this-repo> ~/.config/DankMaterialShell/plugins/rtl2RadioWidget
dms restart

# Option B: install script (copies the plugin, installs Python deps, enables it)
git clone <this-repo> && cd radioWidget
./install.sh
dms restart
```

> The manual paths need the runtime deps installed: `mpv`, Python `aiohttp` and `jeepney` (the install script handles the Python ones). The registry install handles this automatically once the plugin is approved.

## Usage

| Action | Result |
| --- | --- |
| Click pill | Play / pause |
| Wheel over pill | Volume up / down |
| Hover pill | Popout: art, title, artist, volume slider |
| Media tab | Shows the current track; play/pause and volume work |

The volume level is persisted and restored on restart. The radio always starts **paused** (no surprise audio at login).

## How it works

```
┌────────────┐  settings file   ┌──────────────┐
│ RadioWidget│◄───────────────►│  BackendDaemon│  (spawned by DMS while
│  (QML pill)│  plugin_settings │  (daemon)     │   the plugin is enabled)
└────────────┘      .json       └──────┬───────┘
                                       │ spawns + supervises
                              ┌────────▼────────┐
                              │   manager.py     │  mpv (audio, minimal
                              │  - scraper (30s) │  buffer, watchdog)
                              │  - MPRIS2 D-Bus  │  → media tab
                              │  - /tmp socket   │  → debugging
                              └──────────────────┘
```

- The widget reads/writes `~/.config/DankMaterialShell/plugin_settings.json` directly (DMS never re-parses backend writes into its in-memory `pluginData`), and the backend's 1s poll applies `action` (toggle) and `volume` changes.
- The daemon surface (`BackendDaemon.qml`) is DMS's autostart hook: it spawns `manager.py --daemon-spawned`, registers over the socket (`HELLO`), and shuts it down cleanly on unload/quit (`QUIT`).
- `manager.py` owns: the scraper, mpv, the volume store, the MPRIS player, a `flock` single-instance lock, and a health watchdog (stream EOF after idle → reload; mpv crash → restart; missing mpv at boot → retry).

### Socket commands (debugging)

`/tmp/rtl2_radio.sock`: `PLAY`, `PAUSE`, `TOGGLE`, `STATUS`, `METADATA`, `VOLUME[:<0-100>]`, `HELLO`, `QUIT`.

Logs: backend → `/tmp/rtl2_backend.log`.

## Troubleshooting

- **No audio after idle**: the stream edge drops idle connections; the watchdog reloads the stream within ~1s of pressing play (or restarts mpv if it died).
- **Stale title**: scraping is cache-busted and timezone-corrected — if it ever lags, check `manager.py`'s scrape URL.
- **Media tab empty**: the MPRIS name registers on the session bus; a `dms restart` re-registers everything.
- **Ads at start**: RTL2 airs commercial breaks as part of the broadcast; the metadata page keeps showing the last track during them.

## License

MIT — see [LICENSE](LICENSE).

## Publishing (maintainers)

This plugin is registered in the [DMS plugin registry](https://github.com/AvengeMedia/dms-plugin-registry) via a pull request. To (re)publish after changes, fork the registry repo and add/update `plugins/remy-rtl2-radio.json`:

```json
{
    "id": "rtl2RadioWidget",
    "name": "RTL2 Radio",
    "capabilities": ["dankbar-widget", "daemon", "audio"],
    "category": "media",
    "repo": "https://github.com/remydw/radioWidget",
    "author": "Remy",
    "description": "Live RTL2 radio with album art and media-tab integration",
    "dependencies": ["mpv", "python3-aiohttp", "python3-jeepney"],
    "compositors": ["niri", "hyprland"],
    "distro": ["any"],
    "screenshot": "https://raw.githubusercontent.com/remydw/radioWidget/main/screenshot.png"
}
```

Validate locally before the PR (see the registry's `CONTRIBUTING.md` for the validation commands). The `id` and `name` must exactly match this repo's `plugin.json`.
