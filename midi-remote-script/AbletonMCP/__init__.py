# AbletonMCP/__init__.py
"""
AbletonMCP — MIDI Remote Script for Ableton Live.

Bridge between any MCP client (opencode, Claude Desktop, custom agents) and
Ableton Live, running inside Live as a Control Surface.

Communication: TCP socket on 127.0.0.1:9878, JSON messages.
"""
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
from _Framework.Capabilities import (
    CONTROLLER_ID_KEY,
    PORTS_KEY,
    NOTES_CC,
    SCRIPT,
    REMOTE,
    controller_id,
    inport,
    outport,
)
import socket
import json
import threading
import time
import traceback
import os
import sys
import unicodedata

try:
    import hmac as _hmac
except ImportError:
    _hmac = None

try:
    import Queue as queue
except ImportError:
    import queue


def _norm_label(s):
    """Normaliza un nombre: minusculas, sin acentos, espacios colapsados."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split()).lower()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_MESSAGE_BYTES = 4 * 1024 * 1024  # 4 MB
MAX_BROWSER_SCAN_DEPTH = 8

DEFAULT_PORT = 9878
MAX_PORT_SCAN = 16
HOST = "127.0.0.1"

SCRIPT_VERSION = "1.4.4"

DEFAULT_MAIN_THREAD_TIMEOUT = 10
HEAVY_MAIN_THREAD_TIMEOUT = 45
HEAVY_MAIN_THREAD_COMMANDS = frozenset([
    "load_browser_item", "load_instrument_or_effect", "load_audio_clip",
    "scan_browser_full",
])


def _load_expected_token():
    candidates = []
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA", "")
    xdg = os.environ.get("XDG_DATA_HOME", "")

    for base in [
        os.path.join(home, "AppData", "Roaming", "ableton-mcp"),
        os.path.join(home, ".config", "ableton-mcp"),
        os.path.join(home, ".local", "share", "ableton-mcp"),
    ] if appdata else [
        os.path.join(home, ".config", "ableton-mcp"),
        os.path.join(home, ".local", "share", "ableton-mcp"),
    ]:
        candidates.append(os.path.join(base, "bridge_token"))

    for path in candidates:
        try:
            with open(path, "r") as f:
                token = f.read().strip()
                if len(token) >= 16:
                    return token
        except Exception:
            continue
    return ""


def _live_version_string():
    try:
        import Live
        ver = Live.Application.application().get_version()
        return ver
    except Exception:
        return "unknown"


def _live_major_version():
    try:
        import Live
        return Live.Application.application().get_major_version()
    except Exception:
        return 11


def _supports_arrangement_clip_creation(song):
    try:
        t = song.tracks[0]
        return hasattr(t, "create_midi_clip")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------
class F(ControlSurface):

    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._song = self.song()
        self._expected_token = _load_expected_token()
        self._browser_item_cache_by_uri = {}
        self._browser_item_cache_by_path = {}
        self._live_version = _live_version_string()
        self._live_major = _live_major_version()
        self._supports_arr_clip = None
        self.running = True
        self.start_server()
        self.show_message("F MIDI script v{} loaded".format(SCRIPT_VERSION))

    def disconnect(self):
        self.running = False
        ControlSurface.disconnect(self)

    # -----------------------------------------------------------------------
    # Socket server
    # -----------------------------------------------------------------------
    def start_server(self):
        self._server_socket = None
        self._port = DEFAULT_PORT
        for offset in range(MAX_PORT_SCAN):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # Windows: SO_REUSEADDR permite que DOS instancias compartan el
                # mismo puerto (entrega ambigua). Exigir exclusion para que la
                # 2a instancia falle el bind y pase al siguiente puerto.
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                except (AttributeError, OSError):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((HOST, DEFAULT_PORT + offset))
                s.listen(5)
                s.settimeout(1.0)
                self._server_socket = s
                self._port = DEFAULT_PORT + offset
                break
            except OSError:
                try:
                    s.close()
                except Exception:
                    pass
        if self._server_socket is None:
            self.log_message("F: no free port found in {}..{}".format(
                DEFAULT_PORT, DEFAULT_PORT + MAX_PORT_SCAN - 1))
            return
        self.log_message("F listening on 127.0.0.1:{}".format(self._port))
        t = threading.Thread(target=self._server_thread)
        t.daemon = True
        t.start()

    def _server_thread(self):
        while self.running:
            try:
                client, addr = self._server_socket.accept()
                ct = threading.Thread(target=self._handle_client, args=(client,))
                ct.daemon = True
                ct.start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.log_message("Server error: " + str(e))

    def _handle_client(self, client):
        client.settimeout(None)
        buffer = ""
        try:
            while self.running:
                try:
                    data = client.recv(8192)
                    if not data:
                        break
                    try:
                        buffer += data.decode("utf-8")
                    except AttributeError:
                        buffer += data

                    if len(buffer) > MAX_MESSAGE_BYTES:
                        break

                    try:
                        command = json.loads(buffer)
                        buffer = ""
                        response = self._process_command(command)
                        try:
                            client.sendall(json.dumps(response).encode("utf-8"))
                        except AttributeError:
                            client.sendall(json.dumps(response))
                    except ValueError:
                        continue
                except Exception as e:
                    if not isinstance(e, ValueError):
                        break
        finally:
            try:
                client.close()
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------------
    _AUTH_EXEMPT = frozenset(["get_session_info", "get_script_version", "ping"])

    def _authorized(self, command_type, token):
        expected = self._expected_token
        if not expected:
            return True
        if not token:
            return True
        if command_type in self._AUTH_EXEMPT:
            return True
        if _hmac:
            return _hmac.compare_digest(expected, token)
        return expected == token

    # -----------------------------------------------------------------------
    # Main-thread scheduling
    # -----------------------------------------------------------------------
    def _run_on_main(self, fn, timeout=None):
        rq = queue.Queue()

        def task():
            try:
                result = fn()
                rq.put({"status": "success", "result": result})
            except Exception as e:
                rq.put({"status": "error", "message": str(e)})

        try:
            self.schedule_message(0, task)
        except AssertionError:
            task()

        t = timeout or DEFAULT_MAIN_THREAD_TIMEOUT
        try:
            return rq.get(timeout=t)
        except queue.Empty:
            return {"status": "error", "message": "Timeout waiting for operation to complete"}

    # -----------------------------------------------------------------------
    # Command router
    # -----------------------------------------------------------------------
    def _process_command(self, command):
        ct = command.get("type", "")
        params = command.get("params", {})
        token = command.get("token", "")

        if not self._authorized(ct, token):
            return {"status": "error", "message": "Unauthorized"}

        response = {"status": "success", "result": {}}

        # --- Read-only commands (client thread, NO song access) ---
        READ_CMDS = {
            "ping": lambda: {"pong": True, "version": SCRIPT_VERSION,
                             "live_version": self._live_version,
                             "port": self._port},
            "get_script_version": self._get_script_version,
        }

        if ct in READ_CMDS:
            try:
                response["result"] = READ_CMDS[ct](**params) if params else READ_CMDS[ct]()
            except TypeError:
                response["result"] = READ_CMDS[ct]()
            return response

        # --- Read commands that access Live API (must go through main thread) ---
        MAIN_READ_CMDS = {
            "get_session_info": lambda p: self._get_session_info(),
            "get_track_info": lambda p: self._get_track_info(p.get("track_index", 0)),
            "get_arrangement_map": lambda p: self._get_arrangement_map(),
            "get_clip_audio_info": lambda p: self._get_clip_audio_info(
                p.get("track_index", 0), p.get("clip_index", 0), p.get("view", "session")),
            "get_device_parameters": lambda p: self._get_device_parameters(
                p.get("track_index", 0), p.get("device_index", 0)),
            "get_return_tracks": lambda p: self._get_return_tracks(),
            "get_scenes": lambda p: self._get_scenes(),
            "get_transport_state": lambda p: self._get_transport_state(),
            "scan_browser_full": lambda p: self._scan_browser_full(
                p.get("categories"), p.get("max_depth", 2)),
            "get_browser_item": lambda p: self._get_browser_item(p.get("uri", "")),
            "get_browser_categories": lambda p: self._get_browser_categories(),
            "get_browser_items": lambda p: self._get_browser_items(p.get("category", "")),
            "get_browser_tree": lambda p: self._get_browser_tree(p.get("category_type", "all")),
            "get_browser_items_at_path": lambda p: self._get_browser_items_at_path(p.get("path", "")),
            "get_browser_raw": lambda p: self._get_browser_raw(),
            "debug_clip": lambda p: self._debug_clip(
                p.get("track_index", 0), p.get("clip_index", 0)),
            "debug_song": lambda p: self._debug_song(),
            "debug_notes_env": lambda p: self._debug_notes_env(),
        }

        if ct in MAIN_READ_CMDS:
            response = self._run_on_main(lambda: MAIN_READ_CMDS[ct](params))
            return response

        # --- Main-thread commands ---
        MAIN_CMDS = {
            "create_midi_track": lambda p: self._create_midi_track(p.get("index", -1)),
            "create_audio_track": lambda p: self._create_audio_track(p.get("index", -1)),
            "set_track_name": lambda p: self._set_track_name(p["track_index"], p["name"]),
            "create_clip": lambda p: self._create_clip(p["track_index"], p.get("clip_index", 0), p.get("length", 4.0)),
            "create_arrangement_midi_clip": lambda p: self._create_arrangement_midi_clip(
                p["track_index"], p.get("start_time", 0.0), p.get("length", 4.0)),
            "add_notes_to_clip": lambda p: self._add_notes_to_clip(
                p["track_index"], p["clip_index"], p["notes"],
                p.get("clip_length"), p.get("view", "session")),
            "set_clip_name": lambda p: self._set_clip_name(
                p["track_index"], p["clip_index"], p["name"], p.get("view", "session")),
            "set_tempo": lambda p: self._set_tempo(p.get("tempo", 120.0)),
            "fire_clip": lambda p: self._fire_clip(p["track_index"], p["clip_index"]),
            "stop_clip": lambda p: self._stop_clip(p["track_index"], p["clip_index"]),
            "fire_scene": lambda p: self._fire_scene(p["scene_index"]),
            "start_playback": lambda p: self._start_playback(p.get("start_time")),
            "stop_playback": lambda p: self._stop_playback(),
            "load_browser_item": lambda p: self._load_browser_item(
                p["track_index"], p.get("uri", ""), p.get("item_path")),
            "load_instrument_or_effect": lambda p: self._load_browser_item(
                p["track_index"], p.get("uri", ""), p.get("item_path")),
            "set_device_parameter": lambda p: self._set_device_parameter(
                p["track_index"], p["device_index"], p["parameter_name"],
                p.get("value"), p.get("display_value")),
            "set_track_mixer": lambda p: self._set_track_mixer(
                p["track_index"], p.get("volume"), p.get("pan")),
            "set_track_send": lambda p: self._set_track_send(
                p["track_index"], p["send_index"], p.get("value", 0.0)),
            "set_track_mute": lambda p: self._set_track_property(p["track_index"], "mute", p.get("mute", False)),
            "set_track_solo": lambda p: self._set_track_property(p["track_index"], "solo", p.get("solo", False)),
            "set_track_arm": lambda p: self._set_track_property(p["track_index"], "arm", p.get("arm", False)),
            "delete_device": lambda p: self._delete_device(p["track_index"], p["device_index"]),
            "delete_track": lambda p: self._delete_track(p["track_index"]),
            "duplicate_track": lambda p: self._duplicate_track(p["track_index"]),
            "move_track": lambda p: self._move_track(p["track_index"], p["target_index"]),
            "undo": lambda p: self._undo(),
            "redo": lambda p: self._redo(),
            "set_metronome": lambda p: self._set_metronome(p.get("enabled", False)),
            "get_clip_notes": lambda p: self._get_clip_notes(
                p["track_index"], p["clip_index"], p.get("view", "session"), p.get("summary_only", False)),
            "remove_notes_from_clip": lambda p: self._remove_notes_from_clip(
                p["track_index"], p["clip_index"], p.get("view", "session")),
            "set_view": lambda p: self._set_view(p.get("view", "session")),
            "load_audio_clip": lambda p: self._load_audio_clip(
                p["track_index"], p["clip_index"], p["file_path"],
                p.get("view", "session"), p.get("start_time", 0.0)),
            "set_clip_warping": lambda p: self._set_clip_warping(
                p["track_index"], p["clip_index"], p.get("warping", False), p.get("view", "session")),
            "set_clip_automation": lambda p: self._set_clip_automation(
                p["track_index"], p["clip_index"], p.get("view", "session"),
                p.get("parameter", "volume"), p.get("device_index"),
                p.get("points", []), p.get("replace", False)),
            "get_clip_automation": lambda p: self._get_clip_automation(
                p["track_index"], p["clip_index"], p.get("view", "session"),
                p.get("parameter", "volume"), p.get("device_index")),
            "create_locators": lambda p: self._create_locators(p.get("locators", [])),
            "save_song": lambda p: self._save_song(),
            "delete_arrangement_clip": lambda p: self._delete_arrangement_clip(
                p["track_index"], p["clip_index"]),
        }

        if ct in MAIN_CMDS:
            heavy = ct in HEAVY_MAIN_THREAD_COMMANDS
            timeout = HEAVY_MAIN_THREAD_TIMEOUT if heavy else DEFAULT_MAIN_THREAD_TIMEOUT
            response = self._run_on_main(lambda: MAIN_CMDS[ct](params), timeout)
            return response

        response["status"] = "error"
        response["message"] = "Unknown command: {}".format(ct)
        return response

    # =======================================================================
    # READ-ONLY HANDLERS
    # =======================================================================

    def _get_session_info(self):
        s = self._song
        tracks_info = []
        for idx, t in enumerate(s.tracks):
            info = {
                "name": t.name,
                "index": idx,
                "is_midi": t.has_midi_input if hasattr(t, 'has_midi_input') else False,
                "is_audio": t.has_audio_input if hasattr(t, 'has_audio_input') else False,
                "is_return": False,
                "is_master": False,
            }
            if hasattr(t, 'is_group_track'):
                info["is_group"] = t.is_group_track
            tracks_info.append(info)
        returns = []
        for idx, r in enumerate(s.return_tracks):
            returns.append({"name": r.name, "index": idx})
        return {
            "tempo": s.tempo,
            "time_signature_numerator": s.signature_numerator,
            "time_signature_denominator": s.signature_denominator,
            "track_count": len(s.tracks),
            "return_count": len(s.return_tracks),
            "song_name": getattr(s, "name", "") or getattr(s, "current_song", "") or "",
            "port": self._port,
            "tracks": tracks_info,
            "returns": returns,
            "master_volume": s.master_track.mixer_volume.value if hasattr(s.master_track, 'mixer_volume') else 1.0,
            "script_version": SCRIPT_VERSION,
            "live_version": self._live_version,
            "live_major": self._live_major,
        }

    def _get_script_version(self):
        return {
            "script_version": SCRIPT_VERSION,
            "live_version": self._live_version,
            "live_major": self._live_major,
        }

    def _get_track_info(self, track_index=0):
        s = self._song
        if track_index < 0 or track_index >= len(s.tracks):
            return {"error": "Invalid track index"}
        t = list(s.tracks)[track_index]

        clip_slots = []
        for i, cs in enumerate(t.clip_slots):
            slot = {"index": i, "has_clip": cs.has_clip, "is_playing": cs.is_playing}
            if cs.has_clip:
                clip = cs.clip
                slot["clip"] = {
                    "name": clip.name,
                    "length": clip.length,
                    "is_midi": hasattr(clip, "notes") and callable(getattr(clip, "get_notes", None)),
                    "color": clip.color_index if hasattr(clip, "color_index") else None,
                }
                if not slot["clip"]["is_midi"]:
                    try:
                        slot["clip"]["audio_info"] = self._audio_clip_info(clip)
                    except Exception:
                        pass
            clip_slots.append(slot)

        arr_clips = []
        for ac in t.arrangement_clips:
            info = {
                "name": ac.name,
                "start": ac.start_time,
                "end": ac.end_time,
                "length": ac.length,
                "color": ac.color_index if hasattr(ac, "color_index") else None,
            }
            is_midi = hasattr(ac, "notes") and callable(getattr(ac, "get_notes", None))
            info["is_midi"] = is_midi
            if is_midi:
                try:
                    info["note_count"] = len(ac.notes)
                except Exception:
                    info["note_count"] = 0
            else:
                try:
                    info["audio_info"] = self._audio_clip_info(ac)
                except Exception:
                    pass
            arr_clips.append(info)

        devices = []
        for i, d in enumerate(t.devices):
            devs = {
                "index": i,
                "name": d.name,
                "type": str(d.type) if hasattr(d, "type") else "unknown",
                "is_on": d.is_on if hasattr(d, "is_on") else True,
            }
            devices.append(devs)

        sends = []
        for i, s_track in enumerate(s.return_tracks):
            try:
                sends.append({
                    "index": i,
                    "name": s_track.name,
                    "value": t.mixer_volume.value if hasattr(t, "mixer_sends") and i < len(t.mixer_sends) else 0.0,
                })
            except Exception:
                sends.append({"index": i, "value": 0.0})

        return {
            "name": t.name,
            "index": track_index,
            "is_midi": getattr(t, "has_midi_input", False),
            "is_audio": getattr(t, "has_audio_input", False),
            "is_group": getattr(t, "is_group_track", False),
            "is_frozen": t.is_frozen if hasattr(t, "is_frozen") else False,
            "mute": t.mute,
            "solo": t.solo,
            "arm": t.arm if hasattr(t, "arm") else False,
            "volume": t.mixer_volume.value if hasattr(t, "mixer_volume") else 1.0,
            "pan": t.mixer_panorama.value if hasattr(t, "mixer_panorama") else 0.0,
            "clip_slots": clip_slots,
            "arrangement_clips": arr_clips,
            "devices": devices,
            "sends": sends,
        }

    def _get_arrangement_map(self):
        s = self._song
        try:
            song_length = s.get_song_length() if hasattr(s, "get_song_length") else 0
        except Exception:
            song_length = 0

        locators = []
        try:
            for lp in s.arrangement.cue_points:
                locators.append({"name": lp.name, "time": lp.time})
        except Exception:
            pass

        tracks = []
        for i, t in enumerate(s.tracks):
            clips = []
            for ac in t.arrangement_clips:
                ci = {
                    "name": ac.name,
                    "start": ac.start_time,
                    "end": ac.end_time,
                    "length": ac.length,
                }
                is_midi = hasattr(ac, "notes") and callable(getattr(ac, "get_notes", None))
                ci["is_midi"] = is_midi
                if is_midi:
                    try:
                        ci["note_count"] = len(ac.notes)
                    except Exception:
                        ci["note_count"] = 0
                else:
                    try:
                        ci["audio_info"] = self._audio_clip_info(ac)
                    except Exception:
                        pass
                clips.append(ci)
            tracks.append({
                "name": t.name,
                "index": i,
                "clips": clips,
            })

        return {
            "song_length": song_length,
            "song_length_bars": int(song_length / (s.signature_numerator * 4.0 / s.signature_denominator)) if song_length else 0,
            "locators": locators,
            "tracks": tracks,
        }

    def _get_clip_audio_info(self, track_index=0, clip_index=0, view="session"):
        s = self._song
        if view == "arrangement":
            t = list(s.tracks)[track_index]
            clips = list(t.arrangement_clips)
            if clip_index >= len(clips):
                return {"error": "No clip at that index"}
            clip = clips[clip_index]
        else:
            t = list(s.tracks)[track_index]
            cs = t.clip_slots[clip_index]
            if not cs.has_clip:
                return {"error": "No clip in that slot"}
            clip = cs.clip
        return self._audio_clip_info(clip)

    def _audio_clip_info(self, clip):
        info = {
            "name": clip.name if hasattr(clip, "name") else "",
            "length": clip.length if hasattr(clip, "length") else 0,
        }
        try:
            if hasattr(clip, "file_path"):
                info["file_path"] = clip.file_path
            if hasattr(clip, "sample_rate"):
                info["sample_rate"] = clip.sample_rate
            if hasattr(clip, "sample_length"):
                info["sample_length"] = clip.sample_length
            if hasattr(clip, "warping"):
                info["warping"] = clip.warping
            if hasattr(clip, "start_marker"):
                info["start_marker"] = clip.start_marker
                info["end_marker"] = clip.end_marker
                info["loop_start"] = clip.loop_start
                info["loop_end"] = clip.loop_end
            if hasattr(clip, "warp_markers"):
                markers = []
                for wm in clip.warp_markers:
                    markers.append({
                        "beat_time": wm.beat_time,
                        "sample_time": wm.sample_time,
                    })
                info["warp_markers"] = markers
        except Exception:
            pass
        return info

    def _get_device_parameters(self, track_index=0, device_index=0):
        s = self._song
        if track_index >= len(s.tracks):
            return {"error": "Invalid track"}
        t = list(s.tracks)[track_index]
        if device_index >= len(t.devices):
            return {"error": "Invalid device"}
        d = list(t.devices)[device_index]
        params = []
        for i, p in enumerate(d.parameters):
            pd = {
                "index": i,
                "name": p.name if hasattr(p, "name") else "param_{}".format(i),
                "value": p.value if hasattr(p, "value") else 0.0,
                "min": p.min if hasattr(p, "min") else 0.0,
                "max": p.max if hasattr(p, "max") else 1.0,
            }
            if hasattr(p, "str_for_value"):
                try:
                    pd["display_value"] = p.str_for_value(p.value)
                    pd["display_min"] = p.str_for_value(p.min)
                    pd["display_max"] = p.str_for_value(p.max)
                except Exception:
                    pass
            if hasattr(p, "is_quantized"):
                pd["is_quantized"] = p.is_quantized
            params.append(pd)
        return {
            "device_name": d.name,
            "device_index": device_index,
            "parameters": params,
        }

    def _get_return_tracks(self):
        s = self._song
        returns = []
        for i, r in enumerate(s.return_tracks):
            rd = {
                "name": r.name,
                "index": i,
                "volume": r.mixer_volume.value if hasattr(r, "mixer_volume") else 1.0,
                "pan": r.mixer_panorama.value if hasattr(r, "mixer_panorama") else 0.0,
            }
            devs = []
            for j, d in enumerate(r.devices):
                devs.append({"index": j, "name": d.name, "type": str(d.type) if hasattr(d, "type") else "unknown"})
            rd["devices"] = devs
            returns.append(rd)
        return {"return_tracks": returns}

    def _get_scenes(self):
        s = self._song
        scenes = []
        for i, sc in enumerate(s.scenes):
            clips = []
            for j, cs in enumerate(sc.clip_slots):
                clips.append({"track_index": j, "has_clip": cs.has_clip})
            scenes.append({
                "index": i,
                "name": sc.name if hasattr(sc, "name") else "",
                "clip_count": sum(1 for cs in sc.clip_slots if cs.has_clip),
                "clips": clips,
            })
        return {"scenes": scenes}

    def _get_transport_state(self):
        s = self._song
        return {
            "is_playing": s.is_playing,
            "tempo": s.tempo,
            "time_signature_numerator": s.signature_numerator,
            "time_signature_denominator": s.signature_denominator,
            "metronome": s.metronome if hasattr(s, "metronome") else False,
            "current_song_time": s.get_current_song_time() if hasattr(s, "get_current_song_time") else 0,
            "last_event_time": s.last_event_time if hasattr(s, "last_event_time") else 0,
        }

    def _get_clip_notes(self, track_index=0, clip_index=0, view="session", summary_only=False):
        s = self._song
        if view == "arrangement":
            t = list(s.tracks)[track_index]
            clips = list(t.arrangement_clips)
            if clip_index >= len(clips):
                return {"error": "No clip at that index"}
            clip = clips[clip_index]
        else:
            t = list(s.tracks)[track_index]
            cs = t.clip_slots[clip_index]
            if not cs.has_clip:
                return {"error": "No clip in slot"}
            clip = cs.clip

        notes = []
        err = None
        raw_notes = None
        # Live 12: get_notes (tuplas) funciona; get_notes_extended devuelve vacio
        try:
            raw_notes = clip.get_notes(0, 0, clip.length, 128)
        except Exception as exc:
            err = str(exc)
        if not raw_notes:
            try:
                alt = clip.get_notes_extended(0, 0, clip.length, 128)
                if alt:
                    raw_notes = alt
                    err = None
            except Exception as exc:
                err = err or str(exc)

        for n in (raw_notes or []):
            if isinstance(n, dict):
                nd = {
                    "pitch": n.get("pitch", 60),
                    "start": n.get("start_time", n.get("start", 0.0)),
                    "duration": n.get("duration", 0.0),
                    "velocity": n.get("velocity", 100),
                }
            elif isinstance(n, (tuple, list)):
                nd = {
                    "pitch": n[0] if len(n) > 0 else 60,
                    "start": n[1] if len(n) > 1 else 0.0,
                    "duration": n[2] if len(n) > 2 else 0.0,
                    "velocity": n[3] if len(n) > 3 else 100,
                }
            else:
                nd = {
                    "pitch": getattr(n, "pitch", 60),
                    "start": getattr(n, "start_time", getattr(n, "start", 0.0)),
                    "duration": getattr(n, "duration", 0.0),
                    "velocity": getattr(n, "velocity", 100),
                }
            if hasattr(n, "mute"):
                nd["mute"] = n.mute
            nd["start_time"] = nd["start"]
            notes.append(nd)

        if err and not notes:
            return {"error": "get_notes fallo: {}".format(err), "notes": [], "note_count": 0}
        if summary_only:
            return {"note_count": len(notes), "clip_name": clip.name}
        return {"notes": notes, "note_count": len(notes)}

    def _get_clip_automation(self, track_index=0, clip_index=0, view="session", parameter="volume", device_index=None):
        s = self._song
        if view == "arrangement":
            t = list(s.tracks)[track_index]
            clips = list(t.arrangement_clips)
        else:
            t = list(s.tracks)[track_index]
            clips = [cs.clip for cs in t.clip_slots if cs.has_clip]

        if clip_index >= len(clips):
            return {"error": "No clip"}
        clip = clips[clip_index]

        param = self._resolve_parameter(t, parameter, device_index)
        if not param:
            return {"error": "Parameter not found: {}".format(parameter)}

        try:
            envelope = clip.find_box_envelope(param)
            if envelope:
                return {"has_envelope": True, "parameter": parameter}
            return {"has_envelope": False}
        except Exception:
            return {"has_envelope": False}

    # =======================================================================
    # BROWSER HANDLERS (run on client thread)
    # =======================================================================

    def _application(self):
        try:
            return self.application()
        except Exception:
            try:
                import Live
                return Live.Application.application()
            except Exception:
                return None

    def _scan_browser_full(self, categories=None, max_depth=6):
        # Compat: llamadas antiguas pasaban max_depth posicional en categories
        if isinstance(categories, int):
            max_depth = categories
            categories = None
        if isinstance(categories, str):
            categories = [c.strip() for c in categories.split(",") if c.strip()]
        if not isinstance(categories, (list, tuple)) or not categories:
            categories = ["instruments", "sounds", "drums", "audio_effects",
                          "midi_effects", "packs", "plugins"]
        try:
            max_depth = min(int(max_depth), MAX_BROWSER_SCAN_DEPTH)
        except Exception:
            max_depth = 6
        app = self._application()
        if not app or not hasattr(app, "browser"):
            return {"error": "Browser not available"}

        browser = app.browser

        # Cache TTL para no congelar la UI de Live en cada scan
        now = time.time()
        cache_key = (tuple(str(c).lower() for c in categories), max_depth)
        cached = getattr(self, "_scan_cache", None)
        if cached and cached[0] == cache_key and now - cached[1] < 300:
            return {"items": list(cached[2]), "count": len(cached[2]), "cached": True}

        results = []
        cat_map = {}
        try:
            for cat in (browser.categories or []):
                cat_map[str(getattr(cat, "name", ""))] = cat
        except Exception:
            cat_map = {}

        scanned = []
        missing = []
        for cat_name in categories:
            root = self._resolve_browser_root(browser, str(cat_name), cat_map)
            if root is None:
                missing.append(str(cat_name))
                continue
            scanned.append(str(cat_name))
            self._walk_browser(root, str(cat_name), results, 0, max_depth)

        self._browser_item_cache_by_uri = {}
        self._browser_item_cache_by_path = {}
        for item in results:
            if "uri" in item:
                self._browser_item_cache_by_uri[item["uri"]] = item
            if "path" in item:
                self._browser_item_cache_by_path[item["path"]] = item

        self._scan_cache = (cache_key, now, list(results))
        return {"items": results, "count": len(results),
                "scanned": scanned, "missing": missing}

    def _resolve_browser_root(self, browser, key, cat_map):
        """Resuelve una categoria del browser: 1) atributo directo del LOM,
        2) sinonimos EN/ES contra browser.categories (nombres localizados)."""
        k = str(key).lower()
        attr = self._BROWSER_CATEGORY_ATTRS.get(k)
        if attr:
            try:
                if hasattr(browser, attr):
                    return getattr(browser, attr)
            except Exception:
                pass
        syns = [_norm_label(s) for s in self._BROWSER_CATEGORY_SYNONYMS.get(k, (k,))]
        for name, cat in cat_map.items():
            n = _norm_label(name)
            for s in syns:
                if not s:
                    continue
                if n == s or n.startswith(s) or s in n:
                    return cat
        return None

    def _debug_song(self):
        s = self._song
        return {"type": type(s).__name__,
                "sav_methods": [a for a in dir(s)
                                if "sav" in a.lower() or "export" in a.lower()]}

    def _debug_notes_env(self):
        import Live
        out = {}
        try:
            out["live_modules"] = [m for m in dir(Live) if "idi" in m.lower() or "pec" in m.lower()]
        except Exception as exc:
            out["live_modules"] = "ERR: {}".format(exc)
        try:
            import Live.Clip as LC
            out["clip_module_attrs"] = [a for a in dir(LC) if "ote" in a or "pec" in a]
        except Exception as exc:
            out["clip_module_attrs"] = "ERR: {}".format(exc)
        s = self._song
        try:
            t = list(s.tracks)[0]
            for cs in t.clip_slots:
                if cs.has_clip:
                    clip = cs.clip
                    out["clip_type"] = type(clip).__name__
                    out["clip_note_methods"] = [a for a in dir(clip)
                                                if ("note" in a.lower() and callable(getattr(clip, a)))
                                                or a in ("add_new_notes", "get_notes_extended",
                                                         "remove_notes_extended", "apply_note_modifications")]
                    break
        except Exception as exc:
            out["clip_probe"] = "ERR: {}".format(exc)
        return out

    def _debug_clip(self, track_index, clip_index):
        s = self._song
        t = list(s.tracks)[track_index]
        cs = t.clip_slots[clip_index]
        info = {"has_clip": bool(cs.has_clip)}
        if not cs.has_clip:
            return info
        clip = cs.clip
        info["name"] = clip.name
        info["length"] = clip.length
        try:
            info["notes_legacy"] = len(clip.get_notes(0, 0, clip.length + 1, 128))
        except Exception as exc:
            info["notes_legacy"] = "ERR: {}".format(str(exc)[:120])
        try:
            ext = clip.get_notes_extended(0, 0, clip.length + 1, 128)
            info["notes_extended"] = len(ext) if ext is not None else -1
        except Exception as exc:
            info["notes_extended"] = "ERR: {}".format(str(exc)[:120])
        return info

    def _get_browser_raw(self):
        app = self._application()
        b = getattr(app, "browser", None) if app else None
        if b is None:
            return {"error": "no browser"}
        attrs = {}
        for a in ("categories", "children", "instruments", "sounds", "drums",
                  "audio_effects", "midi_effects", "packs", "plugins",
                  "samples", "user_library", "clips"):
            try:
                attrs[a] = hasattr(b, a)
            except Exception:
                attrs[a] = "err"
        cats = []
        try:
            cats = [str(getattr(c, "name", "")) for c in (b.categories or [])]
        except Exception as e:
            cats = ["ERR: {}".format(e)]
        children = []
        try:
            children = [str(getattr(c, "name", "")) for c in (b.children or [])][:20]
        except Exception as e:
            children = ["ERR: {}".format(e)]
        return {"attrs": attrs, "category_names": cats, "children": children}

    def _walk_browser(self, node, path, results, depth, max_depth):
        if depth > max_depth:
            return
        try:
            children = list(node.children) if hasattr(node, "children") else []
        except Exception:
            return
        for child in children:
            item = {
                "name": child.name if hasattr(child, "name") else str(child),
                "path": "{}/{}".format(path, child.name) if hasattr(child, "name") else path,
                "depth": depth,
            }
            if hasattr(child, "uri"):
                item["uri"] = child.uri
            if hasattr(child, "is_loadable"):
                item["is_loadable"] = child.is_loadable
            if hasattr(child, "is_device"):
                item["is_device"] = child.is_device
            results.append(item)
            if hasattr(child, "children") and depth < max_depth:
                try:
                    sub = list(child.children)
                    if sub:
                        self._walk_browser(child, item["path"], results, depth + 1, max_depth)
                except Exception:
                    pass

    def _get_browser_item(self, uri=None, path=None):
        if uri and uri in self._browser_item_cache_by_uri:
            return self._browser_item_cache_by_uri[uri]
        if path and path in self._browser_item_cache_by_path:
            return self._browser_item_cache_by_path[path]
        return {"error": "Item not found"}

    def _get_browser_categories(self, category_type="all"):
        return self._get_browser_tree(category_type)

    def _get_browser_items(self, path="", item_type="all"):
        return self._get_browser_items_at_path(path)

    _BROWSER_CATEGORY_ATTRS = {
        "instruments": "instruments",
        "sounds": "sounds",
        "drums": "drums",
        "audio_effects": "audio_effects",
        "midi_effects": "midi_effects",
        "packs": "packs",
        "max_for_live": "max_for_live",
        "plugins": "plugins",
        "samples": "samples",
        "user_library": "user_library",
        "clips": "clips",
    }

    _BROWSER_CATEGORY_SYNONYMS = {
        "instruments": ("instrumentos", "instruments"),
        "sounds": ("sonidos", "sounds"),
        "drums": ("drums", "baterias", "bateria"),
        "audio_effects": ("efectos de audio", "efectos audio", "audio effects"),
        "midi_effects": ("efectos midi", "midi effects"),
        "packs": ("packs",),
        "plugins": ("plugins", "plug-ins"),
        "samples": ("samples", "muestras"),
        "user_library": ("user library", "biblioteca del usuario", "user_library"),
        "clips": ("clips",),
        "max_for_live": ("max for live", "maxForLive".lower()),
    }

    def _get_browser_tree(self, category_type="all"):
        app = self._application()
        if not app or not hasattr(app, "browser"):
            return {"error": "No browser"}
        cats = []
        seen = set()
        try:
            for cat in (app.browser.categories or []):
                nm = str(getattr(cat, "name", ""))
                if nm and nm.lower() not in seen:
                    seen.add(nm.lower())
                    cats.append({"name": nm, "type": "category"})
        except Exception:
            pass
        try:
            for key, attr in self._BROWSER_CATEGORY_ATTRS.items():
                if hasattr(app.browser, attr):
                    cats.append({"name": key, "type": "attr"})
        except Exception:
            pass
        if category_type != "all":
            ct = _norm_label(category_type)
            filtered = [c for c in cats if _norm_label(c["name"]) == ct]
            if not filtered:
                syns = self._BROWSER_CATEGORY_SYNONYMS.get(ct, (ct,))
                filtered = [c for c in cats
                            if any(_norm_label(s) == _norm_label(c["name"]) for s in syns)]
            cats = filtered
        return {"categories": cats}

    def _get_browser_items_at_path(self, path=""):
        app = self._application()
        if not app or not hasattr(app, "browser"):
            return {"error": "No browser"}

        parts = [p for p in path.split("/") if p]
        current = None
        try:
            for i, part in enumerate(parts):
                if i == 0:
                    attr = self._BROWSER_CATEGORY_ATTRS.get(part.lower())
                    if attr and hasattr(app.browser, attr):
                        current = getattr(app.browser, attr)
                        continue
                    # Resolver por sinonimos contra los hijos del browser
                    # (nombres localizados, p.ej. "Instrumentos")
                    target = _norm_label(part)
                    syns = [_norm_label(s) for s in
                            self._BROWSER_CATEGORY_SYNONYMS.get(part.lower(), (part,))]
                    found_first = False
                    for child in getattr(app.browser, "children", []):
                        nm = _norm_label(getattr(child, "name", ""))
                        if nm == target or any(s and (nm == s or s in nm or nm in s)
                                               for s in syns):
                            current = child
                            found_first = True
                            break
                    if not found_first:
                        return {"error": "Path not found: {}".format(path)}
                    continue
                found = False
                for child in getattr(current, "children", []):
                    if getattr(child, "name", "") == part:
                        current = child
                        found = True
                        break
                if not found:
                    return {"error": "Path not found: {}".format(path)}
        except Exception as e:
            return {"error": str(e)}

        items = []
        try:
            children = list(getattr(current, "children", []))
            for child in children:
                item = {"name": getattr(child, "name", "") or str(child)}
                if hasattr(child, "uri"):
                    item["uri"] = child.uri
                if hasattr(child, "is_loadable"):
                    item["is_loadable"] = child.is_loadable
                if hasattr(child, "is_device"):
                    item["is_device"] = child.is_device
                items.append(item)
        except Exception:
            pass
        return {"items": items, "path": path}

    # =======================================================================
    # MAIN-THREAD HANDLERS
    # =======================================================================

    def _create_midi_track(self, index=-1):
        s = self._song
        t = s.create_midi_track(index)
        return {"track_index": list(s.tracks).index(t), "name": t.name}

    def _create_audio_track(self, index=-1):
        s = self._song
        t = s.create_audio_track(index)
        return {"track_index": list(s.tracks).index(t), "name": t.name}

    def _set_track_name(self, track_index, name):
        s = self._song
        t = list(s.tracks)[track_index]
        t.name = name
        return {"ok": True}

    def _create_clip(self, track_index, clip_index, length=4.0):
        s = self._song
        t = list(s.tracks)[track_index]
        cs = t.clip_slots[clip_index]
        clip = cs.create_clip(length)
        return {"ok": True, "clip_name": clip.name if hasattr(clip, "name") else "",
                "length": length}

    def _create_arrangement_midi_clip(self, track_index, start_time, length):
        s = self._song
        t = list(s.tracks)[track_index]
        if self._supports_arr_clip is None:
            self._supports_arr_clip = _supports_arrangement_clip_creation(s)
        if self._supports_arr_clip:
            t.create_midi_clip(start_time, start_time + length)
        else:
            t.create_clip(length)
        clips = list(t.arrangement_clips)
        return {"ok": True, "start": start_time, "length": length,
                "arrangement_clip_index": (len(clips) - 1) if clips else 0}

    def _add_notes_to_clip(self, track_index, clip_index, notes, clip_length=None, view="session"):
        s = self._song
        t = list(s.tracks)[track_index]

        if view == "arrangement":
            clips = list(t.arrangement_clips)
            clip = clips[clip_index] if clip_index < len(clips) else None
        else:
            cs = t.clip_slots[clip_index]
            clip = cs.clip if cs.has_clip else None

        if not clip and clip_length:
            if view == "session":
                cs = t.clip_slots[clip_index]
                clip = cs.create_clip(clip_length)
            else:
                t.create_midi_clip(clip_index, clip_index + clip_length)
                clips = list(t.arrangement_clips)
                clip = clips[-1] if clips else None

        if not clip:
            return {"error": "No clip found"}

        note_specs = []
        for n in notes:
            note_specs.append({
                "pitch": n.get("pitch", 60),
                "start_time": n.get("start_time", n.get("start", 0.0)),
                "duration": n.get("duration", 0.25),
                "velocity": n.get("velocity", 100),
                "mute": bool(n.get("mute", False)),
            })

        # Vía moderna ÚNICA (Live 11+): add_new_notes con MidiNoteSpecification.
        # La clase vive en Live.Clip, NO en Live raíz.
        # Los métodos legacy (set_notes) disparan el aviso MPE de Live 12.
        from Live.Clip import MidiNoteSpecification
        try:
            specs = tuple(
                MidiNoteSpecification(int(ns["pitch"]), float(ns["start_time"]),
                                      float(ns["duration"]), int(ns["velocity"]), ns["mute"])
                for ns in note_specs
            )
            clip.add_new_notes(specs)
        except Exception as exc:
            return {"error": "no se pudieron agregar las notas: {}".format(exc)}
        added = len(note_specs)

        if clip_length:
            try:
                clip.loop_end = clip_length
            except Exception:
                pass

        return {"ok": True, "note_count": added}

    def _set_clip_name(self, track_index, clip_index, name, view="session"):
        s = self._song
        t = list(s.tracks)[track_index]
        if view == "arrangement":
            clips = list(t.arrangement_clips)
            clip = clips[clip_index] if clip_index < len(clips) else None
        else:
            cs = t.clip_slots[clip_index]
            clip = cs.clip if cs.has_clip else None
        if clip:
            clip.name = name
            return {"ok": True}
        return {"error": "No clip found"}

    def _set_tempo(self, tempo):
        self._song.tempo = max(20.0, min(999.0, float(tempo)))
        return {"tempo": self._song.tempo}

    def _fire_clip(self, track_index, clip_index):
        s = self._song
        t = list(s.tracks)[track_index]
        cs = t.clip_slots[clip_index]
        if cs.has_clip:
            cs.fire()
            return {"ok": True}
        return {"error": "No clip in slot"}

    def _stop_clip(self, track_index, clip_index):
        s = self._song
        t = list(s.tracks)[track_index]
        cs = t.clip_slots[clip_index]
        if cs.has_clip:
            cs.stop()
            return {"ok": True}
        return {"error": "No clip in slot"}

    def _fire_scene(self, scene_index):
        s = self._song
        sc = list(s.scenes)[scene_index]
        sc.fire()
        return {"ok": True}

    def _start_playback(self, start_time=None):
        s = self._song
        if start_time is not None:
            s.set_current_song_time(float(start_time))
        s.start_playing()
        return {"ok": True}

    def _stop_playback(self):
        self._song.stop_playing()
        return {"ok": True}

    def _load_browser_item(self, track_index, uri="", item_path=None):
        s = self._song
        t = list(s.tracks)[track_index]
        if getattr(t, "is_group_track", False):
            return {"error": "Cannot load device on a group track"}

        item = None
        if uri and uri in self._browser_item_cache_by_uri:
            item = self._browser_item_cache_by_uri[uri]["_raw"] if "_raw" in self._browser_item_cache_by_uri[uri] else None

        if not item and item_path:
            parts = [p for p in item_path.split("/") if p]
            app = self._application()
            if app and hasattr(app, "browser"):
                current = None
                try:
                    for i, part in enumerate(parts):
                        if i == 0:
                            attr = self._BROWSER_CATEGORY_ATTRS.get(part.lower())
                            if attr and hasattr(app.browser, attr):
                                current = getattr(app.browser, attr)
                                continue
                            found_first = False
                            for child in getattr(app.browser, "children", []):
                                if getattr(child, "name", "").lower() == part.lower():
                                    current = child
                                    found_first = True
                                    break
                            if not found_first:
                                return {"error": "Path not found: {}".format(item_path)}
                            continue
                        found = False
                        for child in getattr(current, "children", []):
                            if getattr(child, "name", "") == part:
                                current = child
                                found = True
                                break
                        if not found:
                            return {"error": "Path not found: {}".format(item_path)}
                    item = current
                except Exception as e:
                    return {"error": str(e)}

        if not item:
            return {"error": "Browser item not found"}

        try:
            app = self._application()
            if not app or not hasattr(app, "browser"):
                return {"error": "Browser not available"}
            s.view.selected_track = t
            app.browser.load_item(item)
            return {"ok": True}
        except Exception as e:
            return {"error": "Failed to load: {}".format(str(e))}

    def _resolve_parameter(self, track, param_name, device_index=None):
        if param_name == "volume":
            return track.mixer_volume if hasattr(track, "mixer_volume") else None
        if param_name == "pan" or param_name == "panorama":
            return track.mixer_panorama if hasattr(track, "mixer_panorama") else None

        devices = list(track.devices)
        if device_index is not None and 0 <= device_index < len(devices):
            for p in devices[device_index].parameters:
                if p.name.lower() == param_name.lower():
                    return p
        else:
            for d in devices:
                for p in d.parameters:
                    if p.name.lower() == param_name.lower():
                        return p
        return None

    def _set_device_parameter(self, track_index, device_index, param_name, value=None, display_value=None):
        s = self._song
        t = list(s.tracks)[track_index]

        param = self._resolve_parameter(t, param_name, device_index)
        if not param:
            return {"error": "Parameter not found: {}".format(param_name)}

        if display_value is not None:
            found = False
            pmin = param.min if hasattr(param, "min") else 0.0
            pmax = param.max if hasattr(param, "max") else 1.0

            for _ in range(40):
                mid = (pmin + pmax) / 2.0
                try:
                    s_val = param.str_for_value(mid)
                except Exception:
                    break
                if s_val == display_value:
                    param.value = mid
                    found = True
                    break
                elif s_val < display_value:
                    pmin = mid
                else:
                    pmax = mid

            if not found:
                try:
                    step = (param.max - param.min) / 64.0
                    for i in range(65):
                        v = param.min + step * i
                        if param.str_for_value(v) == display_value:
                            param.value = v
                            found = True
                            break
                except Exception:
                    pass

            if not found:
                return {"error": "Display value '{}' not found for parameter '{}'".format(display_value, param_name)}
        elif value is not None:
            param.value = float(value)
        else:
            return {"error": "Provide value or display_value"}

        try:
            return {"ok": True, "display_value": param.str_for_value(param.value)}
        except Exception:
            return {"ok": True}

    def _set_track_mixer(self, track_index, volume=None, pan=None):
        s = self._song
        t = list(s.tracks)[track_index]
        if volume is not None:
            t.mixer_volume.value = max(0.0, min(1.0, float(volume)))
        if pan is not None:
            t.mixer_panorama.value = max(-1.0, min(1.0, float(pan)))
        return {"ok": True}

    def _set_track_send(self, track_index, send_index, value=0.0):
        s = self._song
        t = list(s.tracks)[track_index]
        try:
            sends = list(t.mixer_sends)
            if send_index < len(sends):
                sends[send_index].value = max(0.0, min(1.0, float(value)))
                return {"ok": True}
        except Exception:
            pass
        return {"error": "Invalid send index"}

    def _set_track_property(self, track_index, prop, value):
        s = self._song
        t = list(s.tracks)[track_index]
        if prop == "arm":
            if getattr(t, "is_group_track", False) or not hasattr(t, "arm"):
                return {"error": "Track is not armable"}
        if prop == "mute":
            t.mute = bool(value)
        elif prop == "solo":
            t.solo = bool(value)
        elif prop == "arm":
            t.arm = bool(value)
        return {"ok": True}

    def _delete_device(self, track_index, device_index):
        s = self._song
        t = list(s.tracks)[track_index]
        if getattr(t, "is_frozen", False):
            return {"error": "Cannot delete from frozen track"}
        devs = list(t.devices)
        if device_index >= len(devs):
            return {"error": "Invalid device index"}
        devs[device_index].delete()
        return {"ok": True}

    def _delete_track(self, track_index):
        s = self._song
        if track_index < 0 or track_index >= len(s.tracks):
            return {"error": "Track index out of range (0-%d)" % (len(s.tracks) - 1)}
        if len(s.tracks) <= 1:
            return {"error": "Cannot delete last track"}
        t = list(s.tracks)[track_index]
        if getattr(t, "is_group_track", False) or getattr(t, "is_foldable", False):
            return {"error": "Cannot delete group track directly"}
        if getattr(t, "is_frozen", False):
            return {"error": "Cannot delete frozen track"}
        if s.is_playing:
            s.stop_playing()
            time.sleep(0.1)
        s.delete_track(track_index)
        return {"ok": True}

    def _duplicate_track(self, track_index):
        s = self._song
        if track_index < 0 or track_index >= len(s.tracks):
            return {"error": "Track index out of range (0-%d)" % (len(s.tracks) - 1)}
        s.duplicate_track(track_index)
        return {"ok": True}

    def _move_track(self, track_index, target_index):
        s = self._song
        if track_index < 0 or track_index >= len(s.tracks):
            return {"error": "Track index out of range (0-%d)" % (len(s.tracks) - 1)}
        if target_index < 0 or target_index >= len(s.tracks):
            return {"error": "Target index out of range (0-%d)" % (len(s.tracks) - 1)}
        s.move_track(track_index, target_index)
        return {"ok": True}

    def _undo(self):
        self._song.undo()
        return {"ok": True}

    def _redo(self):
        self._song.redo()
        return {"ok": True}

    def _delete_arrangement_clip(self, track_index, clip_index):
        s = self._song
        t = list(s.tracks)[track_index]
        clips = list(t.arrangement_clips)
        if clip_index >= len(clips):
            return {"error": "No arrangement clip at index {}".format(clip_index)}
        clip = clips[clip_index]
        try:
            t.delete_clip(clip)
        except Exception as exc:
            return {"error": "no se pudo borrar el clip: {}".format(exc)}
        return {"ok": True}

    def _save_song(self):
        s = self._song
        if not getattr(s, "is_mutable", True):
            return {"ok": False, "error": "set no modificable"}
        try:
            s.save_song()
        except Exception:
            s.save()
        return {"ok": True, "saved": str(getattr(s, "name", ""))}

    def _set_metronome(self, enabled):
        self._song.metronome = bool(enabled)
        return {"ok": True, "metronome": enabled}

    def _remove_notes_from_clip(self, track_index, clip_index, view="session"):
        s = self._song
        t = list(s.tracks)[track_index]
        if view == "arrangement":
            # Arrangement: borrar y recrear no es trivial; usar via moderna
            clips = list(t.arrangement_clips)
            clip = clips[clip_index] if clip_index < len(clips) else None
            if not clip:
                return {"error": "No clip found"}
            try:
                clip.remove_notes_extended(0, 0, clip.length + 1, 128)
            except Exception as exc:
                return {"error": "no se pudieron borrar las notas: {}".format(exc)}
            return {"ok": True}
        # Session: borrar el clip y recrearlo con la misma longitud evita por
        # completo los APIs legacy que disparan el aviso MPE de Live 12.
        cs = t.clip_slots[clip_index]
        clip = cs.clip if cs.has_clip else None
        if not clip:
            return {"ok": True, "note_count": 0}
        length = clip.length
        try:
            cs.delete_clip()
            cs.create_clip(length)
        except Exception as exc:
            return {"error": "no se pudieron borrar las notas: {}".format(exc)}
        return {"ok": True}

    def _set_view(self, view_name):
        s = self._song
        app = self._application()
        view = app.view if (app and hasattr(app, "view")) else None
        if not view:
            return {"error": "Cannot access view"}
        mapping = {
            "session": "session",
            "arrangement": "arrangement",
            "detail": "detail",
            "detail_clip": "detail_clip",
            "detail_device_chain": "detail_device_chain",
        }
        target = mapping.get(view_name, view_name)
        try:
            if hasattr(view, "show_{}".format(target)):
                getattr(view, "show_{}".format(target))()
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def _load_audio_clip(self, track_index, clip_index, file_path, view="session", start_time=0.0):
        s = self._song
        t = list(s.tracks)[track_index]

        if view == "arrangement":
            clips = list(t.arrangement_clips)
            clip = clips[clip_index] if clip_index < len(clips) else None
            if not clip:
                t.create_midi_clip(start_time, start_time + 4.0)
                clips = list(t.arrangement_clips)
                clip = clips[-1] if clips else None
        else:
            cs = t.clip_slots[clip_index]
            clip = cs.clip if cs.has_clip else None
            if not clip:
                clip = cs.create_clip(4.0)

        if not clip:
            return {"error": "Could not create clip"}

        try:
            clip.load(file_path, 0)
            self._disable_warp_and_align(clip)
            return {"ok": True, "file_path": file_path}
        except Exception as e:
            return {"error": str(e)}

    def _disable_warp_and_align(self, clip):
        try:
            clip.warping = False
            if hasattr(clip, "file_path") and clip.file_path:
                clip.loop_start = 0.0
                clip.loop_end = clip.length
                clip.start_marker = 0.0
                clip.end_marker = clip.length
        except Exception:
            pass

    def _set_clip_warping(self, track_index, clip_index, warping, view="session"):
        s = self._song
        t = list(s.tracks)[track_index]
        if view == "arrangement":
            clips = list(t.arrangement_clips)
            clip = clips[clip_index] if clip_index < len(clips) else None
        else:
            cs = t.clip_slots[clip_index]
            clip = cs.clip if cs.has_clip else None
        if clip:
            clip.warping = bool(warping)
            return {"ok": True}
        return {"error": "No clip found"}

    def _set_clip_automation(self, track_index, clip_index, view="session",
                             parameter="volume", device_index=None,
                             points=None, replace=False):
        s = self._song
        t = list(s.tracks)[track_index]
        if view == "arrangement":
            clips = list(t.arrangement_clips)
        else:
            clips = [cs.clip for cs in t.clip_slots if cs.has_clip]
        if clip_index >= len(clips):
            return {"error": "No clip found"}
        clip = clips[clip_index]

        param = self._resolve_parameter(t, parameter, device_index)
        if not param:
            return {"error": "Parameter not found: {}".format(parameter)}

        try:
            envelope = clip.find_box_envelope(param)
            if envelope and not replace:
                return {"confirmation_required": True, "message": "Envelope exists. Set replace=true to overwrite."}
            if not envelope:
                envelope = clip.create_automation_envelope(param)
            envelope.clear()
            if points:
                for pt in points:
                    time_beats = pt.get("time_beats", pt.get("time", 0.0))
                    value = pt.get("value", 0.0)
                    envelope.insert_step(time_beats, 0.0, value)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def _create_locators(self, locators=None):
        if not locators:
            return {"error": "No locators specified"}
        s = self._song
        results = []
        for loc in locators[:24]:
            name = loc.get("name", "")
            time_beats = loc.get("time", 0.0)
            try:
                s.set_current_song_time(time_beats)
                s.set_or_delete_cue()
                results.append({"name": name, "time": time_beats, "ok": True})
            except Exception as e:
                results.append({"name": name, "error": str(e)})
        return {"results": results}


# ---------------------------------------------------------------------------
# Entry point â€” Ableton Live calls this to instantiate the script
# ---------------------------------------------------------------------------
def create_instance(c_instance):
    return F(c_instance)
