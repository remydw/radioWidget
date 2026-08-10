#!/usr/bin/env python3
import socket
import sys
import asyncio
import datetime
import fcntl
import json
import os
import re
import signal
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

# MPRIS2 D-Bus integration (optional)
try:
    from jeepney import DBusAddress, Message, MessageType, HeaderFields, new_method_call, new_method_return, new_error
    from jeepney.io.asyncio import open_dbus_connection
    from jeepney.wrappers import new_header
    HAVE_JEEPNEY = True
except Exception:
    HAVE_JEEPNEY = False

if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
from pathlib import Path
import aiohttp
import html
from typing import Optional, Tuple

# ---- Station config ---------------------------------------------------------

STATIONS = {
    "rtl2": {
        "name": "RTL2",
        "stream_url": "http://icecast.rtl2.fr/rtl2-1-44-128?listen=webCwsBCggNCQgLDQUGBAcGBg",
        "logo_url": "https://static.rtl2.fr/versions/www/7.0.413/img/radios/rtl2.png",
        "fallback_logo": "https://www.rtl2.fr/apple-touch-icon.png",
        "mpris_identity": "RTL2 Radio",
        "metadata_mode": "scrape",   # web scrape rtl2.fr/quel-est-ce-titre
        "scrape_url": "https://www.rtl2.fr/quel-est-ce-titre/{date}",
        "scrape_timezone": "Europe/Paris",
    },
    "dance895": {
        "name": "Dance 89.5",
        "stream_url": "https://knhc-ice.streamguys1.com/live",
        "logo_url": "https://www.dance895.org/wp-content/uploads/2024/08/logo.svg",
        "fallback_logo": "https://www.dance895.org/wp-content/uploads/2024/08/cropped-DANCE895_SiteIcon-1-270x270.png",
        "mpris_identity": "Dance 89.5 Radio",
        "metadata_mode": "icy",      # read icy-title from mpv metadata
    },
}

DEFAULT_STATION = "rtl2"
SETTINGS_KEY = "rtl2RadioWidget"   # backward-compatible settings key in plugin_settings.json
SOCKET_PATH = "/tmp/rtl2_radio.sock"
LOCK_PATH = "/tmp/rtl2_radio.lock"
MPV_SOCKET_PATH = "/tmp/mpv_rtl2_socket"
SCRAPE_INTERVAL = 30
ICY_POLL_INTERVAL = 10             # icy-title is cheap (local socket), poll faster

# ---- Globals ----------------------------------------------------------------

current_station = DEFAULT_STATION
current_metadata = {"title": "", "artist": "", "thumbnail": ""}
daemon_registered = False
is_playing = False
mpv_process = None
last_scrape_time = 0
last_processed_action = None
_lock_fd = None
current_volume = None
mpris = None
last_mpv_attempt = 0
mpris_name = "org.mpris.MediaPlayer2.rtl2"
mpris_path = "/org/mpris/MediaPlayer2"
mpris_root_iface = "org.mpris.MediaPlayer2"
mpris_player_iface = "org.mpris.MediaPlayer2.Player"
mpris_props_iface = "org.freedesktop.DBus.Properties"
mpris_intro_iface = "org.freedesktop.DBus.Introspectable"

# ---- Helpers ----------------------------------------------------------------

def current_stream_url():
    return STATIONS[current_station]["stream_url"]

def current_station_info():
    return STATIONS[current_station]


def get_today_date(tz_name="Europe/Paris"):
    return datetime.datetime.now(ZoneInfo(tz_name)).strftime("%d-%m-%Y")


def _acquire_lock():
    global _lock_fd
    _lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[STARTUP] Another backend instance is already running, exiting")
        return False
    _lock_fd.write(str(os.getpid()) + "\n")
    _lock_fd.flush()
    return True


def normalize_text(text):
    if not text:
        return ""
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&apos;", "'")
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text.strip())


def _extract_thumbnail_from_markup(markup: str) -> Optional[str]:
    for srcset_match in re.finditer(r'(?:data-srcset|srcset)\s*=\s*"(?P<srcset>[^"]+)"', markup, re.IGNORECASE):
        srcset = srcset_match.group("srcset")
        for entry in srcset.split(","):
            url = entry.strip().split(" ")[0]
            if url.startswith("http"):
                return url
    for img_match in re.finditer(r'(?:data-src|src)\s*=\s*"(?P<src>[^"]+)"', markup, re.IGNORECASE):
        src = img_match.group("src").strip()
        if src and not src.lower().startswith("data:"):
            return src
    return None


def _extract_from_player_markup(html_text: str) -> Optional[Tuple[str, str, Optional[str]]]:
    cover_match = re.search(r'<div[^>]+class="[^"]*container-cover[^"]*"[^>]*>(?P<cover>.*?)</div>', html_text, re.IGNORECASE | re.DOTALL)
    cover_url = None
    if cover_match:
        cover_url = _extract_thumbnail_from_markup(cover_match.group("cover"))
    title_match = re.search(r'<div[^>]+data-player-title[^>]*>(?P<title>.*?)</div>', html_text, re.IGNORECASE | re.DOTALL)
    artist_match = re.search(r'<div[^>]+data-player-hosts[^>]*>(?P<artist>.*?)</div>', html_text, re.IGNORECASE | re.DOTALL)
    if not title_match and not artist_match:
        return None
    def _strip_tags(frag: str) -> str:
        return re.sub(r"<[^>]+>", " ", frag).strip()
    title = normalize_text(_strip_tags(title_match.group("title")) if title_match else "")
    artist = normalize_text(_strip_tags(artist_match.group("artist")) if artist_match else "")
    if not artist and not title:
        return None
    return artist, title, cover_url


# ---- Metadata: scrape (RTL2) -----------------------------------------------

async def scrape_metadata():
    global current_metadata, last_scrape_time
    info = current_station_info()
    if info["metadata_mode"] != "scrape":
        return False
    tz = info.get("scrape_timezone", "Europe/Paris")
    date_str = get_today_date(tz)
    base_url = info["scrape_url"]
    url = base_url.format(date=date_str) + f"?cb={int(time.time() * 1000)}"
    headers = {
        "User-Agent": "rtl2-dms-widget/0.1",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    return False
                content = await resp.text()
    except Exception as e:
        print(f"[SCRAPE] Error fetching {url}: {e}", file=sys.stderr)
        return False

    card_match = re.search(
        r'<div[^>]+class="[^"]*card-qect[^"]*"[^>]*data-qect-info="(.*?)"',
        content, re.IGNORECASE
    )
    if card_match:
        try:
            raw_info = html.unescape(card_match.group(1))
            info_json = json.loads(raw_info)
            artist = normalize_text(info_json.get("singer") or info_json.get("artist", ""))
            title = normalize_text(info_json.get("title", ""))
            thumbnail = info_json.get("thumbnail") or info_json.get("cover", "")
            if artist or title:
                current_metadata = {"artist": artist, "title": title, "thumbnail": thumbnail}
                last_scrape_time = time.time()
                return True
        except Exception as e:
            print(f"[SCRAPE] Failed to parse card-qect JSON: {e}", file=sys.stderr)

    result = _extract_from_player_markup(content)
    if result:
        artist, title, cover_url = result
        current_metadata = {"artist": artist, "title": title, "thumbnail": cover_url or ""}
        last_scrape_time = time.time()
        return True

    print("[SCRAPE] No metadata patterns matched", file=sys.stderr)
    last_scrape_time = time.time()
    return False


# ---- Metadata: icy-title (Dance 89.5, any icecast with icy metadata) --------

def get_icy_metadata():
    """Read icy-title from mpv's metadata property (local socket, cheap)."""
    global current_metadata
    resp = _mpv_command(["get_property", "metadata"])
    if not resp or '"error":"success"' not in resp:
        return False
    try:
        data = json.loads(resp).get("data")
        if not data or not isinstance(data, dict):
            return False
        icy_title = data.get("icy-title") or data.get("icy_title", "")
        if not icy_title:
            return False
        # Format: "Artist - Title"
        parts = str(icy_title).split(" - ", 1)
        artist = parts[0].strip() if len(parts) > 1 else ""
        title = parts[-1].strip() if parts else str(icy_title)
        artist = normalize_text(artist)
        title = normalize_text(title)
        if not title:
            return False
        current_metadata = {"artist": artist, "title": title, "thumbnail": ""}
        return True
    except Exception as e:
        print(f"[ICY] Error parsing metadata: {e}", file=sys.stderr)
        return False


def poll_metadata():
    """Poll metadata based on current station's mode. Returns True on change."""
    info = current_station_info()
    mode = info["metadata_mode"]
    if mode == "icy":
        return get_icy_metadata()
    return False


# ---- mpv lifecycle ---------------------------------------------------------

def start_mpv():
    global mpv_process, is_playing, last_mpv_attempt
    last_mpv_attempt = time.time()
    stream_url = current_stream_url()
    try:
        if os.path.exists(MPV_SOCKET_PATH):
            os.remove(MPV_SOCKET_PATH)
        mpv_process = subprocess.Popen(
            ["mpv", "--no-terminal", "--vo=null", "--ao=pipewire",
             "--cache=no", "--stream-buffer-size=64KiB", "--demuxer-readahead-secs=2",
             "--demuxer-max-bytes=512KiB", f"--input-ipc-server={MPV_SOCKET_PATH}", stream_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(100):
            if os.path.exists(MPV_SOCKET_PATH):
                break
            time.sleep(0.1)
        else:
            print("[MPV] Socket not created after 10 seconds", file=sys.stderr)
            return False
        time.sleep(1)
        test_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            test_socket.connect(MPV_SOCKET_PATH)
            test_socket.send(json.dumps({"command": ["get_property", "idle"]}).encode() + b"\n")
            response = test_socket.recv(4096).decode()
            if '"error":"success"' in response:
                is_playing = True
                print(f"[MPV] Started ({current_station})")
                return True
        except Exception as e:
            print(f"[MPV] Error: {e}", file=sys.stderr)
            return False
        finally:
            test_socket.close()
    except Exception as e:
        print(f"[MPV] Error: {e}", file=sys.stderr)
        return False


def stop_mpv():
    global mpv_process, is_playing
    if mpv_process:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(MPV_SOCKET_PATH)
                cmd = json.dumps({"command": ["quit"]}).encode() + b"\n"
                s.send(cmd)
            except Exception:
                pass
            finally:
                s.close()
            mpv_process.terminate()
            try:
                mpv_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mpv_process.kill()
            finally:
                mpv_process = None
        except Exception as e:
            print(f"[MPV] Stop error: {e}", file=sys.stderr)
        finally:
            is_playing = False
        if os.path.exists(MPV_SOCKET_PATH):
            try:
                os.remove(MPV_SOCKET_PATH)
            except Exception:
                pass
        print("[MPV] Stopped")


def _mpv_command(cmd_parts):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(MPV_SOCKET_PATH)
        s.send(json.dumps({"command": cmd_parts}).encode() + b"\n")
        resp = s.recv(8192).decode()
        s.close()
        return resp
    except OSError as e:
        print(f"[MPV] command {cmd_parts} failed: {e}", file=sys.stderr)
        return None


def get_mpv_volume():
    resp = _mpv_command(["get_property", "volume"])
    if resp and '"error":"success"' in resp:
        try:
            return int(round(float(json.loads(resp).get("data", 0))))
        except (ValueError, TypeError):
            return None
    return None


def set_mpv_volume(vol):
    global current_volume
    vol = max(0, min(100, int(vol)))
    resp = _mpv_command(["set_property", "volume", vol])
    if resp and '"error":"success"' in resp:
        current_volume = vol
        print(f"[VOLUME] Set mpv volume to {vol}", flush=True)
        _mpris_emit({"Volume": ("d", vol / 100.0)})
        return True
    print(f"[VOLUME] Failed to set mpv volume to {vol}", file=sys.stderr)
    return False


def restart_mpv():
    print("[MPV] Restarting", flush=True)
    stop_mpv()
    if not start_mpv():
        print("[MPV] Restart failed", file=sys.stderr)
        return False
    if current_volume is not None:
        set_mpv_volume(current_volume)
    resp = _mpv_command(["set_property", "pause", not is_playing])
    if resp and '"error":"success"' in resp:
        print(f"[MPV] State restored (paused={not is_playing})", flush=True)
    return True


def check_mpv_health():
    global mpv_process, last_mpv_attempt
    if mpv_process is None:
        if time.time() - last_mpv_attempt > 15:
            print("[MPV] Not running, (re)starting", flush=True)
            restart_mpv()
        return
    if mpv_process.poll() is not None:
        print("[MPV] Process died, restarting", flush=True)
        restart_mpv()
        return
    if not is_playing:
        return
    resp = _mpv_command(["get_property", "eof-reached"])
    if not resp or '"error":"success"' not in resp:
        return
    try:
        eof = json.loads(resp).get("data") is True
    except Exception:
        return
    if eof:
        print("[MPV] Stream EOF, reloading", flush=True)
        _mpv_command(["loadfile", current_stream_url()])


def set_play_state(playing):
    global is_playing
    if playing == is_playing:
        return True
    resp = _mpv_command(["set_property", "pause", not playing])
    if resp and '"error":"success"' in resp:
        is_playing = playing
        print(f"[IPC] Set play state to {playing}", flush=True)
        sync_to_dms_settings()
        _mpris_emit({"PlaybackStatus": ("s", _mpris_playback_status())})
        return True
    print(f"[IPC] Failed to set play state to {playing}", file=sys.stderr)
    return False


# ---- Station switching ------------------------------------------------------

def switch_station(station_id):
    """Switch to a different station, restarting mpv and resetting metadata."""
    global current_station, current_metadata
    if station_id not in STATIONS:
        print(f"[STATION] Unknown station: {station_id}")
        return False
    if station_id == current_station:
        return True
    current_station = station_id
    info = current_station_info()
    current_metadata = {"title": f"Loading {info['name']}...", "artist": "", "thumbnail": ""}
    print(f"[STATION] Switched to {info['name']}", flush=True)
    sync_to_dms_settings()
    _mpris_emit({"Metadata": ("a{sv}", _mpris_metadata_plain())})
    restart_mpv()
    # immediate metadata poll for icy stations
    if info["metadata_mode"] == "icy":
        get_icy_metadata()
        sync_to_dms_settings()
        if mpris:
            mpris.publish_metadata_if_changed()
    return True


# ---- MPRIS2 service ---------------------------------------------------------

MPRIS_INTROSPECT_XML = (
    '<node>'
    '<interface name="org.mpris.MediaPlayer2">'
    '<property name="Identity" type="s" access="read"/>'
    '<property name="CanQuit" type="b" access="read"/>'
    '<property name="CanRaise" type="b" access="read"/>'
    '<property name="CanSetFullscreen" type="b" access="read"/>'
    '<property name="Fullscreen" type="b" access="readwrite"/>'
    '<property name="SupportedUriSchemes" type="as" access="read"/>'
    '<property name="SupportedMimeTypes" type="as" access="read"/>'
    '</interface>'
    '<interface name="org.mpris.MediaPlayer2.Player">'
    '<property name="PlaybackStatus" type="s" access="read"/>'
    '<property name="LoopStatus" type="s" access="readwrite"/>'
    '<property name="Rate" type="d" access="readwrite"/>'
    '<property name="Shuffle" type="b" access="readwrite"/>'
    '<property name="Metadata" type="a{sv}" access="read"/>'
    '<property name="Volume" type="d" access="readwrite"/>'
    '<property name="Position" type="x" access="read"/>'
    '<property name="MinimumRate" type="d" access="read"/>'
    '<property name="MaximumRate" type="d" access="read"/>'
    '<property name="CanGoNext" type="b" access="read"/>'
    '<property name="CanGoPrevious" type="b" access="read"/>'
    '<property name="CanPlay" type="b" access="read"/>'
    '<property name="CanPause" type="b" access="read"/>'
    '<property name="CanSeek" type="b" access="read"/>'
    '<property name="CanControl" type="b" access="read"/>'
    '<method name="Next"/><method name="Previous"/><method name="Pause"/>'
    '<method name="PlayPause"/><method name="Stop"/><method name="Play"/>'
    '<method name="Seek"><arg name="Offset" type="x" direction="in"/></method>'
    '<method name="SetPosition"><arg name="TrackId" type="o" direction="in"/>'
    '<arg name="Position" type="x" direction="in"/></method>'
    '<method name="OpenUri"><arg name="Uri" type="s" direction="in"/></method>'
    '</interface>'
    '<interface name="org.freedesktop.DBus.Properties">'
    '<method name="Get"><arg name="interface_name" type="s" direction="in"/>'
    '<arg name="property_name" type="s" direction="in"/>'
    '<arg name="value" type="v" direction="out"/></method>'
    '<method name="GetAll"><arg name="interface_name" type="s" direction="in"/>'
    '<arg name="properties" type="a{sv}" direction="out"/></method>'
    '<method name="Set"><arg name="interface_name" type="s" direction="in"/>'
    '<arg name="property_name" type="s" direction="in"/>'
    '<arg name="value" type="v" direction="in"/></method>'
    '<signal name="PropertiesChanged"><arg name="interface_name" type="s"/>'
    '<arg name="changed_properties" type="a{sv}"/>'
    '<arg name="invalidated_properties" type="as"/></signal>'
    '</interface>'
    '</node>'
)


def _mpris_playback_status():
    if is_playing:
        return "Playing"
    if current_metadata.get("title"):
        return "Paused"
    return "Stopped"


def _mpris_metadata_plain():
    m = {}
    if current_metadata.get("title"):
        m["mpris:trackid"] = ("o", mpris_path + "/track")
        m["xesam:title"] = ("s", current_metadata["title"])
        artist = current_metadata.get("artist") or ""
        if artist:
            m["xesam:artist"] = ("as", [artist])
        art = current_metadata.get("thumbnail") or current_station_info().get("logo_url", "")
        if art:
            m["mpris:artUrl"] = ("s", art)
    return m


def _mpris_root_props():
    return {
        "Identity": ("s", current_station_info()["mpris_identity"]),
        "CanQuit": ("b", False),
        "CanRaise": ("b", False),
        "CanSetFullscreen": ("b", False),
        "Fullscreen": ("b", False),
        "SupportedUriSchemes": ("as", []),
        "SupportedMimeTypes": ("as", []),
    }


def _mpris_player_props():
    return {
        "PlaybackStatus": ("s", _mpris_playback_status()),
        "LoopStatus": ("s", "None"),
        "Rate": ("d", 1.0),
        "Shuffle": ("b", False),
        "Metadata": ("a{sv}", _mpris_metadata_plain()),
        "Volume": ("d", (current_volume if current_volume is not None else 100) / 100.0),
        "Position": ("x", 0),
        "MinimumRate": ("d", 1.0),
        "MaximumRate": ("d", 1.0),
        "CanGoNext": ("b", True),
        "CanGoPrevious": ("b", True),
        "CanPlay": ("b", True),
        "CanPause": ("b", True),
        "CanSeek": ("b", False),
        "CanControl": ("b", True),
    }


def _mpris_emit(changed_plain):
    if mpris is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(mpris.emit_properties_changed(changed_plain))


class MprisService:
    def __init__(self, conn):
        self.conn = conn
        self._last_metadata = None

    def publish_metadata_if_changed(self):
        meta = _mpris_metadata_plain()
        if meta == self._last_metadata:
            return
        self._last_metadata = meta
        _mpris_emit({"Metadata": ("a{sv}", meta)})

    def publish_playback_if_changed(self):
        _mpris_emit({"PlaybackStatus": ("s", _mpris_playback_status())})

    async def emit_properties_changed(self, changed_plain):
        if not changed_plain:
            return
        body = (
            mpris_player_iface,
            dict(changed_plain),
            [],
        )
        header = new_header(MessageType.signal)
        header.fields[HeaderFields.path] = mpris_path
        header.fields[HeaderFields.interface] = mpris_props_iface
        header.fields[HeaderFields.member] = "PropertiesChanged"
        header.fields[HeaderFields.signature] = "sa{sv}as"
        try:
            await self.conn.send(Message(header, body))
        except Exception as e:
            print(f"[MPRIS] signal error: {e}", file=sys.stderr)

    async def run(self):
        print(f"[MPRIS] Listening on {mpris_name}", flush=True)
        while True:
            try:
                msg = await self.conn.receive()
            except EOFError:
                print("[MPRIS] D-Bus connection closed", file=sys.stderr)
                return
            if msg.header.message_type != MessageType.method_call:
                continue
            if msg.header.fields.get(HeaderFields.path) != mpris_path:
                continue
            try:
                await self._dispatch(msg)
            except Exception as e:
                print(f"[MPRIS] handler error: {e}", file=sys.stderr)

    async def _dispatch(self, msg):
        iface = msg.header.fields.get(HeaderFields.interface)
        member = msg.header.fields.get(HeaderFields.member)

        if iface == mpris_props_iface:
            if member == "Get":
                iface_name, prop = msg.body
                for props in (_mpris_root_props(), _mpris_player_props()):
                    if prop in props:
                        sig, val = props[prop]
                        await self.conn.send(new_method_return(msg, "v", ((sig, val),)))
                        return
                await self.conn.send(new_error(msg, "org.freedesktop.DBus.Error.UnknownProperty",
                                               "s", (f"Unknown property {prop}",)))
                return
            if member == "GetAll":
                iface_name = msg.body[0]
                props = {}
                if iface_name in (mpris_root_iface, ""):
                    props.update(_mpris_root_props())
                if iface_name in (mpris_player_iface, ""):
                    props.update(_mpris_player_props())
                variants = dict(props)
                await self.conn.send(new_method_return(msg, "a{sv}", (variants,)))
                return
            if member == "Set":
                iface_name, prop, value = msg.body
                if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
                    value = value[1]
                if prop == "Volume":
                    try:
                        set_mpv_volume(int(round(float(value) * 100)))
                        await self.conn.send(new_method_return(msg))
                    except (ValueError, TypeError):
                        await self.conn.send(new_error(msg, "org.freedesktop.DBus.Error.InvalidArgs",
                                                       "s", ("Invalid volume",)))
                else:
                    await self.conn.send(new_error(msg, "org.freedesktop.DBus.Error.PropertyReadOnly",
                                                   "s", (f"{prop} is read-only",)))
                return
            if member == "Introspect":
                await self.conn.send(new_method_return(msg, "s", (MPRIS_INTROSPECT_XML,)))
                return
            await self.conn.send(new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod",
                                           "s", (f"Unknown method {member}",)))
            return

        if iface in (mpris_root_iface, mpris_player_iface):
            if member in ("Play", "Pause", "PlayPause", "Stop", "Next", "Previous",
                          "Seek", "SetPosition", "OpenUri"):
                sender = msg.header.fields.get(HeaderFields.sender, "?")
                print(f"[MPRIS] {member} requested by {sender}", flush=True)
                if member == "Play":
                    set_play_state(True)
                elif member == "Pause":
                    set_play_state(False)
                elif member == "PlayPause":
                    toggle_play()
                elif member == "Stop":
                    set_play_state(False)
                elif member == "Next":
                    station_ids = list(STATIONS.keys())
                    idx = station_ids.index(current_station) if current_station in station_ids else -1
                    next_id = station_ids[(idx + 1) % len(station_ids)]
                    if next_id != current_station:
                        switch_station(next_id)
                elif member == "Previous":
                    station_ids = list(STATIONS.keys())
                    idx = station_ids.index(current_station) if current_station in station_ids else -1
                    prev_id = station_ids[(idx - 1) % len(station_ids)]
                    if prev_id != current_station:
                        switch_station(prev_id)
                await self.conn.send(new_method_return(msg))
                return
            await self.conn.send(new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod",
                                           "s", (f"Unknown method {member}",)))
            return

        if iface == mpris_intro_iface and member == "Introspect":
            await self.conn.send(new_method_return(msg, "s", (MPRIS_INTROSPECT_XML,)))
            return

        await self.conn.send(new_error(msg, "org.freedesktop.DBus.Error.UnknownInterface",
                                       "s", (f"Unknown interface {iface}",)))


async def start_mpris():
    global mpris
    if not HAVE_JEEPNEY:
        print("[MPRIS] jeepney not installed, media integration disabled", file=sys.stderr)
        return None
    try:
        conn = await open_dbus_connection(bus="SESSION")
    except Exception as e:
        print(f"[MPRIS] Cannot connect to session bus: {e}", file=sys.stderr)
        return None
    svc = MprisService(conn)
    try:
        reply = await conn.send(new_method_call(
            DBusAddress(bus_name="org.freedesktop.DBus",
                        object_path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus"),
            "RequestName", "su", (mpris_name, 0x4)))
        while True:
            reply = await conn.receive()
            if reply.header.message_type in (MessageType.method_return, MessageType.error):
                break
        if reply.header.message_type == MessageType.error:
            print(f"[MPRIS] Name {mpris_name} already owned: {reply.body}", file=sys.stderr)
            return None
        primary = reply.body[0]
        if primary != 1:
            print(f"[MPRIS] RequestName returned {primary}, continuing anyway", file=sys.stderr)
    except Exception as e:
        print(f"[MPRIS] Name request failed: {e}", file=sys.stderr)
        return None
    mpris = svc
    return svc


def toggle_play():
    global is_playing
    try:
        for attempt in range(3):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(MPV_SOCKET_PATH)
                new_pause_state = is_playing
                cmd = json.dumps({"command": ["set_property", "pause", new_pause_state]}).encode() + b"\n"
                s.send(cmd)
                try:
                    resp = s.recv(4096).decode()
                except socket.timeout:
                    pass
                s.close()
                is_playing = not is_playing
                print(f"[IPC] Toggled play state to {is_playing}")
                _mpris_emit({"PlaybackStatus": ("s", _mpris_playback_status())})
                return True
            except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
                if attempt < 2:
                    time.sleep(0.5)
                else:
                    print(f"[IPC] All toggle attempts failed: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[IPC] Toggle error: {e}", file=sys.stderr)
        return False


def get_play_state():
    return is_playing


def get_metadata():
    return json.dumps(current_metadata)


# ---- Settings I/O -----------------------------------------------------------

def write_settings(plugin_settings):
    settings_path = Path.home() / ".config" / "DankMaterialShell" / "plugin_settings.json"
    tmp_path = settings_path.with_name(settings_path.name + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(plugin_settings, f, indent=2)
    os.replace(tmp_path, settings_path)


def sync_to_dms_settings():
    """Sync current metadata, play state, and station to DMS plugin settings."""
    try:
        settings = {
            "title": current_metadata.get("title", ""),
            "artist": current_metadata.get("artist", ""),
            "artUrl": current_metadata.get("thumbnail", "") or current_station_info().get("logo_url", ""),
            "isPlaying": is_playing,
            "station": current_station,
            "connectionStatus": "connected",
        }

        settings_path = Path.home() / ".config" / "DankMaterialShell" / "plugin_settings.json"
        plugin_settings = {}
        if settings_path.exists():
            try:
                with open(settings_path, "r") as f:
                    plugin_settings = json.load(f)
            except Exception as e:
                print(f"[SETTINGS] Error reading settings: {e}", file=sys.stderr)

        if SETTINGS_KEY in plugin_settings:
            existing = plugin_settings[SETTINGS_KEY]
            for key in existing:
                if key not in ("title", "artist", "artUrl", "isPlaying", "station", "connectionStatus"):
                    settings[key] = existing[key]
        plugin_settings[SETTINGS_KEY] = settings

        write_settings(plugin_settings)
    except Exception as e:
        print(f"[SETTINGS] Error syncing to DMS: {e}", file=sys.stderr)


def process_widget_action():
    """Read plugin_settings.json and process pending widget actions.

    Handles: toggle action, station switch.
    """
    global last_processed_action, current_station
    settings_path = Path.home() / ".config" / "DankMaterialShell" / "plugin_settings.json"
    try:
        if not settings_path.exists():
            return False
        with open(settings_path, "r") as f:
            plugin_settings = json.load(f)
        widget_settings = plugin_settings.get(SETTINGS_KEY, {})
        action = widget_settings.get("action")
        if not action or not str(action).startswith("toggle"):
            pass  # check station below
        else:
            if action == last_processed_action:
                print(f"[SETTINGS] Duplicate action {action!r} skipped (already processed)")
            else:
                last_processed_action = action
                print(f"[SETTINGS] Toggle command received: {action!r}")
                if not is_playing and mpv_process is None:
                    restart_mpv()
                else:
                    toggle_play()
                sync_to_dms_settings()

            # Clear processed action
            with open(settings_path, "r") as f:
                plugin_settings = json.load(f)
            widget_settings = plugin_settings.get(SETTINGS_KEY, {})
            if widget_settings.get("action") == action:
                widget_settings["action"] = None
                plugin_settings[SETTINGS_KEY] = widget_settings
                write_settings(plugin_settings)
            return True

        # Check for station switch
        widget_station = widget_settings.get("station")
        if widget_station and widget_station in STATIONS and widget_station != current_station:
            print(f"[SETTINGS] Station switch to {widget_station}")
            switch_station(widget_station)
            sync_to_dms_settings()
            return True

        return False
    except Exception as e:
        print(f"[SETTINGS] Error checking commands: {e}", file=sys.stderr)
        return False


def process_widget_volume():
    global current_volume
    settings_path = Path.home() / ".config" / "DankMaterialShell" / "plugin_settings.json"
    try:
        if not settings_path.exists():
            return
        with open(settings_path, "r") as f:
            plugin_settings = json.load(f)
        widget_settings = plugin_settings.get(SETTINGS_KEY) or {}
        vol = widget_settings.get("volume")
        if vol is None:
            return
        vol = max(0, min(100, int(vol)))
        if current_volume is not None and vol == current_volume:
            return
        set_mpv_volume(vol)
    except Exception as e:
        print(f"[VOLUME] Error processing: {e}", file=sys.stderr)


# ---- Socket server ----------------------------------------------------------

async def handle_socket(reader, writer):
    global is_playing, daemon_registered
    try:
        data = await reader.read(1024)
        if not data:
            return
        message = data.decode().strip()
        if message == "HELLO":
            daemon_registered = True
            print("[DAEMON] Registered", flush=True)
            while True:
                more = await reader.read(1024)
                if not more:
                    print("[DAEMON] Connection lost, shutting down", flush=True)
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                cmd = more.decode().strip()
                if cmd == "QUIT":
                    print("[DAEMON] QUIT received, shutting down", flush=True)
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
        elif message == "PLAY":
            if not is_playing:
                toggle_play()
                sync_to_dms_settings()
                writer.write(b"OK\n")
                writer.write(json.dumps({"playing": True, "metadata": current_metadata}).encode() + b"\n")
            else:
                writer.write(b"ALREADY_PLAYING\n")
        elif message == "PAUSE":
            if is_playing:
                toggle_play()
                sync_to_dms_settings()
                writer.write(b"OK\n")
                writer.write(json.dumps({"playing": False, "metadata": current_metadata}).encode() + b"\n")
            else:
                writer.write(b"ALREADY_PAUSED\n")
        elif message == "TOGGLE":
            toggle_play()
            sync_to_dms_settings()
            writer.write(json.dumps({"playing": is_playing, "metadata": current_metadata}).encode() + b"\n")
        elif message == "STATUS":
            state = "playing" if is_playing else "paused"
            writer.write(f"{state}\n".encode())
        elif message == "VOLUME" or message.startswith("VOLUME:"):
            if ":" in message:
                try:
                    set_mpv_volume(int(message.split(":", 1)[1]))
                except ValueError:
                    writer.write(b"INVALID_VOLUME\n")
                    await writer.drain()
                    return
            vol = get_mpv_volume()
            if vol is None:
                vol = current_volume
            writer.write(json.dumps({"volume": vol}).encode() + b"\n")
        elif message == "METADATA" or message == "GET_METADATA":
            writer.write(get_metadata().encode() + b"\n")
        elif message == "STATION" or message.startswith("STATION:"):
            if ":" in message:
                sid = message.split(":", 1)[1]
                if sid in STATIONS:
                    switch_station(sid)
                    writer.write(json.dumps({"station": sid, "name": STATIONS[sid]["name"]}).encode() + b"\n")
                else:
                    writer.write(b"UNKNOWN_STATION\n")
            else:
                writer.write(json.dumps({"station": current_station, "name": current_station_info()["name"]}).encode() + b"\n")
        elif message == "QUIT":
            writer.write(b"OK\n")
            await writer.drain()
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            writer.write(b"UNKNOWN_COMMAND\n")
        await writer.drain()
    except Exception as e:
        print(f"[SOCKET] Error: {e}", file=sys.stderr)
    finally:
        writer.close()
        await writer.wait_closed()


async def _await_daemon_registration():
    await asyncio.sleep(15)
    if not daemon_registered:
        print("[DAEMON] No daemon registration within 15s, shutting down", flush=True)
        os.kill(os.getpid(), signal.SIGTERM)


# ---- Poll loops -------------------------------------------------------------

async def toggle_poll_loop():
    """Poll for widget actions every 1s."""
    while True:
        process_widget_action()
        process_widget_volume()
        check_mpv_health()
        await asyncio.sleep(1)


async def metadata_loop():
    """Poll metadata based on current station's metadata_mode."""
    while True:
        info = current_station_info()
        mode = info["metadata_mode"]
        if mode == "scrape":
            success = await scrape_metadata()
            if success:
                print(f"[METADATA] Scraped: {current_metadata['artist']} - {current_metadata['title']}")
                if mpris:
                    mpris.publish_metadata_if_changed()
                await asyncio.sleep(SCRAPE_INTERVAL - 10)
                sync_to_dms_settings()
            else:
                print("[METADATA] Scrape failed", file=sys.stderr)
                await asyncio.sleep(SCRAPE_INTERVAL)
        elif mode == "icy":
            changed = get_icy_metadata()
            if changed and current_metadata.get("title"):
                print(f"[METADATA] ICY: {current_metadata['artist']} - {current_metadata['title']}")
                if mpris:
                    mpris.publish_metadata_if_changed()
                sync_to_dms_settings()
            await asyncio.sleep(ICY_POLL_INTERVAL)
        else:
            await asyncio.sleep(30)


async def socket_server():
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)
    server = await asyncio.start_unix_server(handle_socket, SOCKET_PATH)
    async with server:
        print(f"[SOCKET] Listening on {SOCKET_PATH}")
        await server.serve_forever()


# ---- Main -------------------------------------------------------------------

async def main():
    global current_metadata, is_playing, current_station

    if not _acquire_lock():
        return

    # Adopt-and-exit if another backend is already serving
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(SOCKET_PATH)
        s.send(b"STATUS\n")
        resp = s.recv(64).decode().strip()
        s.close()
        if resp in ("playing", "paused"):
            print("[STARTUP] Backend already running, exiting")
            return
    except OSError:
        pass

    _start_parent_watchdog()

    if "--daemon-spawned" in sys.argv:
        asyncio.create_task(_await_daemon_registration())

    # Kill old mpv processes
    kill_procs = ["pkill", "-f", "mpv.*rtl2"]
    proc = await asyncio.create_subprocess_exec(*kill_procs, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    await proc.wait()
    await asyncio.sleep(0.5)

    if os.path.exists(SOCKET_PATH):
        try:
            os.remove(SOCKET_PATH)
        except Exception:
            pass

    # Read persisted station from settings
    settings_path = Path.home() / ".config" / "DankMaterialShell" / "plugin_settings.json"
    try:
        if settings_path.exists():
            with open(settings_path, "r") as f:
                ps = json.load(f)
            ws = ps.get(SETTINGS_KEY) or {}
            saved_station = ws.get("station", DEFAULT_STATION)
            if saved_station in STATIONS:
                current_station = saved_station
    except Exception:
        pass

    info = current_station_info()
    current_metadata = {"title": f"Loading {info['name']}...", "artist": "", "thumbnail": ""}

    if not start_mpv():
        print(f"[{current_station}] Failed to start mpv", file=sys.stderr)
        return

    # Immediately pause so first click starts playback
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(MPV_SOCKET_PATH)
        s.send(json.dumps({"command": ["set_property", "pause", True]}).encode() + b"\n")
        s.recv(4096)
        s.close()
        is_playing = False
        print(f"[MPV] Started in paused state ({current_station})")
    except Exception as e:
        print(f"[MPV] Failed to pause at startup: {e}", file=sys.stderr)

    # Clear stale toggle, set initial metadata
    try:
        if settings_path.exists():
            with open(settings_path, "r") as f:
                ps = json.load(f)
            ws = ps.get(SETTINGS_KEY) or {}
            ws["action"] = None
            ws["isPlaying"] = False
            ws.setdefault("volume", 100)
            ws["station"] = current_station
            ps[SETTINGS_KEY] = ws
            write_settings(ps)
            print("[STARTUP] Cleared stale action, set isPlaying=False")
    except Exception as e:
        print(f"[STARTUP] Error clearing settings: {e}", file=sys.stderr)

    # Restore volume
    try:
        with open(settings_path, "r") as f:
            ps = json.load(f)
        vol = (ps.get(SETTINGS_KEY) or {}).get("volume", 100)
        set_mpv_volume(vol)
    except Exception as e:
        print(f"[STARTUP] Error restoring volume: {e}", file=sys.stderr)

    # Sync initial metadata to settings
    sync_to_dms_settings()

    # Advertise MPRIS
    mpris_svc = await start_mpris()
    if mpris_svc:
        mpris_svc.publish_metadata_if_changed()

    # Start loops
    meta_task = asyncio.create_task(metadata_loop())
    socket_task = asyncio.create_task(socket_server())
    toggle_task = asyncio.create_task(toggle_poll_loop())
    tasks = [meta_task, socket_task, toggle_task]
    if mpris_svc:
        tasks.append(asyncio.create_task(mpris_svc.run()))

    print(f"[{current_station}] Backend Manager Started")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        stop_mpv()
        if os.path.exists(SOCKET_PATH):
            try:
                os.remove(SOCKET_PATH)
            except Exception:
                pass


def _sigterm_handler(signum, frame):
    print(f"[{current_station}] SIGTERM received, shutting down", flush=True)
    stop_mpv()
    if os.path.exists(SOCKET_PATH):
        try:
            os.remove(SOCKET_PATH)
        except Exception:
            pass
    os._exit(0)


def _start_parent_watchdog():
    ppid = os.getppid()
    if ppid <= 1:
        return
    try:
        with open(f"/proc/{ppid}/comm") as f:
            parent_comm = f.read().strip()
    except OSError:
        return
    if parent_comm != "qs":
        print(f"[WATCHDOG] Parent is {parent_comm}, not watching")
        return
    print(f"[WATCHDOG] Watching parent {ppid}")

    def _watch():
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        while True:
            time.sleep(2)
            if os.getppid() != ppid:
                print("[WATCHDOG] Parent died, shutting down", flush=True)
                stop_mpv()
                if os.path.exists(SOCKET_PATH):
                    try:
                        os.remove(SOCKET_PATH)
                    except Exception:
                        pass
                os._exit(0)

    threading.Thread(target=_watch, daemon=True).start()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm_handler)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print(f"\n[{current_station}] Stopped by user")
