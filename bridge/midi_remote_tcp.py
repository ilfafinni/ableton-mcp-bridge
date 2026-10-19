"""
TCP client for the VocalAssistant MIDI Remote Script.

Communicates with VocalAssistant/__init__.py running inside Ableton Live
via TCP socket on 127.0.0.1:9878 (JSON messages, newline-delimited).

This replaces AbletonOSC + device_helper.amxd for most operations:
  - Device insertion via browser navigation (no M4L needed)
  - Rack/chain/macro via browser + Live API
  - Undo/redo, arrangement control, audio clip access
  - All track/clip/mixer/device parameter operations
  - Browser scanning and item loading

Fallback: For operations not yet in the MIDI Remote Script, fall back
to AbletonOSC (UDP 11000/11001) and device_helper.amxd (UDP 9001/9002).
"""
from __future__ import absolute_import, print_function, unicode_literals

import json
import os
import socket
import threading
import time
import sys
import traceback

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MIDI_SCRIPT_HOST = "127.0.0.1"
MIDI_SCRIPT_PORT = 9878
DEFAULT_PORT = MIDI_SCRIPT_PORT
ACTIVE_PORT = MIDI_SCRIPT_PORT
MAX_PORT_SCAN = 16
CONNECT_TIMEOUT = 3.0
DEFAULT_TIMEOUT = 10.0
HEAVY_TIMEOUT = 45.0
RETRY_COUNT = 2
RETRY_DELAY = 0.5

# Load bridge token (same as MIDI Remote Script looks for)
BRIDGE_TOKEN = ""
_BRIDGE_TOKEN_PATHS = [
    os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "ableton-mcp", "bridge_token"),
    os.path.join(os.path.expanduser("~"), ".config", "ableton-mcp", "bridge_token"),
    os.path.join(os.path.expanduser("~"), ".local", "share", "ableton-mcp", "bridge_token"),
]


def _load_token():
    global BRIDGE_TOKEN
    for path in _BRIDGE_TOKEN_PATHS:
        try:
            with open(path, "r") as f:
                token = f.read().strip()
                if len(token) >= 16:
                    BRIDGE_TOKEN = token
                    return
        except Exception:
            continue


_load_token()

# Commands that take longer (browser scanning, audio clip loading)
HEAVY_COMMANDS = frozenset([
    "load_browser_item", "load_instrument_or_effect", "load_audio_clip",
    "scan_browser_full",
])


# ---------------------------------------------------------------------------
# Connection manager (thread-safe, reuses socket)
# ---------------------------------------------------------------------------

class MidiRemoteClient:
    """Thread-safe TCP client for VocalAssistant MIDI Remote Script."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sock = None
        self._connected = False
        self._last_error = ""

    def _connect(self):
        """Establish a new TCP connection to the MIDI Remote Script."""
        try:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(CONNECT_TIMEOUT)
            self._sock.connect((MIDI_SCRIPT_HOST, ACTIVE_PORT))
            self._sock.settimeout(None)
            self._connected = True
            self._last_error = ""
            return True
        except (ConnectionRefusedError, OSError, socket.timeout) as e:
            self._connected = False
            self._last_error = str(e)
            return False

    def _ensure_connected(self):
        """Make sure we have a live connection, reconnecting if needed."""
        if self._connected and self._sock:
            try:
                self._sock.sendall(b"")
                return True
            except (OSError, BrokenPipeError):
                self._connected = False
        return self._connect()

    def send_command(self, command_type, params=None, timeout=None):
        """Send a command to the MIDI Remote Script and return the response.

        Args:
            command_type: The command name (e.g. "get_session_info", "set_tempo")
            params: Optional dict of parameters for the command
            timeout: Optional timeout in seconds (default: 10s, heavy: 45s)

        Returns:
            dict with "status" ("success"|"error") and "result" or "message"
        """
        if timeout is None:
            timeout = HEAVY_TIMEOUT if command_type in HEAVY_COMMANDS else DEFAULT_TIMEOUT

        cmd = {"type": command_type, "params": params or {}}
        if BRIDGE_TOKEN:
            cmd["token"] = BRIDGE_TOKEN

        for attempt in range(RETRY_COUNT):
            with self._lock:
                try:
                    if not self._ensure_connected():
                        if attempt < RETRY_COUNT - 1:
                            time.sleep(RETRY_DELAY)
                            continue
                        return {
                            "status": "error",
                            "message": "Cannot connect to VocalAssistant MIDI Remote Script on port {}. "
                                       "Is Ableton Live open with VocalAssistant loaded as a control surface? "
                                       "Error: {}".format(ACTIVE_PORT, self._last_error),
                        }

                    payload = json.dumps(cmd).encode("utf-8")
                    self._sock.sendall(payload)

                    # Read response (may be chunked)
                    self._sock.settimeout(timeout)
                    chunks = []
                    total = 0
                    while True:
                        try:
                            chunk = self._sock.recv(65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                            total += len(chunk)
                            # Try to parse what we have
                            raw = b"".join(chunks)
                            try:
                                response = json.loads(raw.decode("utf-8"))
                                self._sock.settimeout(None)
                                return response
                            except (json.JSONDecodeError, ValueError):
                                # Might need more data
                                if total > 4 * 1024 * 1024:  # 4 MB limit
                                    break
                                continue
                        except socket.timeout:
                            self._sock.settimeout(None)
                            if attempt < RETRY_COUNT - 1:
                                break
                            return {
                                "status": "error",
                                "message": "Timeout waiting for response from MIDI Remote Script "
                                           "(command: {}, timeout: {}s)".format(command_type, timeout),
                            }

                    # If we got here without a valid response, retry
                    self._connected = False
                    if attempt < RETRY_COUNT - 1:
                        time.sleep(RETRY_DELAY)
                        continue
                    return {
                        "status": "error",
                        "message": "No valid response from MIDI Remote Script for command: {}".format(command_type),
                    }

                except (BrokenPipeError, ConnectionResetError, OSError) as e:
                    self._connected = False
                    self._last_error = str(e)
                    if attempt < RETRY_COUNT - 1:
                        time.sleep(RETRY_DELAY)
                        continue
                    return {
                        "status": "error",
                        "message": "Connection lost to MIDI Remote Script: {}".format(str(e)),
                    }
                except Exception as e:
                    return {
                        "status": "error",
                        "message": "Unexpected error communicating with MIDI Remote Script: {}".format(str(e)),
                    }

        return {"status": "error", "message": "Failed after {} retries".format(RETRY_COUNT)}

    def ping(self):
        """Quick check if MIDI Remote Script is reachable."""
        try:
            result = self.send_command("ping", timeout=3.0)
            return result.get("status") == "success"
        except Exception:
            return False

    def close(self):
        """Close the connection."""
        with self._lock:
            self._connected = False
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None


# ---------------------------------------------------------------------------
# Singleton client
# ---------------------------------------------------------------------------

_client = None
_client_lock = threading.Lock()


def get_client():
    """Get or create the singleton MidiRemoteClient."""
    global _client
    with _client_lock:
        if _client is None:
            _client = MidiRemoteClient()
    return _client


def send(command_type, params=None, timeout=None):
    """Convenience function to send a command via the singleton client."""
    return get_client().send_command(command_type, params, timeout)


def is_available():
    """Check if the MIDI Remote Script is running and reachable."""
    return get_client().ping()


# ---------------------------------------------------------------------------
# Multi-instance discovery / selection
# ---------------------------------------------------------------------------


def set_active_port(port):
    """Switch the active Ableton instance by its TCP port."""
    global ACTIVE_PORT
    port = int(port)
    if not (DEFAULT_PORT <= port < DEFAULT_PORT + MAX_PORT_SCAN):
        return {"status": "error", "message": "Puerto fuera de rango: {}".format(port)}
    with _client_lock:
        global _client
        if _client is not None:
            _client.close()
            _client = None
    ACTIVE_PORT = port
    ok = is_available()
    return {
        "status": "success" if ok else "error",
        "ok": ok,
        "port": ACTIVE_PORT,
        "message": "Instancia en puerto {} activa".format(ACTIVE_PORT) if ok
                   else "No hay respuesta en el puerto {}".format(ACTIVE_PORT),
    }


def get_active_port():
    """Return the port of the currently active instance."""
    return ACTIVE_PORT


def _ping_port(port, timeout=1.0):
    """Quick TCP ping to a candidate port without touching the singleton."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((MIDI_SCRIPT_HOST, port))
        cmd = {"type": "ping", "params": {}}
        if BRIDGE_TOKEN:
            cmd["token"] = BRIDGE_TOKEN
        s.sendall(json.dumps(cmd).encode("utf-8"))
        s.settimeout(timeout)
        data = s.recv(65536)
        s.close()
        resp = json.loads(data.decode("utf-8"))
        if resp.get("status") == "success":
            result = resp.get("result", {})
            return {
                "port": port,
                "ok": True,
                "version": result.get("version", ""),
                "live_version": result.get("live_version", ""),
                "script_port": result.get("port", port),
            }
    except Exception:
        pass
    return {"port": port, "ok": False}


# Sets cuyo nombre contiene esta etiqueta quedan PROTEGIDOS: la
# auto-deteccion nunca los seleccionara (p.ej. el set "definitivo"
# que el usuario reserva para cantar). select_instance manual si
# permite apuntar a ellos a conciencia.
PROTECTED_SONG_KEYWORDS = ("definitivo",)


def _instance_song_name(port, timeout=4.0):
    """Lee el nombre del set de una instancia sin tocar el singleton."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((MIDI_SCRIPT_HOST, port))
        cmd = {"type": "get_session_info", "params": {}}
        if BRIDGE_TOKEN:
            cmd["token"] = BRIDGE_TOKEN
        s.sendall(json.dumps(cmd).encode("utf-8"))
        data = s.recv(262144)
        s.close()
        resp = json.loads(data.decode("utf-8"))
        if resp.get("status") == "success":
            return str((resp.get("result") or {}).get("song_name", ""))
    except Exception:
        pass
    return ""


def _is_protected_song(name):
    low = str(name or "").lower()
    return any(k in low for k in PROTECTED_SONG_KEYWORDS)


def list_instances():
    """Scan candidate ports and return every reachable Ableton instance.
    Cada instancia incluye song_name y la marca protected si es un set reservado."""
    instances = []
    for offset in range(MAX_PORT_SCAN):
        port = DEFAULT_PORT + offset
        info = _ping_port(port)
        if info["ok"]:
            song = _instance_song_name(port)
            info["song_name"] = song
            info["protected"] = _is_protected_song(song)
            instances.append(info)
    # Ensure the active port is listed first even if slightly stale
    instances.sort(key=lambda i: (0 if i["port"] == ACTIVE_PORT else 1,
                                  not i.get("protected", False)))
    return instances


def select_instance(port):
    """Alias of set_active_port returning a dict suitable for MCP tools."""
    return set_active_port(port)


# ---------------------------------------------------------------------------
# Higher-level command wrappers (match tool signatures in mcp_server.py)
# ---------------------------------------------------------------------------


def midi_get_session_info():
    """Get full session info from the MIDI Remote Script."""
    return send("get_session_info")


def midi_get_track_info(track_index):
    """Get detailed info for a specific track."""
    return send("get_track_info", {"track_index": track_index})


def midi_get_arrangement_map():
    """Get the arrangement timeline map."""
    return send("get_arrangement_map")


def midi_get_clip_audio_info(track_index, clip_index=0, view="session"):
    """Get audio clip info (file_path, sample_rate, etc.)."""
    return send("get_clip_audio_info", {
        "track_index": track_index,
        "clip_index": clip_index,
        "view": view,
    })


def midi_get_device_parameters(track_index, device_index):
    """Get parameters for a specific device."""
    return send("get_device_parameters", {
        "track_index": track_index,
        "device_index": device_index,
    })


def midi_get_transport_state():
    """Get current transport state (playing, tempo, position)."""
    return send("get_transport_state")


def midi_get_scenes():
    """Get all scenes."""
    return send("get_scenes")


def midi_get_return_tracks():
    """Get return tracks info."""
    return send("get_return_tracks")


def midi_get_clip_notes(track_index, clip_index, view="session", summary_only=False):
    """Get MIDI notes from a clip."""
    return send("get_clip_notes", {
        "track_index": track_index,
        "clip_index": clip_index,
        "view": view,
        "summary_only": summary_only,
    })


def midi_get_clip_automation(track_index, clip_index, view="session",
                              parameter="volume", device_index=None):
    """Get clip automation envelope info."""
    params = {
        "track_index": track_index,
        "clip_index": clip_index,
        "view": view,
        "parameter": parameter,
    }
    if device_index is not None:
        params["device_index"] = device_index
    return send("get_clip_automation", params)


# --- Browser commands ---


def midi_scan_browser(categories=None, max_depth=6):
    """Scan the Ableton browser tree."""
    params = {"max_depth": max_depth}
    if categories:
        params["categories"] = categories
    return send("scan_browser_full", params)


def midi_get_browser_tree(category_type="all"):
    """Get top-level browser categories."""
    return send("get_browser_tree", {"category_type": category_type})


def midi_get_browser_items_at_path(path=""):
    """Get browser items at a specific path."""
    return send("get_browser_items_at_path", {"path": path})


def midi_get_browser_item(uri=None, path=None):
    """Get a cached browser item by URI or path."""
    params = {}
    if uri:
        params["uri"] = uri
    if path:
        params["path"] = path
    return send("get_browser_item", params)


# --- Mutating commands (run on main thread via schedule_message) ---


def midi_set_tempo(tempo):
    """Set session tempo."""
    return send("set_tempo", {"tempo": tempo})


def midi_set_metronome(enabled):
    """Enable/disable metronome."""
    return send("set_metronome", {"enabled": enabled})


def midi_start_playback(start_time=None):
    """Start playback."""
    params = {}
    if start_time is not None:
        params["start_time"] = start_time
    return send("start_playback", params)


def midi_stop_playback():
    """Stop playback."""
    return send("stop_playback")


def midi_fire_clip(track_index, clip_index):
    """Fire a clip."""
    return send("fire_clip", {"track_index": track_index, "clip_index": clip_index})


def midi_stop_clip(track_index, clip_index):
    """Stop a clip."""
    return send("stop_clip", {"track_index": track_index, "clip_index": clip_index})


def midi_fire_scene(scene_index):
    """Fire a scene."""
    return send("fire_scene", {"scene_index": scene_index})


def midi_create_midi_track(index=-1):
    """Create a new MIDI track."""
    return send("create_midi_track", {"index": index})


def midi_create_audio_track(index=-1):
    """Create a new audio track."""
    return send("create_audio_track", {"index": index})


def midi_set_track_name(track_index, name):
    """Rename a track."""
    return send("set_track_name", {"track_index": track_index, "name": name})


def midi_create_clip(track_index, clip_index, length=4.0):
    """Create a MIDI clip in a session slot."""
    return send("create_clip", {
        "track_index": track_index,
        "clip_index": clip_index,
        "length": length,
    })


def midi_create_arrangement_midi_clip(track_index, start_time=0.0, length=4.0):
    """Create a MIDI clip in the arrangement."""
    return send("create_arrangement_midi_clip", {
        "track_index": track_index,
        "start_time": start_time,
        "length": length,
    })


def midi_add_notes_to_clip(track_index, clip_index, notes, clip_length=None, view="session"):
    """Add MIDI notes to a clip."""
    params = {
        "track_index": track_index,
        "clip_index": clip_index,
        "notes": notes,
        "view": view,
    }
    if clip_length is not None:
        params["clip_length"] = clip_length
    return send("add_notes_to_clip", params)


def midi_set_clip_name(track_index, clip_index, name, view="session"):
    """Rename a clip."""
    return send("set_clip_name", {
        "track_index": track_index,
        "clip_index": clip_index,
        "name": name,
        "view": view,
    })


def midi_set_device_parameter(track_index, device_index, param_name,
                               value=None, display_value=None):
    """Set a device parameter by name or display value."""
    params = {
        "track_index": track_index,
        "device_index": device_index,
        "parameter_name": param_name,
    }
    if value is not None:
        params["value"] = value
    if display_value is not None:
        params["display_value"] = display_value
    return send("set_device_parameter", params)


def midi_set_track_mixer(track_index, volume=None, pan=None):
    """Set track volume and/or pan."""
    params = {"track_index": track_index}
    if volume is not None:
        params["volume"] = volume
    if pan is not None:
        params["pan"] = pan
    return send("set_track_mixer", params)


def midi_set_track_send(track_index, send_index, value=0.0):
    """Set a send level."""
    return send("set_track_send", {
        "track_index": track_index,
        "send_index": send_index,
        "value": value,
    })


def midi_set_track_mute(track_index, mute):
    """Mute/unmute a track."""
    return send("set_track_mute", {"track_index": track_index, "mute": mute})


def midi_set_track_solo(track_index, solo):
    """Solo/unsolo a track."""
    return send("set_track_solo", {"track_index": track_index, "solo": solo})


def midi_set_track_arm(track_index, arm):
    """Arm/disarm a track."""
    return send("set_track_arm", {"track_index": track_index, "arm": arm})


def midi_delete_device(track_index, device_index):
    """Delete a device from a track."""
    return send("delete_device", {"track_index": track_index, "device_index": device_index})


def midi_delete_track(track_index):
    """Delete a track."""
    return send("delete_track", {"track_index": track_index})


def midi_duplicate_track(track_index):
    """Duplicate a track."""
    return send("duplicate_track", {"track_index": track_index})


def midi_move_track(track_index, target_index):
    """Move a track to a new position."""
    return send("move_track", {"track_index": track_index, "target_index": target_index})


def midi_undo():
    """Undo last operation."""
    return send("undo")


def midi_redo():
    """Redo last undone operation."""
    return send("redo")


def midi_set_view(view_name):
    """Switch Live view (session/arrangement/detail)."""
    return send("set_view", {"view": view_name})


def midi_load_browser_item(track_index, uri="", item_path=None):
    """Load a browser item (device/plugin/sample) onto a track."""
    params = {"track_index": track_index}
    if uri:
        params["uri"] = uri
    if item_path:
        params["item_path"] = item_path
    return send("load_browser_item", params)


def midi_load_audio_clip(track_index, clip_index, file_path, view="session", start_time=0.0):
    """Load an audio file into a clip slot."""
    return send("load_audio_clip", {
        "track_index": track_index,
        "clip_index": clip_index,
        "file_path": file_path,
        "view": view,
        "start_time": start_time,
    })


def midi_set_clip_warping(track_index, clip_index, warping=False, view="session"):
    """Enable/disable clip warping."""
    return send("set_clip_warping", {
        "track_index": track_index,
        "clip_index": clip_index,
        "warping": warping,
        "view": view,
    })


def midi_set_clip_automation(track_index, clip_index, view="session",
                              parameter="volume", device_index=None,
                              points=None, replace=False):
    """Write automation envelope to a clip."""
    params = {
        "track_index": track_index,
        "clip_index": clip_index,
        "view": view,
        "parameter": parameter,
        "replace": replace,
    }
    if device_index is not None:
        params["device_index"] = device_index
    if points is not None:
        params["points"] = points
    return send("set_clip_automation", params)


def midi_get_clip_automation(track_index, clip_index, view="session",
                              parameter="volume", device_index=None):
    """Get clip automation envelope info."""
    params = {
        "track_index": track_index,
        "clip_index": clip_index,
        "view": view,
        "parameter": parameter,
    }
    if device_index is not None:
        params["device_index"] = device_index
    return send("get_clip_automation", params)


def midi_create_locators(locators):
    """Create locators in the arrangement."""
    return send("create_locators", {"locators": locators})


def midi_remove_notes_from_clip(track_index, clip_index, view="session"):
    """Remove all notes from a clip."""
    return send("remove_notes_from_clip", {
        "track_index": track_index,
        "clip_index": clip_index,
        "view": view,
    })


# ---------------------------------------------------------------------------
# Device insertion via browser (replaces device_helper.amxd)
# ---------------------------------------------------------------------------

# Maps common Ableton class_names to browser search paths
DEVICE_BROWSER_MAP = {
    "Eq8": {"category": "audio_effects", "search": "EQ Eight"},
    "Compressor": {"category": "audio_effects", "search": "Compressor"},
    "GlueCompressor": {"category": "audio_effects", "search": "Glue Compressor"},
    "Reverb": {"category": "audio_effects", "search": "Reverb"},
    "SimpleDelay": {"category": "audio_effects", "search": "Simple Delay"},
    "PingPongDelay": {"category": "audio_effects", "search": "Ping Pong Delay"},
    "FilterDelay": {"category": "audio_effects", "search": "Filter Delay"},
    "Saturator": {"category": "audio_effects", "search": "Saturator"},
    "Limiter": {"category": "audio_effects", "search": "Limiter"},
    "Utility": {"category": "audio_effects", "search": "Utility"},
    "MultibandDynamics": {"category": "audio_effects", "search": "Multiband Dynamics"},
    "AutoFilter": {"category": "audio_effects", "search": "Auto Filter"},
    "Gate": {"category": "audio_effects", "search": "Gate"},
    "Chorus": {"category": "audio_effects", "search": "Chorus"},
    "Flanger": {"category": "audio_effects", "search": "Flanger"},
    "Phaser": {"category": "audio_effects", "search": "Phaser"},
    "ChannelEq": {"category": "audio_effects", "search": "Channel EQ"},
    "EQ3": {"category": "audio_effects", "search": "EQ Three"},
    "DynamicTube": {"category": "audio_effects", "search": "Dynamic Tube"},
    "FrequencyShifter": {"category": "audio_effects", "search": "Frequency Shifter"},
    "BeatRepeat": {"category": "audio_effects", "search": "Beat Repeat"},
    "SpectralResonator": {"category": "audio_effects", "search": "Spectral Resonator"},
    "DrumRack": {"category": "drums", "search": "Drum Rack"},
    "InstrumentRack": {"category": "instruments", "search": "Instrument Rack"},
    "AudioEffectRack": {"category": "audio_effects", "search": "Audio Effect Rack"},
    "HybridReverb": {"category": "audio_effects", "search": "Hybrid Reverb"},
    "SurroundLimiter": {"category": "audio_effects", "search": "Surround Limiter"},
    "GrainDelay": {"category": "audio_effects", "search": "Grain Delay"},
    "AutoPan": {"category": "audio_effects", "search": "Auto Pan"},
}


def midi_insert_device(track_index, device_class):
    """Insert a native Ableton device via browser navigation.

    This replaces device_helper.amxd: instead of M4L, we search the browser
    and load the device by its UI name.
    """
    info = DEVICE_BROWSER_MAP.get(device_class)
    if not info:
        # Try searching with the class name directly
        info = {"category": "audio_effects", "search": device_class}

    # Navigate browser to find and load the device
    result = send("load_browser_item", {
        "track_index": track_index,
        "item_path": "{}/{}".format(info["category"], info["search"]),
    }, timeout=HEAVY_TIMEOUT)

    return result


def midi_insert_device_by_search(track_index, search_query):
    """Insert a device by searching the browser with a query string.

    More flexible than midi_insert_device: works with any device name,
    partial names, and third-party plugins.
    """
    # First try to scan browser and find matching item
    scan_result = send("scan_browser_full", {
        "categories": ["instruments", "audio_effects", "midi_effects", "drums"],
        "max_depth": 4,
    }, timeout=HEAVY_TIMEOUT)

    if scan_result.get("status") != "success":
        return scan_result

    items = scan_result.get("result", {}).get("items", [])
    query_lower = search_query.lower()

    # Find best match
    best_match = None
    for item in items:
        name = item.get("name", "").lower()
        if query_lower in name:
            if item.get("is_loadable"):
                best_match = item
                break
            if not best_match:
                best_match = item

    if not best_match:
        return {
            "status": "error",
            "message": "No matching device found for '{}' in browser".format(search_query),
        }

    # Load it
    return send("load_browser_item", {
        "track_index": track_index,
        "uri": best_match.get("uri", ""),
        "item_path": best_match.get("path", ""),
    }, timeout=HEAVY_TIMEOUT)
