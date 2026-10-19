#!/usr/bin/env python3
"""
Bridge MCP (Model Context Protocol) entre opencode y Ableton Live.

Habla con AbletonOSC (remote script instalado en Live) vía OSC:
  - Envía comandos al puerto 11000
  - Recibe respuestas en el puerto 11001
El device M4L bridge/device_helper.amxd escucha en el puerto 9001 y se usa
para INSERTAR devices nativos en una pista (AbletonOSC no puede hacerlo).

Implementa el protocolo MCP por stdio (JSON-RPC 2.0, framing Content-Length)
sin dependencias externas — solo requiere `python-osc`.

Dependencias:  pip install python-osc
Ejecución (la lanza la app Electron automáticamente):
  python bridge/mcp_server.py
"""

import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
import difflib

from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder

# VocalAssistant MIDI Remote Script TCP bridge (primary backend)
try:
    import midi_remote_tcp as midi_remote
    MIDI_REMOTE_AVAILABLE = True
except ImportError:
    MIDI_REMOTE_AVAILABLE = False

OSC_SEND_HOST = "127.0.0.1"
OSC_SEND_PORT = int(os.environ.get("ABLETON_OSC_PORT", 11000))
OSC_RECV_PORT = 11001
M4L_HELPER_PORT = 9001
M4L_REPLY_PORT = 9002
TEMPO_SERVER_PORT = 9003

SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_state.json")
GLOBAL_MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "global_memory.json")
ACTIVE_INSTANCE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_instance.json")

# Cache for list_song results (reduces token usage on repeated calls)
_song_cache = {"data": None, "params_key": None, "timestamp": 0}
CACHE_TTL_SECONDS = 10  # Cache valid for 10 seconds
_cache_lock = threading.Lock()

# insert_device (Live 12.3+) exige el nombre TAL COMO aparece en la UI de Live,
# no el class_name ("EQ Eight", no "Eq8"). El agente usa class_name; aquí se
# mapea. Lo que no esté en la tabla se pasa tal cual (best effort).
DEVICE_UI_NAMES = {
    "Eq8": "EQ Eight",
    "EQ3": "EQ Three",
    "ChannelEq": "Channel EQ",
    "GlueCompressor": "Glue Compressor",
    "AutoFilter": "Auto Filter",
    "AutoPan": "Auto Pan",
    "SimpleDelay": "Simple Delay",
    "PingPongDelay": "Ping Pong Delay",
    "FilterDelay": "Filter Delay",
    "GrainDelay": "Grain Delay",
    "MultiBandDynamics": "Multiband Dynamics",
    "DynamicTube": "Dynamic Tube",
    "BeatRepeat": "Beat Repeat",
    "FrequencyShifter": "Frequency Shifter",
    "SpectralResonator": "Spectral Resonator",
    "SurroundLimiter": "Surround Limiter",
    "DrumRack": "Drum Rack",
    "InstrumentRack": "Instrument Rack",
    "AudioEffectRack": "Audio Effect Rack",
    "HybridReverb": "Hybrid Reverb",
}


# ---------------------------------------------------------------------------
# Cliente OSC síncrono request/response contra AbletonOSC
# ---------------------------------------------------------------------------


class AbletonClient:
    def __init__(self, send_port=OSC_SEND_PORT, recv_port=OSC_RECV_PORT):
        self.send_port = send_port
        self.lock = threading.Lock()
        self.recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.recv.bind(("127.0.0.1", recv_port))
        self.recv.settimeout(0.5)
        self.send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _build(self, address, args):
        b = OscMessageBuilder(address)
        for a in args:
            b.add_arg(a)  # python-osc deduce el tipo (i/f/s) automáticamente
        return b.build().dgram

    def send_cmd(self, address, *args):
        """Fire-and-forget (para setter y métodos). No espera respuesta ni
        lee del socket de recepción, así que NO necesita el lock: los
        comandos se envían sin esperar turno (paralelismo real)."""
        self.send.sendto(self._build(address, list(args)), (OSC_SEND_HOST, self.send_port))

    def query(self, address, *args, timeout=3.0):
        """Envía y espera la respuesta de AbletonOSC con la misma dirección."""
        with self.lock:
            self.send.sendto(self._build(address, list(args)), (OSC_SEND_HOST, self.send_port))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, _ = self.recv.recvfrom(65535)
                except socket.timeout:
                    continue
                msg = OscMessage(data)
                if msg.address == address:
                    return list(msg.params)
        raise ConnectionError(
            f"No response from AbletonOSC for {address}. "
            "¿Está Live abierto con AbletonOSC activo en Preferencias > Link/Tempo/MIDI?"
        )

    def close(self):
        self.recv.close()
        self.send.close()


ableton = AbletonClient()

# ---------------------------------------------------------------------------
# Helper M4L (device_helper.amxd): canal de respuestas en el 9002.
# El helper responde /ok <op> o /error <motivo> a cada mensaje.
# ---------------------------------------------------------------------------

helper_reply = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
helper_reply.bind(("127.0.0.1", M4L_REPLY_PORT))
helper_reply.settimeout(0.3)

# El helper M4L responde al puerto fijo 9002, así que las llamadas
# concurrentes deben serializarse para no leer respuestas ajenas.
_helper_lock = threading.Lock()


def _send_helper(args, timeout=4.0):
    """Envía un mensaje al helper M4L (9001) y espera su respuesta (9002).

    Devuelve (ok, texto): ok=False también cuando no llega respuesta
    (helper no cargado en Live).
    """
    with _helper_lock:
        b = OscMessageBuilder(args[0])
        for a in args[1:]:
            b.add_arg(a)
        ableton.send.sendto(b.build().dgram, (OSC_SEND_HOST, M4L_HELPER_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = helper_reply.recvfrom(65535)
            except socket.timeout:
                continue
            text = data.decode("utf-8", errors="replace").strip()
            if text.startswith("/error"):
                return False, text[len("/error"):].strip() or "error del helper M4L"
            if text.startswith("/ok"):
                return True, text[len("/ok"):].strip()
    return False, (
        "sin respuesta del helper M4L (¿device_helper.amxd cargado en alguna "
        "pista de Live? Escucha UDP en el puerto 9001.)"
    )


def _tempo_server():
    """Sirve el tempo actual a audio_server.py (UDP 9003) para cuantizar MIDI."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", TEMPO_SERVER_PORT))
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            try:
                bpm = None
                if _midi_remote_ok():
                    ok, tresp = _try_midi_remote("get_transport_state", timeout=2.0)
                    if ok and tresp:
                        bpm = tresp.get("tempo")
                if bpm is None:
                    bpm = float(ableton.query("/live/song/get/tempo", timeout=2.0)[0])
                sock.sendto(str(bpm).encode(), addr)
            except Exception:
                sock.sendto(b"", addr)
        except OSError:
            return


# ---------------------------------------------------------------------------
# Memoria de sesión (ajuste iterativo: "suaviza el compresor")
# ---------------------------------------------------------------------------


def _load_session():
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {"log": [], "notes": []}
    state.setdefault("preferences", {})
    state.setdefault("notes", [])
    state.setdefault("log", [])
    return state


def _save_session(state):
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Memoria global (persiste entre TODOS los chats)
# ---------------------------------------------------------------------------


def _load_global_memory():
    try:
        with open(GLOBAL_MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"preferences": {}, "user_profile": {}, "project_defaults": {}}


def _save_global_memory(data):
    try:
        with open(GLOBAL_MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _session_log(action, detail):
    state = _load_session()
    state["log"].append({
        "time": time.strftime("%H:%M:%S"),
        "action": action,
        "detail": detail,
    })
    state["log"] = state["log"][-100:]
    _save_session(state)


# ---------------------------------------------------------------------------
# Utilidades de los tools
# ---------------------------------------------------------------------------


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _json_error(exc, context=""):
    return json.dumps({"error": f"{context}{exc}"})


def _param_ranges(track, device):
    mins = ableton.query("/live/device/get/parameters/min", track, device)[2:]
    maxs = ableton.query("/live/device/get/parameters/max", track, device)[2:]
    quant = ableton.query("/live/device/get/parameters/is_quantized", track, device)[2:]
    return mins, maxs, quant


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def ping(params):
    # Try MIDI Remote Script first
    ok, result = _try_midi_remote("ping")
    if ok:
        return json.dumps(result)
    # Fallback to AbletonOSC
    try:
        result = ableton.query("/live/test", timeout=1.5)
        return json.dumps({"connected": True, "reply": result, "backend": "abletonosc"})
    except Exception as exc:
        return json.dumps({"connected": False, "error": str(exc)})


def list_instances(params):
    """Discover every Ableton instance running the VocalAssistant MIDI
    Remote Script. Each Live copy binds a different TCP port (9878, 9879...).
    Returns the active instance and all detected ones."""
    if not MIDI_REMOTE_AVAILABLE:
        return json.dumps({"ok": False, "error": "MIDI Remote TCP no disponible"})
    try:
        instances = midi_remote.list_instances()
        active = midi_remote.get_active_port()
        return json.dumps({
            "ok": True,
            "active_port": active,
            "instances": instances,
            "count": len(instances),
        })
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def select_instance(params):
    """Switch the active Ableton instance by port. Ports come from
    list_instances (9878, 9879, ...). Persists the selection."""
    global _midi_remote_detected
    if not MIDI_REMOTE_AVAILABLE:
        return json.dumps({"ok": False, "error": "MIDI Remote TCP no disponible"})
    port = params.get("port")
    if port is None:
        return json.dumps({"ok": False, "error": "Falta el parámetro port"})
    try:
        port = int(port)
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "port debe ser un número"})
    result = midi_remote.set_active_port(port)
    if result.get("ok"):
        _save_active_instance(port, manual=True)
        _midi_remote_detected = True
    else:
        _midi_remote_detected = False
    return json.dumps(result)


def list_song(params):
    """Lista la sesión con opciones de optimización de tokens.
    track: (opcional) filtra por pista específica
    compact: (opcional, default false) solo índices y nombres, sin valores/min/max
    skip_values: (opcional, default false) omite valores actuales pero mantiene rangos"""
    # Cache check
    params_key = json.dumps(params or {}, sort_keys=True)
    with _cache_lock:
        now = time.time()
        if _song_cache["params_key"] == params_key and (now - _song_cache["timestamp"]) < CACHE_TTL_SECONDS:
            return _song_cache["data"]

    # Try MIDI Remote Script first (single TCP call vs many OSC round-trips)
    if _midi_remote_ok():
        try:
            ok, session = _try_midi_remote("get_session_info", timeout=10)
            if ok and session.get("tracks") is not None:
                track_filter = int(params.get("track", -1)) if params else -1
                compact = bool(params.get("compact", False)) if params else False
                all_tracks = session["tracks"]
                if track_filter >= 0:
                    all_tracks = [t for t in all_tracks if t.get("index") == track_filter]
                tracks = []
                for t in all_tracks:
                    entry = {"index": t.get("index", 0), "name": t.get("name", "")}
                    if not compact:
                        entry["is_midi"] = t.get("is_midi", False)
                        entry["is_audio"] = t.get("is_audio", False)
                    tracks.append(entry)
                result = json.dumps({"tracks": tracks, "backend": "midi_remote"})
                with _cache_lock:
                    _song_cache["data"] = result
                    _song_cache["params_key"] = params_key
                    _song_cache["timestamp"] = time.time()
                return result
        except Exception:
            pass

    try:
        n = int(ableton.query("/live/song/get/num_tracks")[0])
        names = ableton.query("/live/song/get/track_names")
    except Exception as exc:
        return _json_error(exc, "No se pudo listar la sesión: ")
    
    track_filter = int(params.get("track", -1)) if params else -1
    compact = bool(params.get("compact", False)) if params else False
    skip_values = bool(params.get("skip_values", False)) if params else False
    depth = int(params.get("depth", 0)) if params else 0
    
    track_indices = [track_filter] if track_filter >= 0 else range(n)
    tracks = []
    for t in track_indices:
        if t >= n:
            continue
        name = names[t] if t < len(names) else f"Track {t}"
        if depth >= 1:
            track_data = _list_song_track_deep(t, name, depth)
            if compact:
                track_data = _compact_track(track_data)
            tracks.append(track_data)
            continue
        try:
            num_dev = int(ableton.query("/live/track/get/num_devices", t)[1])
        except Exception:
            num_dev = 0
        devices = []
        for d in range(num_dev):
            try:
                dev = {"index": d, "name": "", "class_name": "", "parameters": []}
                try:
                    dev["name"] = ableton.query("/live/device/get/name", t, d)[2]
                except Exception:
                    pass
                try:
                    dev["class_name"] = ableton.query("/live/device/get/class_name", t, d)[2]
                except Exception:
                    pass
                if not compact:
                    try:
                        pnames = ableton.query("/live/device/get/parameters/name", t, d)[2:]
                        if not skip_values:
                            pvalues = ableton.query("/live/device/get/parameters/value", t, d)[2:]
                        pmins = ableton.query("/live/device/get/parameters/min", t, d)[2:]
                        pmaxs = ableton.query("/live/device/get/parameters/max", t, d)[2:]
                        pquant = ableton.query("/live/device/get/parameters/is_quantized", t, d)[2:]
                        for i, pname in enumerate(pnames):
                            param = {
                                "index": i,
                                "name": pname,
                                "min": pmins[i] if i < len(pmins) else 0.0,
                                "max": pmaxs[i] if i < len(pmaxs) else 1.0,
                            }
                            if not skip_values:
                                param["value"] = pvalues[i] if i < len(pvalues) else None
                            if i < len(pquant) and pquant[i]:
                                param["is_quantized"] = True
                            dev["parameters"].append(param)
                    except Exception:
                        pass
                elif not skip_values:
                    try:
                        pnames = ableton.query("/live/device/get/parameters/name", t, d)[2:]
                        pvalues = ableton.query("/live/device/get/parameters/value", t, d)[2:]
                        for i, pname in enumerate(pnames):
                            dev["parameters"].append({
                                "index": i,
                                "name": pname,
                                "value": pvalues[i] if i < len(pvalues) else None,
                            })
                    except Exception:
                        pass
                devices.append(dev)
            except Exception:
                continue
        tracks.append({"index": t, "name": name, "devices": devices})
    result = json.dumps({"tracks": tracks})
    
    # Store in cache
    with _cache_lock:
        _song_cache["data"] = result
        _song_cache["params_key"] = params_key
        _song_cache["timestamp"] = time.time()
    
    return result


def invalidate_song_cache():
    """Invalidate song cache (call after structural changes)."""
    with _cache_lock:
        _song_cache["data"] = None
        _song_cache["params_key"] = None
        _song_cache["timestamp"] = 0


# ---------------------------------------------------------------------------
# MIDI Remote Script routing layer
# ---------------------------------------------------------------------------

_midi_remote_detected = False


def _load_active_instance():
    """Load the persisted active instance port (default: first MIDI port)."""
    try:
        with open(ACTIVE_INSTANCE_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f).get("port", midi_remote.DEFAULT_PORT))
    except Exception:
        return midi_remote.DEFAULT_PORT


def _load_active_instance_info():
    """Devuelve {"port": int, "manual": bool} de la persistencia."""
    info = {"port": midi_remote.DEFAULT_PORT, "manual": False}
    try:
        with open(ACTIVE_INSTANCE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        info["port"] = int(data.get("port", midi_remote.DEFAULT_PORT))
        info["manual"] = bool(data.get("manual", False))
    except Exception:
        pass
    return info


def _save_active_instance(port, manual=False):
    try:
        with open(ACTIVE_INSTANCE_FILE, "w", encoding="utf-8") as f:
            json.dump({"port": int(port), "manual": bool(manual),
                       "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    except Exception:
        pass


def _apply_persisted_active_instance():
    """At bridge startup, restore the instance the user chose last time."""
    if not MIDI_REMOTE_AVAILABLE:
        return
    port = _load_active_instance()
    if port != midi_remote.DEFAULT_PORT:
        midi_remote.set_active_port(port)
        print("[bridge] Instancia activa restaurada: puerto {}".format(port), file=sys.stderr, flush=True)


def _detect_midi_remote():
    """Detecta el VocalAssistant MIDI Remote Script. NUNCA elige
    automaticamente una instancia protegida (set 'definitivo'), salvo que
    el usuario la haya seleccionado manualmente antes."""
    global _midi_remote_detected
    if not MIDI_REMOTE_AVAILABLE:
        return False
    try:
        # 1) Seleccion persistida del usuario
        info = _load_active_instance_info()
        ping = midi_remote._ping_port(info["port"], timeout=1.5)
        if ping.get("ok"):
            song = midi_remote._instance_song_name(info["port"])
            if info.get("manual") or not midi_remote._is_protected_song(song):
                midi_remote.set_active_port(info["port"])
                _midi_remote_detected = True
                print("[bridge] VocalAssistant MIDI Remote Script detected (TCP {})".format(
                    midi_remote.get_active_port()), file=sys.stderr, flush=True)
                return True
            print("[bridge] Puerto {} tiene el set '{}' (PROTEGIDO); se busca otra instancia".format(
                info["port"], song), file=sys.stderr, flush=True)
        # 2) Escaneo automatico: primera instancia NO protegida
        for inst in midi_remote.list_instances():
            if inst.get("protected"):
                print("[bridge] Set '{}' en puerto {} esta PROTEGIDO; se omite".format(
                    inst.get("song_name", "?"), inst["port"]), file=sys.stderr, flush=True)
                continue
            midi_remote.set_active_port(inst["port"])
            _save_active_instance(inst["port"], manual=False)
            _midi_remote_detected = True
            print("[bridge] VocalAssistant MIDI Remote Script detected (TCP {})".format(
                inst["port"]), file=sys.stderr, flush=True)
            return True
        _midi_remote_detected = False
        return False
    except Exception:
        _midi_remote_detected = False
        return False


def _midi_remote_ok():
    """Return True if MIDI Remote Script is available for routing.
    Lazily re-detects in case Ableton loaded the script after we started."""
    if not MIDI_REMOTE_AVAILABLE:
        return False
    if not _midi_remote_detected:
        _detect_midi_remote()
    return _midi_remote_detected


def _try_midi_remote(command_type, params=None, timeout=None):
    """Try sending a command to the MIDI Remote Script.
    Returns (success, result_dict). success=False means fallback to OSC."""
    if not _midi_remote_ok():
        return False, None
    try:
        result = midi_remote.send(command_type, params, timeout)
        if result.get("status") == "success":
            return True, result.get("result", {})
        # If MIDI Remote Script returns an error that suggests it doesn't
        # support this command, fall back to OSC
        msg = result.get("message", "")
        if "Unknown command" in msg:
            return False, None
        # Otros errores son reales: ok=False con detalle para no fingir éxito
        return False, {"error": msg or "error del script F"}
    except Exception:
        return False, None


def _compact_track(track_data):
    """Compacta datos de pista: solo índices y nombres para navegación."""
    out = {"index": track_data.get("index", track_data.get("i")), "name": track_data.get("name", track_data.get("n"))}
    if "devices" in track_data:
        out["devices"] = [{"index": d.get("index", d.get("i")), "name": d.get("name", d.get("n")), "class_name": d.get("class_name", d.get("c", ""))} for d in track_data["devices"]]
    if "chains" in track_data:
        out["chains"] = [{"name": c.get("name", ""), "devices": [{"index": d.get("index"), "name": d.get("name", ""), "class_name": d.get("class_name", "")} for d in c.get("devices", [])]} for c in track_data["chains"]]
    if "eq8" in track_data:
        out["eq8"] = track_data["eq8"]
    return out


def _compact_eq8(device):
    """Devuelve un resumen compacto de las bandas de un Eq8 (freq/gain/q/on)
    o None si el device no es un Eq8. Los nombres de parámetros del Eq8 suelen
    ser "N <etiqueta>" (p.ej. "1 Frequency", "1 Gain", "1 Q", "1 On")."""
    if device.get("class_name") != "Eq8":
        return None
    bands = {}
    for p in device.get("parameters", []):
        pname = str(p.get("name", ""))
        m = re.match(r"^\s*(\d+)\s+(.+)$", pname)
        if not m:
            continue
        idx = int(m.group(1))
        label = m.group(2).strip().lower()
        if idx not in bands:
            bands[idx] = {"band": idx}
        if "freq" in label and "freq" not in bands[idx]:
            bands[idx]["freq"] = p.get("value")
        elif "gain" in label and "gain" not in bands[idx]:
            bands[idx]["gain"] = p.get("value")
        elif label == "q" and "q" not in bands[idx]:
            bands[idx]["q"] = p.get("value")
        elif label in ("on", "enabled") and "on" not in bands[idx]:
            bands[idx]["on"] = p.get("value")
    out = [b for _, b in sorted(bands.items())]
    return out or None


def _list_song_track_deep(t, name, depth):
    """Devuelve el árbol anidado de una pista vía /live/inspect/track (sin
    play) y añade el resumen compacto de los Eq8 de la pista."""
    try:
        reply = ableton.query("/live/inspect/track", t, depth, 1, timeout=15.0)
        payload = reply[-1]
        data = json.loads(payload)
    except Exception as exc:
        return {"index": t, "name": name, "error": f"inspect: {exc}"}
    eq8 = []
    for dev in data.get("devices", []):
        bands = _compact_eq8(dev)
        if bands:
            eq8.append({"device_index": dev.get("index"),
                        "name": dev.get("name", ""),
                        "bands": bands})
        for chain in dev.get("chains", []):
            for inner in chain.get("devices", []):
                ibands = _compact_eq8(inner)
                if ibands:
                    eq8.append({"device_index": inner.get("index"),
                                "chain": chain.get("name", ""),
                                "name": inner.get("name", ""),
                                "bands": ibands})
    data["index"] = t
    data["name"] = name
    if eq8:
        data["eq8"] = eq8
    return data


def get_device_parameters(params):
    """Parámetros de UN device: nombres, valores, min, max, on/off."""
    t, d = params["track_index"], params["device_index"]

    # Try MIDI Remote Script first
    if _midi_remote_ok():
        ok, result = _try_midi_remote("get_device_parameters", {
            "track_index": t, "device_index": d})
        if ok and isinstance(result, dict) and "parameters" in result:
            return json.dumps({
                "track": t,
                "device": d,
                "parameters": result["parameters"],
                "device_name": result.get("device_name", ""),
                "backend": "midi_remote",
            })

    # Fallback to AbletonOSC
    try:
        pnames = ableton.query("/live/device/get/parameters/name", t, d)[2:]
        pvalues = ableton.query("/live/device/get/parameters/value", t, d)[2:]
        pmins = ableton.query("/live/device/get/parameters/min", t, d)[2:]
        pmaxs = ableton.query("/live/device/get/parameters/max", t, d)[2:]
        pquant = ableton.query("/live/device/get/parameters/is_quantized", t, d)[2:]
    except Exception as exc:
        return _json_error(exc)
    return json.dumps({
        "track": t,
        "device": d,
        "parameters": [
            {
                "index": i,
                "name": pnames[i],
                "value": pvalues[i] if i < len(pvalues) else None,
                "min": pmins[i] if i < len(pmins) else 0.0,
                "max": pmaxs[i] if i < len(pmaxs) else 1.0,
                "is_quantized": bool(pquant[i]) if i < len(pquant) else False,
            }
            for i in range(len(pnames))
        ],
        "backend": "abletonosc",
    })


def set_parameter(params):
    """Mueve una perilla a un valor NORMALIZADO 0–1."""
    t, d, p = params["track_index"], params["device_index"], params["parameter_index"]
    v01 = _clamp(float(params.get("value01", 0.5)), 0.0, 1.0)

    # Try MIDI Remote Script first (needs parameter name, not index)
    if _midi_remote_ok():
        # First get parameter names to find the right one
        ok, result = _try_midi_remote("get_device_parameters", {
            "track_index": t, "device_index": d})
        if ok and isinstance(result, dict):
            parameters = result.get("parameters", [])
            if p < len(parameters):
                param_name = parameters[p].get("name", "")
                if param_name:
                    ok2, result2 = _try_midi_remote("set_device_parameter", {
                        "track_index": t, "device_index": d,
                        "parameter_name": param_name, "value": v01})
                    if ok2:
                        try:
                            dev_name = result.get("device_name", f"device{d}")
                            pname = param_name
                        except Exception:
                            dev_name, pname = f"device{d}", f"param{p}"
                        _session_log("set_parameter",
                                     f"pista {t} {dev_name} ({pname}) -> {round(v01, 3)} (0–1)")
                        invalidate_song_cache()
                        return json.dumps({
                            "ok": True, "track": t, "device": d,
                            "device_name": dev_name, "parameter": p,
                            "parameter_name": pname, "value_normalized": v01,
                            "backend": "midi_remote",
                        })

    # Fallback to AbletonOSC
    try:
        mins, maxs, quant = _param_ranges(t, d)
    except Exception as exc:
        return _json_error(exc)
    lo = mins[p] if p < len(mins) else 0.0
    hi = maxs[p] if p < len(maxs) else 1.0
    value = lo + v01 * (hi - lo)
    if p < len(quant) and quant[p]:
        value = round(value)
    ableton.send_cmd("/live/device/set/parameter/value", t, d, p, value)
    try:
        dev_name = ableton.query("/live/device/get/name", t, d, timeout=1.0)[2]
        pname = ableton.query("/live/device/get/parameters/name", t, d, timeout=1.0)[2:][p]
    except Exception:
        dev_name, pname = f"device{d}", f"param{p}"
    _session_log("set_parameter",
                 f"pista {t} {dev_name} ({pname}) -> {round(v01, 3)} (0–1)")
    invalidate_song_cache()
    return json.dumps({
        "ok": True,
        "track": t,
        "device": d,
        "device_name": dev_name,
        "parameter": p,
        "parameter_name": pname,
        "value_normalized": v01,
        "value_sent": value,
        "backend": "abletonosc",
    })


def set_track(params):
    """Ajusta propiedades de pista:
    volume|panning (0–1), mute|solo|arm|monitoring (0/1),
    name (string, renombra), color_index (0–69),
    send (usa send_index + value), input_routing_type (ej "Ext. In"),
    input_routing_channel (ej "1/2")."""
    t = params["track_index"]
    prop = params.get("property_name")
    value = params.get("value")

    # Try MIDI Remote Script for supported properties
    if _midi_remote_ok():
        if prop == "name":
            ok, result = _try_midi_remote("set_track_name", {"track_index": t, "name": str(value)})
            if ok:
                _session_log("set_track", f"pista {t} name -> {value}")
                invalidate_song_cache()
                return json.dumps({"ok": True, "track": t, "property": prop, "value": value, "backend": "midi_remote"})
        elif prop in ("volume", "panning"):
            mixer_params = {"track_index": t}
            if prop == "volume":
                mixer_params["volume"] = float(value)
            else:
                mixer_params["pan"] = float(value)
            ok, result = _try_midi_remote("set_track_mixer", mixer_params)
            if ok:
                _session_log("set_track", f"pista {t} {prop} -> {value}")
                invalidate_song_cache()
                return json.dumps({"ok": True, "track": t, "property": prop, "value": value, "backend": "midi_remote"})
        elif prop == "mute":
            ok, result = _try_midi_remote("set_track_mute", {"track_index": t, "mute": bool(value)})
            if ok:
                _session_log("set_track", f"pista {t} mute -> {value}")
                return json.dumps({"ok": True, "track": t, "property": prop, "value": value, "backend": "midi_remote"})
        elif prop == "solo":
            ok, result = _try_midi_remote("set_track_solo", {"track_index": t, "solo": bool(value)})
            if ok:
                _session_log("set_track", f"pista {t} solo -> {value}")
                return json.dumps({"ok": True, "track": t, "property": prop, "value": value, "backend": "midi_remote"})
        elif prop == "arm":
            ok, result = _try_midi_remote("set_track_arm", {"track_index": t, "arm": bool(value)})
            if ok:
                _session_log("set_track", f"pista {t} arm -> {value}")
                return json.dumps({"ok": True, "track": t, "property": prop, "value": value, "backend": "midi_remote"})
        elif prop == "send":
            send_idx = int(params.get("send_index", 0))
            ok, result = _try_midi_remote("set_track_send", {
                "track_index": t, "send_index": send_idx, "value": float(value)})
            if ok:
                _session_log("set_track", f"pista {t} send[{send_idx}] -> {value}")
                return json.dumps({"ok": True, "track": t, "property": prop, "value": value, "backend": "midi_remote"})

    # Fallback to AbletonOSC
    if prop == "name":
        ableton.send_cmd("/live/track/set/name", t, str(value))
    elif prop == "color_index":
        ableton.send_cmd("/live/track/set/color_index", t, int(value))
    elif prop == "monitoring":
        ableton.send_cmd("/live/track/set/current_monitoring_state", t, int(value))
    elif prop == "input_routing_type":
        ableton.send_cmd("/live/track/set/input_routing_type", t, str(value))
    elif prop == "input_routing_channel":
        ableton.send_cmd("/live/track/set/input_routing_channel", t, str(value))
    elif prop == "output_routing_type":
        ableton.send_cmd("/live/track/set/output_routing_type", t, str(value))
    elif prop == "output_routing_channel":
        ableton.send_cmd("/live/track/set/output_routing_channel", t, str(value))
    elif prop == "send":
        ableton.send_cmd("/live/track/set/send", t, int(params.get("send_index", 0)), float(value))
    elif prop in ("volume", "panning", "mute", "solo", "arm"):
        addresses = {
            "volume": "/live/track/set/volume",
            "panning": "/live/track/set/panning",
            "mute": "/live/track/set/mute",
            "solo": "/live/track/set/solo",
            "arm": "/live/track/set/arm",
        }
        ableton.send_cmd(addresses[prop], t, float(value))
    else:
        return json.dumps({
            "error": f"Propiedad inválida: {prop}. Usa {['volume','panning','mute','solo','arm','monitoring','name','color_index','send','input_routing_type','input_routing_channel','output_routing_type','output_routing_channel']}"
        })
    _session_log("set_track", f"pista {t} {prop} -> {value}")
    invalidate_song_cache()
    return json.dumps({"ok": True, "track": t, "property": prop, "value": value, "backend": "abletonosc"})


TRANSPORT = {
    "play": "/live/song/start_playing",
    "stop": "/live/song/stop_playing",
    "stop_all_clips": "/live/song/stop_all_clips",
    "record": "/live/song/trigger_session_record",
    "tap_tempo": "/live/song/tap_tempo",
    "undo": "/live/song/undo",
    "redo": "/live/song/redo",
}


def transport(params):
    """Control del transport de Ableton. action: play, stop, record, tap_tempo,
    undo, redo, stop_all_clips."""
    action = params.get("action")
    if action not in TRANSPORT:
        return json.dumps({"error": f"Acción inválida: {action}. Usa {list(TRANSPORT)}"})

    # Try MIDI Remote Script for supported actions
    if _midi_remote_ok():
        midi_action_map = {
            "play": ("start_playback", {}),
            "stop": ("stop_playback", {}),
            "undo": ("undo", {}),
            "redo": ("redo", {}),
        }
        if action in midi_action_map:
            cmd, cmd_params = midi_action_map[action]
            ok, result = _try_midi_remote(cmd, cmd_params)
            if ok:
                _session_log("transport", action)
                return json.dumps({"ok": True, "action": action, "backend": "midi_remote"})

    # Fallback to AbletonOSC
    ableton.send_cmd(TRANSPORT[action])
    _session_log("transport", action)
    return json.dumps({"ok": True, "action": action, "backend": "abletonosc"})


def set_tempo(params):
    """Cambia el tempo de la sesión en BPM (ej: 128.0)."""
    bpm = float(params["bpm"])
    if not 20 <= bpm <= 999:
        return json.dumps({"error": "Tempo fuera de rango"})
    if _midi_remote_ok():
        ok, result = _try_midi_remote("set_tempo", {"tempo": bpm})
        if ok:
            _session_log("set_tempo", f"{bpm} BPM")
            return json.dumps({"ok": True, "bpm": bpm, "backend": "midi_remote"})
    ableton.send_cmd("/live/song/set/tempo", bpm)
    _session_log("set_tempo", f"{bpm} BPM")
    return json.dumps({"ok": True, "bpm": bpm, "backend": "abletonosc"})


def set_metronome(params):
    """Enciende/apaga el metrónomo. on: 1 o 0."""
    enabled = bool(params["on"])
    if _midi_remote_ok():
        ok, result = _try_midi_remote("set_metronome", {"enabled": enabled})
        if ok:
            _session_log("set_metronome", f"{'on' if enabled else 'off'}")
            return json.dumps({"ok": True, "metronome": int(enabled), "backend": "midi_remote"})
    ableton.send_cmd("/live/song/set/metronome", int(enabled))
    _session_log("set_metronome", f"{'on' if enabled else 'off'}")
    return json.dumps({"ok": True, "metronome": int(enabled), "backend": "abletonosc"})


def get_scenes(params):
    """Lista las escenas de la sesión (nombres)."""
    if _midi_remote_ok():
        ok, result = _try_midi_remote("get_scenes")
        if ok and isinstance(result, dict) and "scenes" in result:
            return json.dumps({"scenes": result["scenes"], "backend": "midi_remote"})
    try:
        n = int(ableton.query("/live/song/get/num_scenes")[0])
    except Exception as exc:
        return _json_error(exc)
    scenes = []
    for i in range(n):
        try:
            scenes.append({"index": i, "name": ableton.query("/live/scene/get/name", i)[1]})
        except Exception:
            scenes.append({"index": i, "name": f"Escena {i + 1}"})
    return json.dumps({"scenes": scenes, "backend": "abletonosc"})


def fire_scene(params):
    """Lanza una escena (dispara los clips de su fila)."""
    scene_idx = params["scene_index"]
    if _midi_remote_ok():
        ok, result = _try_midi_remote("fire_scene", {"scene_index": scene_idx})
        if ok:
            _session_log("fire_scene", f"escena {scene_idx}")
            return json.dumps({"ok": True, "scene": scene_idx, "backend": "midi_remote"})
    ableton.send_cmd("/live/scene/fire", scene_idx)
    _session_log("fire_scene", f"escena {scene_idx}")
    return json.dumps({"ok": True, "scene": scene_idx, "backend": "abletonosc"})


def _create_track(midi, name):
    # Try MIDI Remote Script first
    if _midi_remote_ok():
        cmd = "create_midi_track" if midi else "create_audio_track"
        ok, result = _try_midi_remote(cmd, {"index": -1})
        if ok:
            # Rename the track
            track_idx = result.get("track_index", -1)
            if track_idx >= 0:
                _try_midi_remote("set_track_name", {"track_index": track_idx, "name": name})
            _session_log("create_track" if not midi else "create_midi_track",
                         f"pista {track_idx} \"{name}\"")
            invalidate_song_cache()
            return json.dumps({"ok": True, "track_index": track_idx, "name": name, "is_midi": midi, "backend": "midi_remote"})

    # Fallback to AbletonOSC
    cmd = "/live/song/create_midi_track" if midi else "/live/song/create_audio_track"
    ableton.send_cmd(cmd, -1)
    time.sleep(0.3)
    n = int(ableton.query("/live/song/get/num_tracks")[0])
    idx = n - 1
    ableton.send_cmd("/live/track/set/name", idx, name)
    _session_log("create_track" if not midi else "create_midi_track",
                 f"pista {idx} \"{name}\"")
    invalidate_song_cache()
    return json.dumps({"ok": True, "track_index": idx, "name": name, "is_midi": midi, "backend": "abletonosc"})


def create_track(params):
    """Crea una pista de AUDIO nueva con el nombre indicado."""
    return _create_track(False, params["name"])


def create_midi_track(params):
    """Crea una pista MIDI nueva con el nombre indicado."""
    return _create_track(True, params["name"])


def delete_track(params):
    """Borra la pista en el índice indicado. No borra pistas de grupo/frozen."""
    t = params["track_index"]
    if _midi_remote_ok():
        ok, result = _try_midi_remote("delete_track", {"track_index": t}, timeout=20.0)
        if ok:
            if isinstance(result, dict) and result.get("ok"):
                _session_log("delete_track", f"pista {t}")
                invalidate_song_cache()
                return json.dumps({"ok": True, "track_index": t, "backend": "midi_remote"})
            msg = result.get("error", result.get("message", "error"))
            return json.dumps({"error": str(msg)})
    try:
        ableton.send_cmd("/live/track/delete", t)
    except Exception as exc:
        return _json_error(exc, "No se pudo borrar la pista: ")
    _session_log("delete_track", f"pista {t}")
    invalidate_song_cache()
    return json.dumps({"ok": True, "track_index": t, "backend": "abletonosc"})


def insert_device(params):
    """Inserta un device NATIVO de Ableton en una pista.
    Primero intenta vía el browser del MIDI Remote Script (sin M4L).
    Si falla, usa el helper M4L device_helper.amxd como fallback."""
    t = params["track_index"]
    device_class = params["device_class"]
    ui_name = DEVICE_UI_NAMES.get(device_class, device_class)

    # Browser del MIDI Remote Script: escaneo (cacheado en el script) + match.
    # Cubre instrumentos Y efectos sin depender de rutas fijas ni del helper M4L.
    if _midi_remote_ok():
        ok, scan_result = _try_midi_remote("scan_browser_full", {
            "categories": ["instruments", "audio_effects", "midi_effects"],
            "max_depth": 5,
        }, timeout=60.0)
        if ok and isinstance(scan_result, dict):
            items = scan_result.get("items", [])
            best = None
            for it in items:
                nm = str(it.get("name", "")).lower()
                exact = nm == ui_name.lower() or nm == str(device_class).lower()
                if exact or _matches_query(nm, device_class):
                    if it.get("is_loadable"):
                        best = it
                        if exact:
                            break
                    if not best:
                        best = it
            if best:
                load_ok, load_result = _try_midi_remote("load_browser_item", {
                    "track_index": t,
                    "uri": best.get("uri", ""),
                    "item_path": best.get("path", ""),
                }, timeout=45.0)
                if load_ok and isinstance(load_result, dict) and load_result.get("ok"):
                    _session_log("insert_device",
                                 f"pista {t} + {device_class} -> {best.get('name')} [midi_remote]")
                    invalidate_song_cache()
                    return json.dumps({
                        "ok": True,
                        "track": t,
                        "device_class": device_class,
                        "loaded": best.get("name"),
                        "backend": "midi_remote",
                        "nota": "Verifica con list_song(track=N) que el device apareció.",
                    })

    # Fallback to M4L helper
    ok, reply = _send_helper(["/create_device", t, ui_name])
    if not ok:
        return json.dumps({"error": reply})
    _session_log("insert_device", f"pista {t} + {device_class} ({ui_name}) [m4l]")
    invalidate_song_cache()
    return json.dumps({
        "ok": True,
        "track": t,
        "device_class": device_class,
        "helper": reply,
        "backend": "m4l",
        "nota": "Verifica con list_song(track=N) que el device apareció (índice nuevo al final).",
    })


def _norm_text(s):
    """Normaliza texto para matching flexible: minúsculas, sin acentos, espacios colapsados."""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def _squash(s):
    """Colapsa letras repetidas: accordion->acordion, akkordeon->akordeon."""
    return re.sub(r"(.)\1+", r"\1", s)


def _matches_query(item_name, query):
    """Match flexible: contención directa, con letras colapsadas, por palabras
    clave, o similitud difusa (p.ej. 'Accordion' encuentra 'Akkordeon')."""
    n = _norm_text(item_name)
    q = _norm_text(query)
    if not q:
        return True
    if q in n or n in q:
        return True
    qs, ns = _squash(q), _squash(n)
    if qs in ns or ns in qs:
        return True
    q_words = [w for w in q.split() if len(w) > 2]
    if q_words and any(w in n for w in q_words):
        return True
    if len(qs) >= 5 and difflib.SequenceMatcher(None, qs, ns).ratio() >= 0.75:
        return True
    for w in ns.split():
        if len(w) >= 5 and difflib.SequenceMatcher(None, qs, w).ratio() >= 0.75:
            return True
    return False


def load_plugin(params):
    """Busca un plugin/preset en el browser de Live por nombre (VST/AU de
    terceros o nativos: 'Serum', 'Ozone', 'Valhalla', 'Keyscape'...) y lo
    inserta en la pista. Primero intenta vía el MIDI Remote Script browser,
    luego AbletonOSC BrowserHandler como fallback."""
    t = params["track_index"]
    query = str(params["query"]).strip()
    if not query:
        return json.dumps({"error": "query vacío: escribe el nombre del plugin"})

    # Try MIDI Remote Script browser scan + load first
    if _midi_remote_ok():
        # Scan browser to find matching items
        ok, scan_result = _try_midi_remote("scan_browser_full", {
            "categories": ["instruments", "sounds", "drums", "audio_effects", "midi_effects", "packs", "plugins"],
            "max_depth": 5,
        }, timeout=60.0)
        if ok and isinstance(scan_result, dict):
            items = scan_result.get("items", [])
            best_match = None
            for item in items:
                if _matches_query(item.get("name", ""), query):
                    if item.get("is_loadable"):
                        best_match = item
                        break
                    if not best_match:
                        best_match = item
            if best_match:
                load_ok, load_result = _try_midi_remote("load_browser_item", {
                    "track_index": t,
                    "uri": best_match.get("uri", ""),
                    "item_path": best_match.get("path", ""),
                }, timeout=30.0)
                if load_ok and isinstance(load_result, dict) and load_result.get("ok"):
                    loaded_name = best_match.get("name", query)
                    _session_log("load_plugin", f"pista {t} + '{query}' -> {loaded_name} [midi_remote]")
                    invalidate_song_cache()
                    return json.dumps({
                        "ok": True,
                        "track": t,
                        "query": query,
                        "loaded": loaded_name,
                        "backend": "midi_remote",
                        "nota": "Verifica con list_song(track=N) qué device se creó.",
                    })

    # Fallback to AbletonOSC BrowserHandler
    try:
        reply = ableton.query("/live/browser/load_by_query", t, query, timeout=30.0)
    except ConnectionError as err:
        return json.dumps({"error": str(err)})
    loaded = str(reply[2]) if len(reply) > 2 else ""
    if not loaded:
        return json.dumps({
            "error": f"No encontré nada que cargue con '{query}' en el browser "
                     f"(revisé sounds/instruments/drums/samples/packs/user_library/"
                     f"plugins/audio_effects/midi_effects). Verifica el nombre y "
                     f"que el plugin esté instalado.",
        })
    _session_log("load_plugin", f"pista {t} + '{query}' -> {loaded} [abletonosc]")
    invalidate_song_cache()
    return json.dumps({
        "ok": True,
        "track": t,
        "query": query,
        "loaded": loaded,
        "backend": "abletonosc",
        "nota": "Verifica con list_song(track=N) qué device se creó (y si el plugin expone parámetros).",
    })


def create_rack(params):
    """Crea un rack en la pista: kind = audio (Audio Effect Rack), instrument
    (Instrument Rack) o drum (Drum Rack). Después usa chain_create_device para
    meter devices dentro de la cadena del rack."""
    t = params["track_index"]
    kind = str(params.get("kind", "audio")).lower()
    rack_ui = {"audio": "Audio Effect Rack",
               "instrument": "Instrument Rack",
               "drum": "Drum Rack"}.get(kind)
    if not rack_ui:
        return json.dumps({"error": f"kind inválido: {kind}. Usa audio | instrument | drum"})
    ok, reply = _send_helper(["/create_rack", t, rack_ui])
    if not ok:
        return json.dumps({"error": reply})
    _session_log("create_rack", f"pista {t} rack {kind} ({rack_ui})")
    invalidate_song_cache()
    return json.dumps({
        "ok": True,
        "track": t,
        "kind": kind,
        "helper": reply,
        "nota": "El rack queda al final de los devices de la pista; usa list_song(track=N) para ver su índice.",
    })


def chain_create_device(params):
    """Inserta un device DENTRO de la cadena de un rack (rack de FX con
    cadena). rack_index es el índice del rack en la pista (ver list_song).

    El helper llega a la cadena navegando con live.object: track -> get
    devices -> zl nth -> rack -> get chains -> cadena. live.path no puede
    navegar dentro de `devices` (devuelve id 0), por eso se usa get.
    El índice de zl nth se calcula AQUÍ: el listado es `devices id a id b ...`;
    tras el [route devices] queda `id a id b ...` (cada device = par id+num), así
    que el número del device r está en la posición 1-based 2*r+2."""
    t = params["track_index"]
    rack = params["rack_index"]
    device_class = params["device_class"]
    ui_name = DEVICE_UI_NAMES.get(device_class, device_class)
    zl_idx = 2 * int(rack) + 2
    ok, reply = _send_helper(["/chain_create_device", t, zl_idx, ui_name])
    if not ok:
        return json.dumps({"error": reply})
    _session_log("chain_create_device",
                 f"pista {t} rack {rack} + {device_class} (en cadena)")
    invalidate_song_cache()
    return json.dumps({
        "ok": True,
        "track": t,
        "rack_index": rack,
        "device_class": device_class,
        "helper": reply,
        "nota": "El device va dentro de la cadena del rack (no lo verás con list_song; sí en Live).",
    })


def map_macro(params):
    """Mapea un parámetro de un device dentro de la cadena de un rack a una
    macro (perilla) del rack. Necesita el rack creado con create_rack y
    devices dentro con chain_create_device. Índices 0-based."""
    t = params["track_index"]
    rack = params["rack_index"]
    chain_dev = params["chain_device_index"]
    param = params["parameter_index"]
    macro = params["macro_index"]
    ok, reply = _send_helper(["/map_macro", t, rack, chain_dev, param, macro])
    if not ok:
        return json.dumps({"error": reply})
    _session_log("map_macro",
                 f"pista {t} rack {rack} device {chain_dev} param {param} -> macro {macro}")
    return json.dumps({
        "ok": True,
        "track": t,
        "rack_index": rack,
        "chain_device_index": chain_dev,
        "parameter_index": param,
        "macro_index": macro,
        "helper": reply,
    })


def session_summary(params):
    """Memoria de sesión compacta: devuelve resumen estructurado en vez de log completo."""
    state = _load_session()
    log = state.get("log", [])
    notes = state.get("notes", [])
    prefs = state.get("preferences", {})
    
# Compact log: group by action type, keep last 20 entries
    if len(log) > 20:
        summary = {"log": log[-20:], "total_actions": len(log), "note": f"Mostrando últimas 20 de {len(log)} acciones"}
    else:
        summary = {"log": log}

    # Compact notes (últimas 10) — siempre presente para mantener el esquema
    summary["notes"] = notes[-10:]
    
    # Preferences always included
    if prefs:
        summary["preferences"] = prefs
    
    # Global memory injected by main.js (persists between chats)
    if state.get("global"):
        summary["global"] = state["global"]
    
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


def session_note(params):
    """Guarda una nota en la memoria de sesión (p.ej. "la voz es de Laura,
    prefiere reverb corta"). Se acumula con el historial de acciones."""
    text = str(params.get("text", "")).strip()
    if not text:
        return json.dumps({"error": "text vacío"})
    state = _load_session()
    state["notes"].append({"time": time.strftime("%H:%M:%S"), "text": text})
    state["notes"] = state["notes"][-50:]
    _save_session(state)
    return json.dumps({"ok": True, "nota_guardada": text})


def session_clear(params):
    """Borra la memoria de sesión (historial, notas y preferencias)."""
    _save_session({"log": [], "notes": [], "preferences": {}})
    return json.dumps({"ok": True, "mensaje": "Memoria de sesión borrada"})


def session_preference(params):
    """Guarda una PREFERENCIA de la sesión (clave/valor): datos del tema/canción
    actual (p.ej. qué pista es la voz, qué parámetro es el gain, el archivo de
    audio). Vive SOLO en el archivo de sesión de esta conversación y aparece en
    session_summary. NO se propaga a la memoria global: para datos universales
    del usuario usa global_memory_set."""
    key = str(params.get("key", "")).strip()
    if not key:
        return json.dumps({"error": "key vacío"})
    value = params.get("value")
    state = _load_session()
    state["preferences"][key] = value
    _save_session(state)
    return json.dumps({"ok": True, "preferencia": key, "valor": value})


def create_clip(params):
    """Crea un clip MIDI vacío en un slot. length_beats: duración en pulsos."""
    t, c = params["track_index"], params["clip_index"]
    length = float(params.get("length_beats", 4.0))
    if _midi_remote_ok():
        ok, result = _try_midi_remote("create_clip", {
            "track_index": t, "clip_index": c, "length": length})
        if ok:
            _session_log("create_clip", f"pista {t} clip {c} ({length} beats)")
            return json.dumps({"ok": True, "track": t, "clip": c, "length_beats": length, "backend": "midi_remote"})
    ableton.send_cmd("/live/clip_slot/create_clip", t, c, length)
    _session_log("create_clip", f"pista {t} clip {c} ({length} beats)")
    return json.dumps({"ok": True, "track": t, "clip": c, "length_beats": length, "backend": "abletonosc"})


def create_arrangement_clip(params):
    """Crea un clip MIDI VACÍO directamente en el timeline del Arrangement.
    start_time: posición en pulsos (beats) desde el inicio del proyecto
    (compás 1 = beat 0; 4 beats = 1 compás en 4/4).
    length_beats: duración del clip. Después usa add_notes con view='arrangement'
    para escribir las notas dentro."""
    t = params["track_index"]
    start = float(params.get("start_time", 0.0))
    length = float(params.get("length_beats", 16.0))
    name = str(params.get("name", "")).strip()
    if _midi_remote_ok():
        ok, result = _try_midi_remote("create_arrangement_midi_clip", {
            "track_index": t, "start_time": start, "length": length}, timeout=20.0)
        if ok and isinstance(result, dict) and result.get("ok"):
            clip_index = result.get("arrangement_clip_index")
            if name:
                _try_midi_remote("set_clip_name", {
                    "track_index": t, "clip_index": clip_index if clip_index is not None else 0,
                    "name": name, "view": "arrangement"}, timeout=10.0)
            invalidate_song_cache()
            _session_log("create_arrangement_clip",
                         f"pista {t} arrangement @beat {start} ({length} beats)")
            return json.dumps({"ok": True, "track": t, "start_time": start,
                               "length_beats": length, "view": "arrangement",
                               "backend": "midi_remote"})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"error": result["error"]})
    return json.dumps({"error": "creación en Arrangement requiere el MIDI Remote Script (no disponible)"})


def add_notes(params):
    """Pega notas MIDI en un clip. notes: string JSON con la lista
    [{"pitch": 60, "start_time": 0.0, "duration": 0.5, "velocity": 100}, ...]
    start_time y duration en pulsos (beats). view: 'session' (default) o
    'arrangement' (clip_index = índice del clip en el timeline de la pista)."""
    t, c = params["track_index"], params["clip_index"]
    view = str(params.get("view", "session") or "session")
    try:
        if isinstance(params["notes"], str):
            raw = params["notes"]
            try:
                notes = json.loads(raw)
            except Exception:
                # Tolerancia a JSON del modelo: comas colgadas antes de } o ]
                cleaned = re.sub(r",\s*([\]}])", r"\1", raw)
                notes = json.loads(cleaned)
        else:
            notes = params["notes"]
    except Exception as exc:
        return _json_error(exc, "notes inválido: ")

    # Try MIDI Remote Script first
    if _midi_remote_ok():
        ok, result = _try_midi_remote("add_notes_to_clip", {
            "track_index": t, "clip_index": c, "notes": notes, "view": view})
        if ok:
            added = result.get("note_count", len(notes)) if isinstance(result, dict) else len(notes)
            _session_log("add_notes", f"pista {t} clip {c} [{view}]: {added} notas")
            return json.dumps({"ok": True, "track": t, "clip": c, "view": view,
                               "notes_added": added, "backend": "midi_remote"})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"error": result["error"]})

    # Fallback to AbletonOSC
    args = [t, c]
    for n in notes:
        args += [
            int(n["pitch"]),
            float(n.get("start_time", 0.0)),
            float(n.get("duration", 0.25)),
            int(n.get("velocity", 100)),
            0,
        ]
    ableton.send_cmd("/live/clip/add/notes", *args)
    _session_log("add_notes", f"pista {t} clip {c}: {len(notes)} notas")
    return json.dumps({"ok": True, "track": t, "clip": c, "notes_added": len(notes), "backend": "abletonosc"})


def get_tempo(params):
    """Devuelve el tempo actual de la sesión (BPM)."""
    if _midi_remote_ok():
        ok, result = _try_midi_remote("get_transport_state")
        if ok and isinstance(result, dict) and "tempo" in result:
            return json.dumps({"ok": True, "bpm": result["tempo"], "backend": "midi_remote"})
    try:
        bpm = float(ableton.query("/live/song/get/tempo")[0])
        return json.dumps({"ok": True, "bpm": bpm, "backend": "abletonosc"})
    except Exception as exc:
        return _json_error(exc)


def fire_clip(params):
    """Lanza (reproduce) el clip indicado."""
    t, c = params["track_index"], params["clip_index"]
    if _midi_remote_ok():
        ok, result = _try_midi_remote("fire_clip", {"track_index": t, "clip_index": c})
        if ok:
            _session_log("fire_clip", f"pista {t} clip {c}")
            return json.dumps({"ok": True, "track": t, "clip": c, "backend": "midi_remote"})
    ableton.send_cmd("/live/clip_slot/fire", t, c)
    _session_log("fire_clip", f"pista {t} clip {c}")
    return json.dumps({"ok": True, "track": t, "clip": c, "backend": "abletonosc"})


def undo(params):
    """Deshace la última operación en Ableton."""
    if _midi_remote_ok():
        ok, result = _try_midi_remote("undo")
        if ok:
            _session_log("undo", "deshacer")
            return json.dumps({"ok": True, "backend": "midi_remote"})
    ableton.send_cmd("/live/song/undo")
    _session_log("undo", "deshacer")
    return json.dumps({"ok": True, "backend": "abletonosc"})


def _normalized_to_param_value(t, d, p, v01):
    """Convierte un valor normalizado 0-1 al rango real del parámetro."""
    mins, maxs, _ = _param_ranges(t, d)
    lo = mins[p] if p < len(mins) else 0.0
    hi = maxs[p] if p < len(maxs) else 1.0
    return lo + v01 * (hi - lo)


def automate_clip_param(params):
    """Escribe automatización en un parámetro de device de un SESSION clip
    (p. ej. el Gain de un Utility) vía el envelope del clip. points: string
    JSON con [{"time": beats (desde inicio del clip), "value": 0-1}, ...].
    El valor se normaliza al rango real del parámetro. No requiere play.
    Después usa clip_to_arrangement para pasarla al timeline."""
    t = params["track_index"]
    c = int(params.get("clip_index", 0))
    d = int(params.get("device_index", 0))
    p = int(params.get("param_index", 9))
    try:
        points = json.loads(params["points"]) if isinstance(params["points"], str) else params["points"]
    except Exception as exc:
        return _json_error(exc, "points inválido: ")
    if not points:
        return json.dumps({"ok": False, "error": "points vacío"})

    # Try MIDI Remote Script first
    if _midi_remote_ok():
        param_name = params.get("parameter") or params.get("param_name")
        if not param_name:
            ok, result = _try_midi_remote("get_device_parameters", {
                "track_index": t, "device_index": d}, timeout=10.0)
            if ok and isinstance(result, dict):
                parameters = result.get("parameters", [])
                if p < len(parameters):
                    param_name = parameters[p].get("name", "")
        if param_name:
            ok, result = _try_midi_remote("set_clip_automation", {
                "track_index": t, "clip_index": c, "view": "session",
                "parameter": param_name, "device_index": d,
                "points": points, "replace": True}, timeout=15.0)
            if ok:
                if isinstance(result, dict) and result.get("ok"):
                    _session_log("automate_clip_param",
                                 f"pista {t} clip {c} device {d} param '{param_name}': {len(points)} puntos [midi_remote]")
                    return json.dumps({
                        "ok": True, "track": t, "clip_index": c, "device_index": d,
                        "param_index": p, "points_written": len(points), "backend": "midi_remote",
                        "note": "Automatización escrita en el clip de SESIÓN. Para verla en el timeline usa clip_to_arrangement."})
                if isinstance(result, dict) and result.get("confirmation_required"):
                    return json.dumps({"ok": False, "error": result.get("message", "envelope exists")})

    try:
        args = [t, c, d, p]
        for pt in points:
            args += [
                float(pt.get("time", 0.0)),
                _normalized_to_param_value(t, d, p, max(0.0, min(1.0, float(pt.get("value", 0.0))))),
            ]
        reply = ableton.query("/live/automation/write_param", *args, timeout=60.0)
    except Exception as exc:
        return _json_error(exc, "No se pudo automatizar el parámetro: ")
    _session_log("automate_clip_param",
                 f"pista {t} clip {c} device {d} param {p}: {len(points)} puntos")
    return json.dumps({
        "ok": True,
        "track": t,
        "clip_index": c,
        "device_index": d,
        "param_index": p,
        "points_written": reply[-1] if reply else 0,
        "note": "Automatización escrita en el clip de SESIÓN. Para verla en el timeline usa clip_to_arrangement.",
    })


def clear_clip_param(params):
    """Borra la automatización del parámetro de device en un session clip."""
    t = params["track_index"]
    c = int(params.get("clip_index", 0))
    d = int(params.get("device_index", 0))
    p = int(params.get("param_index", 9))

    # Try MIDI Remote Script first
    if _midi_remote_ok():
        param_name = params.get("parameter") or params.get("param_name")
        if not param_name:
            ok, result = _try_midi_remote("get_device_parameters", {
                "track_index": t, "device_index": d}, timeout=10.0)
            if ok and isinstance(result, dict):
                parameters = result.get("parameters", [])
                if p < len(parameters):
                    param_name = parameters[p].get("name", "")
        if param_name:
            ok, result = _try_midi_remote("set_clip_automation", {
                "track_index": t, "clip_index": c, "view": "session",
                "parameter": param_name, "device_index": d,
                "points": [], "replace": True}, timeout=15.0)
            if ok and isinstance(result, dict) and result.get("ok"):
                _session_log("clear_clip_param", f"pista {t} clip {c} device {d} param '{param_name}' [midi_remote]")
                return json.dumps({"ok": True, "track": t, "clip_index": c,
                                   "device_index": d, "param_index": p, "backend": "midi_remote"})

    try:
        ableton.query("/live/automation/clear_param", t, c, d, p, timeout=10.0)
    except Exception as exc:
        return _json_error(exc)
    _session_log("clear_clip_param", f"pista {t} clip {c} device {d} param {p}")
    return json.dumps({"ok": True, "track": t, "clip_index": c, "device_index": d, "param_index": p})


def get_clip_param_envelope(params):
    """Comprueba si un parámetro de device tiene envelope de automatización en
    el session clip (exists, point_count)."""
    t = params["track_index"]
    c = int(params.get("clip_index", 0))
    d = int(params.get("device_index", 0))
    p = int(params.get("param_index", 9))

    # Try MIDI Remote Script first
    if _midi_remote_ok():
        param_name = params.get("parameter") or params.get("param_name")
        if not param_name:
            ok, result = _try_midi_remote("get_device_parameters", {
                "track_index": t, "device_index": d}, timeout=10.0)
            if ok and isinstance(result, dict):
                parameters = result.get("parameters", [])
                if p < len(parameters):
                    param_name = parameters[p].get("name", "")
        if param_name:
            ok, result = _try_midi_remote("get_clip_automation", {
                "track_index": t, "clip_index": c, "view": "session",
                "parameter": param_name, "device_index": d}, timeout=15.0)
            if ok and isinstance(result, dict) and "has_envelope" in result:
                return json.dumps({"ok": True, "track": t, "clip_index": c, "device_index": d,
                                   "param_index": p, "has_envelope": result.get("has_envelope"),
                                   "points": result.get("point_count", 0), "backend": "midi_remote"})

    try:
        reply = ableton.query("/live/automation/get_param_envelope", t, c, d, p, timeout=3.0)
    except Exception as exc:
        return _json_error(exc)
    exists = bool(reply[-2]) if len(reply) >= 2 else False
    count = reply[-1] if len(reply) >= 1 else 0
    return json.dumps({"ok": True, "track": t, "clip_index": c, "device_index": d,
                       "param_index": p, "has_envelope": exists, "points": count})


def clip_to_arrangement(params):
    """Copia un SESSION clip (con su automatización) al timeline del
    Arrangement en una posición. start_time en pulsos (beats). No requiere
    play: Live 12 convierte la automatización del clip en track-based.
    track_index = pista (ej. 6 = '7-Audio'), clip_index = slot (0)."""
    t = params["track_index"]
    c = int(params.get("clip_index", 0))
    start = float(params.get("start_time", 0.0))
    try:
        reply = ableton.query("/live/automation/to_arrangement", t, c, start, timeout=5.0)
    except Exception as exc:
        return _json_error(exc, "No se pudo copiar al arrangement: ")
    status = reply[-1] if reply else "error"
    _session_log("clip_to_arrangement", f"pista {t} clip {c} -> beat {start} ({status})")
    return json.dumps({"ok": status == "ok", "track": t, "clip_index": c,
                       "start_time": start, "status": status,
                       "note": "El clip quedó en el timeline del Arrangement con su automatización."})


def list_arrangement_clips(params):
    """Lista los clips del Arrangement de una pista (nombres, longitudes en
    beats y posiciones de inicio). Util para saber cuántos clips hay y cuál
    borrar antes de clip_to_arrangement."""
    t = params["track_index"]

    # Try MIDI Remote Script first
    if _midi_remote_ok():
        ok, result = _try_midi_remote("get_arrangement_map", timeout=15.0)
        if ok and isinstance(result, dict):
            for tr in result.get("tracks", []):
                if tr.get("index") == t or tr.get("name") == str(t):
                    clips = tr.get("clips", [])
                    return json.dumps({"ok": True, "track": t, "clips": clips,
                                       "n": len(clips), "backend": "midi_remote"})
            return json.dumps({"ok": True, "track": t, "clips": [], "n": 0, "backend": "midi_remote"})

    try:
        names = ableton.query("/live/track/get/arrangement_clips/name", t, timeout=5.0)
        lengths = ableton.query("/live/track/get/arrangement_clips/length", t, timeout=5.0)
        starts = ableton.query("/live/track/get/arrangement_clips/start_time", t, timeout=5.0)
    except Exception as exc:
        return _json_error(exc, "No se pudieron listar los clips: ")
    # AbletonOSC antepone el índice de pista a la respuesta; lo descartamos
    names = list(names[1:]) if names and names[0] == t else list(names or [])
    lengths = list(lengths[1:]) if lengths and lengths[0] == t else list(lengths or [])
    starts = list(starts[1:]) if starts and starts[0] == t else list(starts or [])
    clips = []
    n = len(names)
    for i in range(n):
        clips.append({
            "index": i,
            "name": names[i] if i < len(names) else None,
            "length_beats": lengths[i] if i < len(lengths) else None,
            "start_beats": starts[i] if i < len(starts) else None,
        })
    return json.dumps({"ok": True, "track": t, "n_clips": n, "clips": clips})


def delete_arrangement_clip(params):
    """Borra un clip del Arrangement por su índice en track.arrangement_clips
    (0-based). El índice lo obtienes consultando el estado del Arrangement."""
    t = params["track_index"]
    arr_clip_index = int(params.get("arr_clip_index", 0))
    if _midi_remote_ok():
        ok, result = _try_midi_remote("delete_arrangement_clip", {
            "track_index": t, "clip_index": arr_clip_index}, timeout=15.0)
        if ok and isinstance(result, dict) and result.get("ok"):
            invalidate_song_cache()
            _session_log("delete_arrangement_clip", f"pista {t} arrangement clip {arr_clip_index} [midi_remote]")
            return json.dumps({"ok": True, "track": t, "arr_clip_index": arr_clip_index,
                               "backend": "midi_remote"})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"error": result["error"]})
    try:
        reply = ableton.query("/live/automation/delete_arrangement_clip", t, arr_clip_index, timeout=5.0)
    except Exception as exc:
        return _json_error(exc, "No se pudo borrar el clip: ")
    status = reply[-1] if reply else "error"
    _session_log("delete_arrangement_clip", f"pista {t} arrangement clip {arr_clip_index} ({status})")
    return json.dumps({"ok": status == "ok", "track": t, "arr_clip_index": arr_clip_index,
                       "status": status})


def set_follow_action(params):
    """Programa la transición automática A→B de un clip: al terminar de sonar,
    salta solo a la parte siguiente. action: 0 none, 1 stop, 2 play again,
    3 next, 4 previous, 5 first, 6 last. time: (opcional) follow_action_time en
    pulsos (duración de la voz en la parte A). index: (opcional) destino si el
    action es next/previous relativo. Envía cada propiedad por separado al
    helper M4L (/clip_follow_action) y espera confirmación."""
    t = params["track_index"]
    c = params["clip_index"]
    action = params["action"]
    try:
        action = int(action)
    except Exception:
        return json.dumps({"error": f"action debe ser int 0-6: {action}"})
    if not 0 <= action <= 6:
        return json.dumps({"error": f"action fuera de rango 0-6: {action}"})
    calls = [["/clip_follow_action", t, c, "follow_action", action]]
    if params.get("time") is not None:
        calls.append(["/clip_follow_action", t, c, "follow_action_time", float(params["time"])])
    if params.get("index") is not None:
        calls.append(["/clip_follow_action", t, c, "follow_action_index", int(params["index"])])
    results = []
    for call in calls:
        ok, reply = _send_helper(call)
        if not ok:
            return json.dumps({"error": reply})
        results.append(reply)
    _session_log("set_follow_action",
                 f"pista {t} clip {c} action={action} "
                 f"time={params.get('time')} index={params.get('index')}")
    return json.dumps({
        "ok": True,
        "track": t,
        "clip_index": c,
        "action": action,
        "time": params.get("time"),
        "index": params.get("index"),
        "helper": results,
        "note": "Al terminar de sonar este clip saltará solo a la parte B "
                "(o al destino indicado). No requiere play.",
    })


def browser_list(params):
    """Lista los elementos (plugins, instrumentos, packs, sonidos) disponibles
    en una categoría del navegador de Ableton. category: 'plugins' | 'instruments'
    | 'drums' | 'samples' | 'packs' | 'sounds' | 'user_library' | 'audio_effects'
    | 'midi_effects'. max_items: cuántos nombres devolver (default 20).
    query: (opcional) filtra por substring (sin mayúsculas) — ÚSALO para buscar
    un plugin concreto sin descargar toda la lista."""
    category = params.get("category", "plugins")
    max_items = int(params.get("max_items", 20))
    query = str(params.get("query", "")).strip()

    # Try MIDI Remote Script first
    if _midi_remote_ok():
        ok, result = _try_midi_remote("get_browser_items_at_path", {"path": category})
        items = result.get("items", []) if ok and isinstance(result, dict) else []
        if query and items:
            filtered = [i for i in items if _matches_query(str(i.get("name", "")), query)]
            items = filtered if filtered else items
        # Categoría inexistente (p.ej. "presets") o sin resultados: scan global
        if not items and query:
            ok2, scan_result = _try_midi_remote("scan_browser_full", {
                "categories": ["instruments", "sounds", "drums", "audio_effects",
                               "midi_effects", "packs", "plugins"],
                "max_depth": 5,
            }, timeout=60.0)
            if ok2 and isinstance(scan_result, dict):
                items = [i for i in scan_result.get("items", [])
                         if _matches_query(str(i.get("name", "")), query)]
        if items:
            names = [i.get("name", "") for i in items[:max_items]]
            _session_log("browser_list", f"{category}: {len(names)} elementos [midi_remote]")
            return json.dumps({"ok": True, "category": category, "n": len(names),
                               "items": names, "backend": "midi_remote"})

    # Fallback to AbletonOSC
    try:
        reply = ableton.query("/live/browser/list_children", category, max_items, timeout=8.0)
    except Exception as exc:
        return _json_error(exc, "No se pudo listar el browser: ")
    names = list(reply[1:]) if len(reply) > 1 else []
    if query:
        names = [n for n in names if query in str(n).lower()]
    _session_log("browser_list", f"{category}: {len(names)} elementos [abletonosc]")
    return json.dumps({"ok": True, "category": category, "n": len(names), "items": names, "backend": "abletonosc"})


def browser_load(params):
    """Carga desde el navegador de Ableton el primer elemento cuyo nombre
    contenga query, insertándolo en la pista indicada (es más fiable que
    insert_device para plugins y packs de terceros como Magenta Studio).
    query: texto a buscar (sin mayúsculas ni espacios al inicio)."""
    t = params["track_index"]
    query = params.get("query", "")

    # Try MIDI Remote Script first
    if _midi_remote_ok():
        ok, scan_result = _try_midi_remote("scan_browser_full", {
            "categories": ["instruments", "sounds", "drums", "audio_effects",
                           "midi_effects", "packs", "plugins"],
            "max_depth": 5,
        }, timeout=60.0)
        if ok and isinstance(scan_result, dict):
            items = scan_result.get("items", [])
            best_match = None
            for item in items:
                if not _matches_query(str(item.get("name", "")), query):
                    continue
                if item.get("is_loadable"):
                    best_match = item
                    break
                if not best_match:
                    best_match = item
            if best_match:
                load_ok, load_result = _try_midi_remote("load_browser_item", {
                    "track_index": t,
                    "uri": best_match.get("uri", ""),
                    "item_path": best_match.get("path", ""),
                }, timeout=30.0)
                if load_ok and isinstance(load_result, dict) and load_result.get("ok"):
                    loaded_name = best_match.get("name", query)
                    _session_log("browser_load", f"pista {t} query '{query}' -> {loaded_name} [midi_remote]")
                    invalidate_song_cache()
                    return json.dumps({"ok": True, "track": t, "query": query,
                                       "loaded": loaded_name, "backend": "midi_remote"})

    # Fallback to AbletonOSC
    try:
        reply = ableton.query("/live/browser/load_by_query", t, query, timeout=12.0)
    except Exception as exc:
        return _json_error(exc, "No se pudo cargar del browser: ")
    loaded = reply[-1] if reply else ""
    _session_log("browser_load", f"pista {t} query '{query}' -> {loaded or 'no encontrado'} [abletonosc]")
    return json.dumps({"ok": bool(loaded), "track": t, "query": query, "loaded": loaded, "backend": "abletonosc"})


def global_memory_get(params):
    """Lee un valor de la memoria global (persiste entre TODOS los chats).
    category: 'preferences' | 'user_profile' | 'project_defaults'. key: nombre del campo."""
    data = _load_global_memory()
    category = params.get("category", "preferences")
    key = params.get("key", "")
    if category not in data:
        return json.dumps({"ok": False, "error": f"categoría '{category}' no existe"})
    if key:
        value = data[category].get(key)
        return json.dumps({"ok": True, "category": category, "key": key, "value": value}, ensure_ascii=False, separators=(",", ":"))
    return json.dumps({"ok": True, "category": category, "data": data[category]}, ensure_ascii=False, separators=(",", ":"))


def global_memory_set(params):
    """Escribe un valor en la memoria global (persiste entre TODOS los chats).
    category: 'preferences' | 'user_profile' | 'project_defaults'. key/value: dato a guardar."""
    data = _load_global_memory()
    category = params.get("category", "preferences")
    key = str(params.get("key", "")).strip()
    if not key:
        return json.dumps({"error": "key vacío"})
    if category not in data:
        data[category] = {}
    data[category][key] = params.get("value")
    _save_global_memory(data)
    return json.dumps({"ok": True, "category": category, "key": key, "value": params.get("value")}, separators=(",", ":"))


def global_memory_list(params):
    """Lista toda la memoria global (preferencias, perfil de usuario, defaults del proyecto)."""
    data = _load_global_memory()
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def global_memory_delete(params):
    """Borra una clave de la memoria global."""
    data = _load_global_memory()
    category = params.get("category", "preferences")
    key = params.get("key", "")
    if category not in data or key not in data[category]:
        return json.dumps({"ok": False, "error": "clave no encontrada"})
    del data[category][key]
    _save_global_memory(data)
    return json.dumps({"ok": True, "deleted": f"{category}.{key}"}, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Registro de tools (nombre -> (descripción, schema de params, handler))
# ---------------------------------------------------------------------------

AUDIO_LISTEN_URL = "http://127.0.0.1:8787/listen"
AUDIO_ANALYZE_URL = "http://127.0.0.1:8787/analyze_file"


def listen_master(params):
    """Pide al audio server que escuche el master (WASAPI loopback) y
    devuelve el análisis."""
    try:
        import urllib.request
        import urllib.parse

        seconds = params.get("seconds") if params else None
        url = AUDIO_LISTEN_URL
        if seconds:
            url += "?" + urllib.parse.urlencode({"seconds": seconds})
        with urllib.request.urlopen(url, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return json.dumps({"ok": data.get("ok", False), **data})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def analyze_clip(params):
    """Analiza el archivo de audio de un SESSION clip SIN reproducirlo y SIN
    loopback: obtiene su file_path (LOM, Live 11+) y delega en el audio server
    para leer el archivo del disco. Devuelve el mismo análisis que
    ableton_listen (signal, rms_db, dominante, bands_db, duración...)."""
    import urllib.request
    import urllib.parse

    try:
        t = int(params["track_index"])
        c = int(params.get("clip_index", 0))
        try:
            reply = ableton.query("/live/clip/get/file_path", t, c, timeout=3.0)
            path = reply[2] if len(reply) > 2 else ""
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"no se pudo consultar file_path: {exc}"})
        if not path:
            return json.dumps({"ok": False, "error": "el clip no tiene archivo de audio (¿es un clip MIDI o está vacío?)"})
        url = AUDIO_ANALYZE_URL + "?" + urllib.parse.urlencode({"path": path})
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return json.dumps({"ok": data.get("ok", False), **data})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def set_clip_warping(params):
    """Activa/desactiva el warp de un SESSION clip y opcionalmente su modo
    (warp_mode: 0 Beats, 1 REX, 2 Tones, 3 Texture, 4 Complex, 5 Complex Pro).
    Úsalo al cargar audio para que los stems de la misma fuente no se
    desalineen (warp OFF = cada clip respeta su duración original)."""
    try:
        t = int(params["track_index"])
        c = int(params.get("clip_index", 0))
        warping = 1 if params.get("warping", True) else 0

        # Try MIDI Remote Script first
        if _midi_remote_ok():
            ok, result = _try_midi_remote("set_clip_warping", {
                "track_index": t, "clip_index": c,
                "warping": bool(warping), "view": "session"})
            if ok:
                mode = params.get("warp_mode")
                if mode is not None:
                    # MIDI Remote Script doesn't have set_warp_mode yet, use OSC
                    ableton.send_cmd("/live/clip/set/warp_mode", t, c, int(mode))
                _session_log("set_clip_warping", f"pista {t} clip {c} warping={warping}")
                return json.dumps({"ok": True, "track": t, "clip": c,
                                   "warping": bool(warping), "warp_mode": mode, "backend": "midi_remote"})

        # Fallback to AbletonOSC
        ableton.send_cmd("/live/clip/set/warping", t, c, warping)
        mode = params.get("warp_mode")
        if mode is not None:
            ableton.send_cmd("/live/clip/set/warp_mode", t, c, int(mode))
        _session_log("set_clip_warping", f"pista {t} clip {c} warping={warping}")
        return json.dumps({"ok": True, "track": t, "clip": c,
                           "warping": bool(warping), "warp_mode": mode, "backend": "abletonosc"})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def get_clip_info(params):
    """Info del archivo fuente y estado de un clip: file_path, is_audio_clip,
    is_midi_clip, warping, warp_mode, sample_length, start/end marker, loop
    bounds, length. Acepta view 'arrangement' (default) o 'session'. Úsalo
    para saber de dónde viene un clip y su estado de warp antes de editarlo."""
    try:
        t = int(params["track_index"])
        c = int(params.get("clip_index", 0))
        view = params.get("view", "arrangement")
        if view == "session":
            # Try MIDI Remote Script first
            if _midi_remote_ok():
                ok, result = _try_midi_remote("get_clip_audio_info", {
                    "track_index": t, "clip_index": c, "view": "session"}, timeout=10.0)
                if ok and isinstance(result, dict) and "name" in result:
                    result["view"] = "session"
                    result["track_index"] = t
                    result["clip_index"] = c
                    return json.dumps({"ok": True, **result})
            reply = ableton.query("/live/clip/get/file_path", t, c, timeout=3.0)
            path = reply[2] if len(reply) > 2 else ""
            clip = {
                "view": "session",
                "file_path": path,
                "track_index": t,
                "clip_index": c,
            }
            for prop in ("is_audio_clip", "is_midi_clip", "warping", "warp_mode",
                         "sample_length", "length", "start_marker", "end_marker",
                         "loop_start", "loop_end", "looping"):
                try:
                    r = ableton.query(f"/live/clip/get/{prop}", t, c, timeout=3.0)
                    clip[prop] = r[2] if len(r) > 2 else r[0] if len(r) == 1 else r
                except Exception:
                    pass
            return json.dumps({"ok": True, **clip})
        # view arrangement: usa list_song + track.get arrangement info (AbletonOSC).
        info = list_arrangement_clips({"track_index": t})
        parsed = json.loads(info) if isinstance(info, str) else info
        if isinstance(parsed, dict) and parsed.get("clips"):
            clips = parsed["clips"]
            if 0 <= c < len(clips):
                return json.dumps({"ok": True, "view": "arrangement",
                                   "track_index": t, "clip_index": c, **clips[c]})
        return json.dumps({"ok": False, "error": "no se encontró el clip en el arrangement"})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def load_audio_clip(params):
    """Carga un archivo de audio en un clip slot (session) usando el helper
    M4L (track.create_audio_clip vía Live 11+). Después apaga el warp por OSC
    (warping 0) para que los stems de la misma fuente queden alineados.
    Devuelve el track/clip y el estado final. Para el timeline usa después
    clip_to_arrangement."""
    import urllib.parse
    import os

    try:
        t = int(params["track_index"])
        c = int(params.get("clip_index", 0))
        path = params.get("path") or _require_path(params)
        if not path:
            return json.dumps({"ok": False, "error": "hacen falta path o track_index/clip_index de un session clip con archivo."})
        if not os.path.exists(path):
            return json.dumps({"ok": False, "error": f"archivo no encontrado: {path}"})

        # Try MIDI Remote Script first
        if _midi_remote_ok():
            ok, result = _try_midi_remote("load_audio_clip", {
                "track_index": t, "clip_index": c,
                "file_path": path, "view": "session"}, timeout=45.0)
            if ok and isinstance(result, dict) and result.get("ok"):
                _session_log("load_audio_clip", f"pista {t} clip {c} <- {path} [midi_remote]")
                return json.dumps({"ok": True, "track_index": t, "clip_index": c,
                                   "file_path": path, "warping": False,
                                   "backend": "midi_remote"})

        # Fallback to M4L helper
        ok, reply = _send_helper(["/load_audio", t, c, path], timeout=8.0)
        if not ok:
            return json.dumps({"ok": False, "error": reply})
        # Warp OFF para que el clip respete su duración original.
        ableton.send_cmd("/live/clip/set/warping", t, c, 0)
        _session_log("load_audio_clip", f"pista {t} clip {c} <- {path} [m4l]")
        return json.dumps({"ok": True, "track_index": t, "clip_index": c,
                           "file_path": path, "warping": False, "helper": reply,
                           "backend": "m4l"})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def _audio_request(endpoint, params, timeout=60):
    """GET genérico al audio server (endpoint sin la barra). Devuelve el JSON
    del servidor o un dict de error."""
    import urllib.request
    import urllib.parse

    url = f"http://127.0.0.1:8787/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _require_path(params):
    """Extrae file_path del session clip (como analyze_clip) si el usuario
    pasa track_index/clip_index, o acepta un path directo."""
    import urllib.parse

    if params.get("path"):
        return params["path"]
    try:
        t = int(params["track_index"])
        c = int(params.get("clip_index", 0))
        # Try MIDI Remote Script first
        if _midi_remote_ok():
            ok, result = _try_midi_remote("get_clip_audio_info", {
                "track_index": t, "clip_index": c, "view": "session"}, timeout=10.0)
            if ok and isinstance(result, dict) and result.get("file_path"):
                return result["file_path"]
        reply = ableton.query("/live/clip/get/file_path", t, c, timeout=3.0)
        path = reply[2] if len(reply) > 2 else ""
        return path or None
    except Exception:
        return None


def analyze_key(params):
    """Detecta tonalidad (key), modo, tempo y compás de un archivo de audio o
    de un session clip, SIN reproducirlo. Para el proyecto entero usa la del
    clip de referencia o pide el archivo."""
    path = _require_path(params)
    if not path:
        return json.dumps({"ok": False, "error": "no se encontró el archivo. Pasa path o track_index/clip_index de un session clip."})
    data = _audio_request("analyze", {"path": path})
    return json.dumps({"ok": data.get("ok", False), **data})


def get_chords(params):
    """Detecta los acordes que suenan en cada barra del proyecto de un archivo
    (o session clip), alineados al grid (tempo, beats_per_bar, start_bar).
    Devuelve por barra el acorde y su energía. Úsalo para saber la armonía de
    una canción y componer encima coherente."""
    path = _require_path(params)
    if not path:
        return json.dumps({"ok": False, "error": "no se encontró el archivo."})
    q = {"path": path}
    for k in ("tempo", "beats_per_bar", "start_bar", "max_seconds"):
        if params.get(k) is not None:
            q[k] = params[k]
    data = _audio_request("harmony", q)
    return json.dumps({"ok": data.get("ok", False), **data})


def get_groove(params):
    """Mide el timing de un archivo de audio (o session clip): dónde caen los
    golpes respecto al grid del proyecto (tempo). Devuelve offset medio en
    beats/ms, % de golpes a tiempo y ratio de swing. Úsalo para saber si una
    grabación está bien cuantizada y cuánto hay que corregir."""
    path = _require_path(params)
    if not path:
        return json.dumps({"ok": False, "error": "no se encontró el archivo."})
    q = {"path": path}
    for k in ("tempo", "beats_per_bar", "time_scale"):
        if params.get(k) is not None:
            q[k] = params[k]
    data = _audio_request("groove", q)
    return json.dumps({"ok": data.get("ok", False), **data})


def transcribe_file_tool(params):
    """Transcribe un archivo de audio (o session clip) completo a notas MIDI.
    Usa Basic Pitch (polifónico) si está instalado, si no librosa (monofónico).
    Devuelve notas cuantizadas {pitch, start_time, duration, velocity} para
    meterlas en un clip con create_clip/add_notes. Úsalo para extraer melodías
    o acordes de un audio (voz, instrumento) SIN reproducirlo."""
    path = _require_path(params)
    if not path:
        return json.dumps({"ok": False, "error": "no se encontró el archivo."})
    q = {"path": path}
    if params.get("grid"):
        q["grid"] = params["grid"]
    data = _audio_request("transcribe_file", q, timeout=90)
    return json.dumps({"ok": data.get("ok", False), **data})


def separate_stems_tool(params):
    """Separa un archivo de audio (o session clip) en stems con Demucs:
    drums, bass, vocals, other. Con probe=true solo comprueba si ya hay stems
    cacheados (rápido, no arranca el modelo). Requiere pip install demucs la
    primera vez. Devuelve las rutas de los stems para cargarlos en pistas."""
    path = _require_path(params)
    if not path:
        return json.dumps({"ok": False, "error": "no se encontró el archivo."})
    q = {"path": path, "quality": params.get("quality", "fast")}
    if params.get("probe"):
        q["probe"] = "true"
    data = _audio_request("stems", q, timeout=300)
    return json.dumps({"ok": data.get("ok", False), **data})


def compose_accompaniment_tool(params):
    """Genera un acompañamiento (bajo + acordes) a partir de una progresión y
    lo guarda como .mid + .notes.json. progression: JSON string con
    [{"chord": "Am", "from_bar": 1, "to_bar": 4}, ...]. Devuelve midi_path con
    las notas; carga el MIDI con load_midi_file si lo añades. Úsalo para crear
    una base armónica que siga la voz del proyecto."""
    q = {}
    for k in ("tempo", "beats_per_bar", "start_bar", "bars", "progression"):
        if params.get(k) is not None:
            q[k] = params[k]
    data = _audio_request("compose", q)
    return json.dumps({"ok": data.get("ok", False), **data})


def describe_sound_tool(params):
    """Describe el TIMBRE de un archivo (o session clip) para encontrar sonidos
    parecidos: tags (dark/warm/bright, sustained/rhythmic, harmonic...), key y
    queries de búsqueda del browser de Ableton. Úsalo para sugerir qué sample o
    plugin usar cuando el usuario dice 'quiero un sonido como este'."""
    path = _require_path(params)
    if not path:
        return json.dumps({"ok": False, "error": "no se encontró el archivo."})
    data = _audio_request("describe", {"path": path})
    return json.dumps({"ok": data.get("ok", False), **data})


def compare_mix(params):
    """Compara tu mezcla contra una referencia: balance por bandas (sub, bass,
    low_mid, mid, high_mid, high) en dB de diferencia, loudness (rms_db) y
    rango dinámico (crest), con consejos de qué subir/bajar en el bus. Pass
    mix_path y reference_path (o track_index de tu mezcla). Úsalo para dar
    feedback de calidad de mezcla objetivo."""
    mix = params.get("mix_path") or _require_path(params)
    ref = params.get("reference_path")
    if not ref:
        ref_params = {
            "track_index": params.get("reference_track"),
            "clip_index": params.get("reference_clip"),
        }
        ref = _require_path(ref_params)
    if not mix or not ref:
        return json.dumps({"ok": False, "error": "hacen falta mix y reference (paths o track_index)."})
    data = _audio_request("compare", {"mix": mix, "reference": ref})
    return json.dumps({"ok": data.get("ok", False), **data})

TOOLS = {
    "ping": {
        "description": "Verifica la conexión con Ableton Live. Devuelve {connected: true} si AbletonOSC responde.",
        "params": {},
        "handler": ping,
    },
    "list_instances": {
        "description": "Detecta todas las instancias de Ableton Live con el script VocalAssistant (cada una en un puerto distinto: 9878, 9879...). Devuelve la activa y todas las detectadas. Úsalo si tienes varias sesiones de Live abiertas a la vez.",
        "params": {},
        "handler": list_instances,
    },
    "select_instance": {
        "description": "Elige qué instancia de Ableton Live controlar (port viene de list_instances). Cada copia de Live abierta usa un puerto distinto: 9878, 9879, etc. La selección se guarda y se restaura al reiniciar.",
        "params": {"port": "int"},
        "handler": select_instance,
    },
    "list_song": {
        "description": "Lista la sesión optimizado para tokens. Opciones: track=N (filtra por pista), compact=true (solo índices/nombres, sin valores), skip_values=true (omite valores actuales), depth>=1 (árbol anidado racks/cadenas). Ej: list_song(track=6) para ver solo pista 6, list_song(compact=true) para navegación rápida.",
        "params": {"track": "int", "compact": "bool", "skip_values": "bool", "depth": "int"},
        "optional": ["track", "compact", "skip_values", "depth"],
        "handler": list_song,
    },
    "get_device_parameters": {
        "description": "Parámetros de un device concreto: nombres, valores actuales, min, max, si es on/off. Úsalo antes de set_parameter para conocer índices y rangos.",
        "params": {"track_index": "int", "device_index": "int"},
        "handler": get_device_parameters,
    },
    "set_parameter": {
        "description": "Mueve una perilla (parámetro) de un plugin a un valor NORMALIZADO 0–1 (0 = mínimo, 1 = máximo). Ej: set_parameter(0, 1, 2, 0.8).",
        "params": {"track_index": "int", "device_index": "int", "parameter_index": "int", "value01": "float"},
        "handler": set_parameter,
    },
    "set_track": {
        "description": "Ajusta una propiedad de pista: volume (0–1, fader), panning (0–1), mute, solo, arm, monitoring (0/1), name (renombrar), color_index (0–69), send (con send_index + value), input_routing_type/channel y output_routing_type/channel. Ej: set_track(2, 'volume', 0.75).",
        "params": {"track_index": "int", "property_name": "str", "value": "float", "send_index": "int"},
        "optional": ["send_index"],
        "handler": set_track,
    },
    "transport": {
        "description": "Controla el transport de Ableton: play, stop, record, tap_tempo, undo, redo, stop_all_clips.",
        "params": {"action": "str"},
        "handler": transport,
    },
    "set_tempo": {
        "description": "Cambia el tempo de la sesión en BPM (ej: 128.0).",
        "params": {"bpm": "float"},
        "handler": set_tempo,
    },
    "set_metronome": {
        "description": "Enciende (1) o apaga (0) el metrónomo.",
        "params": {"on": "int"},
        "handler": set_metronome,
    },
    "get_scenes": {
        "description": "Lista las escenas de la sesión con sus nombres.",
        "params": {},
        "handler": get_scenes,
    },
    "fire_scene": {
        "description": "Lanza una escena (dispara todos los clips de su fila).",
        "params": {"scene_index": "int"},
        "handler": fire_scene,
    },
    "create_track": {
        "description": "Crea una pista de AUDIO nueva y la nombra. Devuelve su índice.",
        "params": {"name": "str"},
        "handler": create_track,
    },
    "create_midi_track": {
        "description": "Crea una pista MIDI nueva y la nombra. Devuelve su índice.",
        "params": {"name": "str"},
        "handler": create_midi_track,
    },
    "delete_track": {
        "description": "Borra la pista en el índice indicado (no borra pistas de grupo/frozen).",
        "params": {"track_index": "int"},
        "handler": delete_track,
    },
    "insert_device": {
        "description": "Inserta un device NATIVO de Ableton en una pista (vía helper M4L) y espera confirmación. device_class puede ser: Eq8, Compressor, GlueCompressor, Reverb, Delay, Saturator, Limiter, Utility, MultibandDynamics, AutoFilter, Gate, Chorus, Flanger, Phaser. Después verifica con list_song().",
        "params": {"track_index": "int", "device_class": "str"},
        "handler": insert_device,
    },
    "load_plugin": {
        "description": "Busca en el browser de Live un plugin por nombre (VST/AU de terceros o nativos: 'Serum', 'Ozone', 'Valhalla', 'Keyscape', 'Arturia'...) y lo inserta en la pista. Usa el BrowserHandler de AbletonOSC; el nombre puede ser parcial (substring). Después verifica con list_song() qué device se creó y si expone parámetros.",
        "params": {"track_index": "int", "query": "str"},
        "handler": load_plugin,
    },
    "create_rack": {
        "description": "Crea un rack en la pista: kind = audio (AudioEffectRack) | instrument (InstrumentRack) | drum (DrumRack). Después usa chain_create_device para meter devices DENTRO de la cadena del rack. Para cadenas con macros: create_rack + chain_create_device + map_macro.",
        "params": {"track_index": "int", "kind": "str"},
        "handler": create_rack,
    },
    "chain_create_device": {
        "description": "Inserta un device DENTRO de la cadena de un rack ya creado (rack de FX). rack_index = índice del rack en la pista (ver list_song). El device queda dentro de la cadena, no se ve con list_song.",
        "params": {"track_index": "int", "rack_index": "int", "device_class": "str"},
        "handler": chain_create_device,
    },
    "map_macro": {
        "description": "Mapea un parámetro de un device DENTRO de la cadena de un rack a una macro (perilla) del rack. Requiere rack creado con create_rack y devices dentro con chain_create_device. Índices 0-based.",
        "params": {"track_index": "int", "rack_index": "int", "chain_device_index": "int", "parameter_index": "int", "macro_index": "int"},
        "handler": map_macro,
    },
    "session_summary": {
        "description": "Memoria de la sesión: historial de acciones (faders movidos, plugins insertados, cadenas, notas) y PREFERENCIAS guardadas de la IA. Úsala cuando el usuario pida refinar algo ya hecho ('suaviza el compresor', 'dónde quedó el bajo') o para retomar trabajo previo.",
        "params": {},
        "handler": session_summary,
    },
    "session_note": {
        "description": "Guarda una nota en la memoria de sesión (p.ej. 'la voz es de Laura, prefiere reverb corta'). Se acumula con el historial de acciones.",
        "params": {"text": "str"},
        "handler": session_note,
    },
    "session_clear": {
        "description": "Borra toda la memoria de sesión (historial, notas y preferencias).",
        "params": {},
        "handler": session_clear,
    },
    "session_preference": {
        "description": "Guarda una PREFERENCIA persistente de la IA (clave y valor): datos durables que deben recordarse entre sesiones (pista de la voz, índice del device/parámetro del gain, archivo de audio, gustos del usuario, estilos de mezcla). Se acumula en el archivo de sesión y aparece en session_summary.",
        "params": {"key": "str", "value": "str"},
        "handler": session_preference,
    },
    "global_memory_get": {
        "description": "Lee de la memoria GLOBAL (persiste entre TODOS los chats). category: 'preferences' | 'user_profile' | 'project_defaults'. key: (opcional) campo específico. Sin key devuelve toda la categoría.",
        "params": {"category": "str", "key": "str"},
        "optional": ["key"],
        "handler": global_memory_get,
    },
    "global_memory_set": {
        "description": "Escribe en la memoria GLOBAL (persiste entre TODOS los chats). category: 'preferences' | 'user_profile' | 'project_defaults'. Úsalo para datos que SIEMPRE deben recordarse: idioma del usuario, pista de voz, tempo habitual, gustos de mezcla.",
        "params": {"category": "str", "key": "str", "value": "str"},
        "handler": global_memory_set,
    },
    "global_memory_list": {
        "description": "Lista TODA la memoria global (preferencias, perfil de usuario, defaults del proyecto). Úsalo al inicio de un chat nuevo para recordar quién es el usuario y sus configuraciones.",
        "params": {},
        "handler": global_memory_list,
    },
    "global_memory_delete": {
        "description": "Borra una clave de la memoria global.",
        "params": {"category": "str", "key": "str"},
        "handler": global_memory_delete,
    },
    "create_clip": {
        "description": "Crea un clip MIDI vacío en un slot de la pista. length_beats: duración en pulsos (ej: 4 = un compás a 4/4).",
        "params": {"track_index": "int", "clip_index": "int", "length_beats": "float"},
        "handler": create_clip,
    },
    "create_arrangement_clip": {
        "description": "Crea un clip MIDI VACÍO directamente en el timeline del ARRANGEMENT. start_time: posición en beats desde el inicio (compás 1 = beat 0, cada compás 4/4 = 4 beats). length_beats: duración del clip. Después escribe las notas con add_notes usando view='arrangement' (el índice del clip nuevo suele ser el último de la pista; verifica con list_arrangement_clips).",
        "params": {"track_index": "int", "start_time": "float", "length_beats": "float", "name": "str"},
        "optional": ["name"],
        "handler": create_arrangement_clip,
    },
    "add_notes": {
        "description": "Pega notas MIDI en un clip. notes: string JSON con [{pitch, start_time, duration, velocity}] en pulsos (beats). pitch = nota MIDI (60 = C4), velocity 0–127. view: 'session' (default) o 'arrangement' para clips del timeline.",
        "params": {"track_index": "int", "clip_index": "int", "notes": "str", "view": "str"},
        "optional": ["view"],
        "handler": add_notes,
    },
    "get_tempo": {
        "description": "Tempo actual de la sesión en BPM.",
        "params": {},
        "handler": get_tempo,
    },
    "fire_clip": {
        "description": "Lanza (reproduce) el clip indicado de la pista.",
        "params": {"track_index": "int", "clip_index": "int"},
        "handler": fire_clip,
    },
    "undo": {
        "description": "Deshace la última operación en Ableton.",
        "params": {},
        "handler": undo,
    },
    "ableton_listen": {
        "description": "ESCUCHA el audio del master de Ableton (captura por WASAPI loopback de la salida por defecto, ~2 s; puedes pedir más con seconds hasta 8) y analiza: si hay señal real (signal), nivel (rms/peak/rms_db), frecuencia dominante, primer golpe, la región más fuerte (loudest_region_s) y energía por bandas en dB (bands_db: sub, bass, low_mid, mid, high_mid, high). Úsalo SIEMPRE antes de ecualizar/mezclar: si signal=false es silencio y NO hay nada que ecualizar.",
        "params": {"seconds": "float"},
        "handler": listen_master,
    },
    "analyze_clip": {
        "description": "Analiza el audio de un SESSION clip SIN reproducirlo y SIN loopback: lee el archivo del clip desde el disco (vía LOM file_path) y devuelve el mismo análisis que ableton_listen (signal, rms_db, dominant_hz, bands_db, duration_s...). Si el clip es MIDI o está vacío devuelve error. Úsalo en vez de ableton_listen cuando quieras analizar un clip concreto sin darle play.",
        "params": {"track_index": "int", "clip_index": "int"},
        "optional": ["clip_index"],
        "handler": analyze_clip,
    },
    "analyze_key": {
        "description": "Detecta tonalidad (key), modo (mayor/menor), tempo y compás de un archivo de audio o de un session clip SIN reproducirlo. Pasa path (ruta del archivo) o track_index/clip_index de un session clip. Úsalo para saber en qué tonalidad está una canción o pista antes de componer/ajustar.",
        "params": {"path": "str", "track_index": "int", "clip_index": "int"},
        "optional": ["path", "clip_index"],
        "handler": analyze_key,
    },
    "get_chords": {
        "description": "Detecta los acordes que suenan en cada barra del proyecto de un archivo o session clip, alineados al grid (tempo en BPM, beats_per_bar, start_bar). Devuelve por barra el acorde (C, Am, Gsus4...) y su energía. Úsalo para conocer la armonía de una canción y componer encima con coherencia.",
        "params": {"path": "str", "track_index": "int", "clip_index": "int", "tempo": "float", "beats_per_bar": "int", "start_bar": "int", "max_seconds": "float"},
        "optional": ["path", "clip_index", "tempo", "beats_per_bar", "start_bar", "max_seconds"],
        "handler": get_chords,
    },
    "get_groove": {
        "description": "Mide el timing de un archivo de audio o session clip: dónde caen los golpes respecto al grid del proyecto (tempo). Devuelve offset medio en beats/ms, % de golpes a tiempo y ratio de swing. Úsalo para saber si una grabación está bien cuantizada y cuánto corregir.",
        "params": {"path": "str", "track_index": "int", "clip_index": "int", "tempo": "float", "beats_per_bar": "int", "time_scale": "float"},
        "optional": ["path", "clip_index", "tempo", "beats_per_bar", "time_scale"],
        "handler": get_groove,
    },
    "transcribe_file": {
        "description": "Transcribe un archivo de audio o session clip completo a notas MIDI (Basic Pitch polifónico si está instalado; si no, librosa monofónico). Devuelve notas cuantizadas {pitch, start_time, duration, velocity} para meter en un clip con create_clip + add_notes. Úsalo para extraer melodía o acordes de un audio SIN reproducirlo.",
        "params": {"path": "str", "track_index": "int", "clip_index": "int", "grid": "float"},
        "optional": ["path", "clip_index", "grid"],
        "handler": transcribe_file_tool,
    },
    "separate_stems": {
        "description": "Separa un archivo de audio o session clip en stems con Demucs: drums, bass, vocals, other. probe=true solo comprueba si ya hay stems cacheados (rápido, sin arrancar el modelo). Requiere pip install demucs la primera vez. Devuelve las rutas de los stems.",
        "params": {"path": "str", "track_index": "int", "clip_index": "int", "quality": "str", "probe": "bool"},
        "optional": ["path", "clip_index", "quality", "probe"],
        "handler": separate_stems_tool,
    },
    "compose_accompaniment": {
        "description": "Genera un acompañamiento (bajo + acordes) a partir de una progresión y lo guarda como .mid + .notes.json. progression: JSON string con [{\"chord\": \"Am\", \"from_bar\": 1, \"to_bar\": 4}, ...]. Devuelve midi_path con las notas para cargarlas en un clip. Úsalo para crear una base armónica que siga la voz del proyecto.",
        "params": {"tempo": "float", "beats_per_bar": "int", "start_bar": "int", "bars": "int", "progression": "str"},
        "optional": ["beats_per_bar", "start_bar", "bars", "progression"],
        "handler": compose_accompaniment_tool,
    },
    "describe_sound": {
        "description": "Describe el TIMBRE de un archivo o session clip para encontrar sonidos parecidos: tags (dark/warm/bright, sustained/rhythmic, harmonic...), key y queries de búsqueda del browser de Ableton. Úsalo cuando el usuario diga 'quiero un sonido como este'.",
        "params": {"path": "str", "track_index": "int", "clip_index": "int"},
        "optional": ["path", "clip_index"],
        "handler": describe_sound_tool,
    },
    "compare_mix": {
        "description": "Compara tu mezcla contra una referencia: diferencia en dB por banda (sub, bass, low_mid, mid, high_mid, high), loudness (rms_db) y rango dinámico (crest), con consejos de qué subir/bajar en el bus. mix_path y reference_path (rutas), o track_index para la mezcla y reference_path para la referencia. Úsalo para dar feedback objetivo de mezcla.",
        "params": {"mix_path": "str", "reference_path": "str", "mix_track": "int", "mix_clip": "int", "reference_track": "int", "reference_clip": "int"},
        "optional": ["mix_path", "mix_track", "mix_clip", "reference_track", "reference_clip"],
        "handler": compare_mix,
    },
    "set_clip_warping": {
        "description": "Activa/desactiva el warp de un SESSION clip y opcionalmente su modo (warp_mode: 0 Beats, 1 REX, 2 Tones, 3 Texture, 4 Complex, 5 Complex Pro). Úsalo tras cargar audio para que los stems de la misma fuente queden alineados (warp OFF respeta la duración original).",
        "params": {"track_index": "int", "warping": "bool", "warp_mode": "int", "clip_index": "int"},
        "optional": ["warping", "warp_mode", "clip_index"],
        "handler": set_clip_warping,
    },
    "get_clip_info": {
        "description": "Info del archivo fuente y estado de un clip: file_path, is_audio_clip, warping, warp_mode, sample_length, start/end marker, loop bounds, length. view: 'arrangement' (default) o 'session'. Úsalo para saber de dónde viene un clip y su estado de warp.",
        "params": {"track_index": "int", "clip_index": "int", "view": "str"},
        "optional": ["clip_index", "view"],
        "handler": get_clip_info,
    },
    "load_audio_clip": {
        "description": "Carga un archivo de audio en un clip slot (session) de una pista usando el helper M4L y apaga el warp (para que respete su duración original y los stems queden alineados). path: ruta del archivo. Úsalo para poner una grabación/stem en la sesión; para el timeline usa después clip_to_arrangement.",
        "params": {"track_index": "int", "path": "str", "clip_index": "int"},
        "optional": ["path", "clip_index"],
        "handler": load_audio_clip,
    },
    "automate_clip_param": {
        "description": "Escribe automatización de un parámetro de device en un SESSION clip (p. ej. el Gain de un Utility de la voz) vía el envelope del clip. points: string JSON con [{\"time\": beats, \"value\": 0-1}, ...]. time desde el inicio del clip; value 0-1 se normaliza al rango del parámetro. No requiere play. Úsalo para automatizar el Utility Gain (track de la voz, device_index 0, param_index 9) y después clip_to_arrangement para pasarla al timeline.",
        "params": {"track_index": "int", "points": "str", "clip_index": "int", "device_index": "int", "param_index": "int"},
        "optional": ["clip_index", "device_index", "param_index"],
        "handler": automate_clip_param,
    },
    "clear_clip_param": {
        "description": "Borra la automatización de un parámetro de device en un session clip (por defecto Utility Gain: device 0, param 9).",
        "params": {"track_index": "int", "clip_index": "int", "device_index": "int", "param_index": "int"},
        "optional": ["clip_index", "device_index", "param_index"],
        "handler": clear_clip_param,
    },
    "get_clip_param_envelope": {
        "description": "Comprueba si un parámetro de device tiene envelope de automatización en el session clip (has_envelope, points). Úsalo para verificar que la automatización del Utility Gain quedó escrita.",
        "params": {"track_index": "int", "clip_index": "int", "device_index": "int", "param_index": "int"},
        "optional": ["clip_index", "device_index", "param_index"],
        "handler": get_clip_param_envelope,
    },
    "clip_to_arrangement": {
        "description": "Copia un SESSION clip (con su automatización) al timeline del Arrangement en una posición en beats. No requiere play: Live 12 convierte la automatización del clip en track-based. Ej: pista de la voz (track_index 6), clip slot 0, start_time en pulsos.",
        "params": {"track_index": "int", "start_time": "float", "clip_index": "int"},
        "optional": ["clip_index"],
        "handler": clip_to_arrangement,
    },
    "list_arrangement_clips": {
        "description": "Lista los clips del Arrangement de una pista: nombre, longitud (beats) y posición de inicio de cada uno. Úsalo para saber cuántos clips viejos hay y cuál borrar antes de clip_to_arrangement.",
        "params": {"track_index": "int"},
        "handler": list_arrangement_clips,
    },
    "delete_arrangement_clip": {
        "description": "Borra un clip del Arrangement por su índice en track.arrangement_clips (0-based). Para reemplazar la voz en el timeline: borra el clip viejo (arr_clip_index 0) y después clip_to_arrangement con el session clip.",
        "params": {"track_index": "int", "arr_clip_index": "int"},
        "optional": ["arr_clip_index"],
        "handler": delete_arrangement_clip,
    },
    "browser_list": {
        "description": "Lista lo que hay disponible en el navegador de Ableton por categoría (plugins, instruments, drums, samples, packs, user_library...). Acepta query (substring, opcional) para buscar un plugin concreto sin descargar toda la lista y max_items (default 20). Úsalo para descubrir si un plugin/pack está instalado (p. ej. browser_list(category='plugins', query='magenta')) antes de intentar cargarlo.",
        "params": {"category": "str", "max_items": "int", "query": "str"},
        "optional": ["category", "max_items", "query"],
        "handler": browser_list,
    },
    "browser_load": {
        "description": "Carga desde el navegador de Ableton el primer elemento cuyo nombre contenga query y lo inserta en la pista indicada. Más fiable que insert_device para plugins/packs de terceros (Magenta Studio, Kontakt, etc.).",
        "params": {"track_index": "int", "query": "str"},
        "handler": browser_load,
    },
    "set_follow_action": {
        "description": "Programa la transición automática A→B: cuando termina de sonar un clip, salta solo a la parte siguiente. action: 0 none, 1 stop, 2 play again, 3 next, 4 previous, 5 first, 6 last. time (opcional): follow_action_time en pulsos (duración de la voz en la parte A). index (opcional): destino relativo para next/previous. No requiere play. Úsalo para que la parte A salte sola a la parte B al terminar la voz.",
        "params": {"track_index": "int", "clip_index": "int", "action": "int", "time": "float", "index": "int"},
        "optional": ["time", "index"],
        "handler": set_follow_action,
    },
}


# ---------------------------------------------------------------------------
# MCP por stdio: JSON-RPC 2.0 con framing Content-Length
# ---------------------------------------------------------------------------


def _schema_for(params, optional=()):
    types = {"int": "integer", "float": "number", "str": "string", "bool": "boolean"}
    properties = {}
    required = []
    for name, ptype in params.items():
        properties[name] = {"type": types.get(ptype, "string")}
        if name not in optional:
            required.append(name)
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _read_message():
    """Lee un mensaje MCP del stdin (framing Content-Length)."""
    content_length = None
    while True:
        try:
            line = sys.stdin.buffer.readline()
        except (OSError, ValueError):
            return None  # pipe broken
        if not line:
            return None  # EOF
        line = line.strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":")[1].strip())
    if content_length is None:
        return None
    body = sys.stdin.buffer.read(content_length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def _send_message(payload):
    body = json.dumps(payload).encode("utf-8")
    try:
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body)
        sys.stdout.buffer.flush()
    except (OSError, ValueError):
        pass  # pipe broken, parent died


def _handle_request(msg):
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        requested_version = (msg.get("params") or {}).get("protocolVersion", "2024-11-05")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ableton-assistant-bridge", "version": "0.1.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        tools = [
            {
                "name": name,
                "description": spec["description"],
                "inputSchema": _schema_for(spec["params"], spec.get("optional", ())),
            }
            for name, spec in TOOLS.items()
        ]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {}) or {}
        spec = TOOLS.get(name)
        if spec is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": f"Tool desconocido: {name}"},
            }
        try:
            text = spec["handler"](arguments)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                },
            }
    if method == "notifications/initialized":
        return None
    if method.startswith("notifications/") or method.startswith("$/"):
        return None
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Método no soportado: {method}"},
    }


def main():
    threading.Thread(target=_tempo_server, daemon=True, name="tempo-server").start()
    # Detect VocalAssistant MIDI Remote Script (restore last active instance)
    if MIDI_REMOTE_AVAILABLE:
        _apply_persisted_active_instance()
        _detect_midi_remote()
    try:
        ableton.query("/live/test", timeout=2.0)
        print("[bridge] AbletonOSC conectado", file=sys.stderr, flush=True)
    except Exception:
        print(
            "[bridge] AVISO: Ableton no responde (¿Live abierto con AbletonOSC activo en Preferencias > Link/Tempo/MIDI?)",
            file=sys.stderr,
            flush=True,
        )
        # If MIDI Remote Script is available, OSC fallback is less critical
        if _midi_remote_ok():
            print(
                "[bridge] VocalAssistant MIDI Remote Script está disponible como alternativa",
                file=sys.stderr,
                flush=True,
            )
    debug = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_debug.log"), "a")
    debug.write("=== bridge iniciado ===\n")
    debug.flush()
    while True:
        msg = _read_message()
        debug.write(">> " + json.dumps(msg)[:500] + "\n")
        debug.flush()
        if msg is None:
            break
        try:
            if "id" in msg and "method" in msg:
                reply = _handle_request(msg)
                if reply is not None:
                    debug.write("<< " + json.dumps(reply)[:500] + "\n")
                    debug.flush()
                    _send_message(reply)
        except Exception as exc:
            debug.write(f"!! error: {exc}\n")
            debug.flush()
    debug.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        ableton.close()
