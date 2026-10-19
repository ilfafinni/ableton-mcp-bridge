#!/usr/bin/env python3
"""
Servidor de audio para el asistente de Ableton (HTTP local, puerto 8787).

Endpoints:
  GET  /health                  -> {"ok": true, "bridge": bool, "ableton": bool, "tempo": float|null}
  POST /stt                     -> {"text": "..."}       (voz -> texto, Whisper)
  POST /midi                    -> {"notes": [...], "bpm": N} (canto -> notas MIDI)
  POST /midi?mode=rhythm        -> notas de percusión (kick) a partir de un
                                   ritmo tarareado/golpeado, cuantizado al compás

El body de POST es el audio crudo (webm/opus o wav, mono o estéreo).

Transcripción a MIDI: usa Basic Pitch (Spotify) si está instalado
(pip install basic-pitch), y si no, cae a librosa (pyin) como fallback.
Las notas se cuantizan a corcheas (16th) usando el tempo actual de la sesión
de Ableton, que se pide al bridge MCP (UDP 127.0.0.1:9003) — así no hay
conflicto de puertos con el OSC de AbletonOSC.

Dependencias:  pip install faster-whisper av
               pip install basic-pitch        (recomendado para /midi)
               pip install librosa            (fallback de /midi y /midi?mode=rhythm)

Ejecución (la lanza la app Electron automáticamente):
  python python/audio_server.py
"""

import io
import json
import os
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("VOCAL_AUDIO_PORT", 8787))
BRIDGE_TEMPO_PORT = int(os.environ.get("BRIDGE_TEMPO_PORT", 9003))
# El bridge tarda ~2 s en responder si Ableton está caído (timeout de su query).
BRIDGE_TEMPO_TIMEOUT = 3.5
LOCAL_TOKEN = os.environ.get("ABLETON_MCP_TOKEN", "")
LOCAL_TOKEN_HEADER = "x-ableton-token"

STT_MODEL = os.environ.get("STT_MODEL", "small")

_whisper_model = None
_whisper_lock = threading.Lock()
_basic_pitch = None
_librosa = None
RHYTHM_PITCH = 36  # C1: kick en un DrumRack


def get_live_tempo():
    """Pide el tempo actual al bridge MCP (UDP 9003). Devuelve None si no hay."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(BRIDGE_TEMPO_TIMEOUT)
        s.sendto(b"tempo", ("127.0.0.1", BRIDGE_TEMPO_PORT))
        data, _ = s.recvfrom(1024)
        s.close()
        if data.strip():
            return float(data)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Escucha del master de Ableton: WASAPI loopback (nativo de Windows).
# Captura el audio de la salida por defecto (por donde sale Ableton)
# sin necesidad de reenrutar con Voicemeeter.
# ---------------------------------------------------------------------------

LISTEN_SECONDS = 2.0
LISTEN_RATE = 44100


def capture_loopback(duration=LISTEN_SECONDS, rate=LISTEN_RATE):
    """Captura el loopback de la salida por defecto (WASAPI). Devuelve
    (audio float32 mono 1-D, rate). Levanta RuntimeError si falla."""
    import numpy as np
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    try:
        dev = p.get_default_wasapi_loopback()
        stream = p.open(
            format=pyaudio.paInt16,
            channels=2,
            rate=int(dev["defaultSampleRate"]),
            input=True,
            output=True,
            input_device_index=dev["index"],
            output_device_index=dev["index"],
            frames_per_buffer=1024,
        )
        frames = []
        t0 = time.time()
        while time.time() - t0 < duration:
            data = stream.read(1024, exception_on_overflow=False)
            frames.append(np.frombuffer(data, dtype=np.int16))
        stream.stop_stream()
        stream.close()
    finally:
        p.terminate()
    if not frames:
        raise RuntimeError("no se capturó audio de la salida")
    sig = np.concatenate(frames).astype(np.float32) / 32768.0
    sig = sig.reshape(-1, 2).mean(axis=1)
    return sig, int(dev["defaultSampleRate"])


def _find_interface_loopback(p, min_channels=6):
    """Busca una interfaz de audio con >= min_channels de entrada. En
    interfaces con loopback por hardware (p. ej. Focusrite Scarlett 3ª gen),
    los últimos 2 canales reflejan la salida (DAW 5-6). Devuelve el índice
    del dispositivo y el número de canales, o (None, None)."""
    best = None
    for i in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(i)
        except Exception:
            continue
        ch = info.get("maxInputChannels") or 0
        if ch >= min_channels:
            if best is None or ch > best[1]:
                best = (i, ch)
    return best


def capture_interface_loopback(duration=LISTEN_SECONDS, rate=LISTEN_RATE):
    """Captura la salida de la interfaz de audio vía loopback por hardware
    (canales 5-6 en Scarlett 3ª gen). Devuelve (audio mono float32, rate).
    Esto captura lo que Ableton manda por ASIO sin reenrutar nada."""
    import numpy as np
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    try:
        idx, ch = _find_interface_loopback(p)
        if idx is None:
            raise RuntimeError("no hay interfaz con loopback por hardware (>=6 canales)")
        dev_rate = int(p.get_device_info_by_index(idx)["defaultSampleRate"] or rate)
        stream = p.open(
            format=pyaudio.paInt16,
            channels=ch,
            rate=dev_rate,
            input=True,
            output=False,
            input_device_index=idx,
            frames_per_buffer=1024,
        )
        frames = []
        t0 = time.time()
        while time.time() - t0 < duration:
            data = stream.read(1024, exception_on_overflow=False)
            frames.append(np.frombuffer(data, dtype=np.int16))
        stream.stop_stream()
        stream.close()
    finally:
        p.terminate()
    if not frames:
        raise RuntimeError("no se capturó audio del loopback de la interfaz")
    sig = np.concatenate(frames).astype(np.float32) / 32768.0
    sig = sig.reshape(-1, ch)
    loop = sig[:, ch - 2 :]  # últimos 2 canales = loopback por hardware
    return loop.mean(axis=1), dev_rate


def analyze_master(audio, rate):
    """Análisis de una señal mono: nivel global, pico, espectro por bandas,
    frecuencia dominante, detección de golpe (kick) y si hay señal real
    (no silencio). Devuelve además la ventana más fuerte para localizar
    dónde está el audio cuando hay pasajes en silencio."""
    import numpy as np

    n = len(audio)
    if n == 0:
        return {"error": "sin audio"}
    rms = float(np.sqrt((audio ** 2).mean()))
    peak = float(np.abs(audio).max())
    rms_db = float(20 * np.log10(rms + 1e-9))
    signal = rms > 0.004  # ~ por debajo de -48 dBFS se considera silencio
    loudest_db = None
    loudest_at = 0.0
    if n > 4096:
        hop = 1024
        win = 4096
        num = (n - win) // hop + 1
        if num > 1:
            rms_win = []
            for i in range(num):
                seg = audio[i * hop : i * hop + win]
                rms_win.append(float(np.sqrt((seg ** 2).mean())))
            rms_win = np.array(rms_win)
            loudest_db = float(20 * np.log10(rms_win.max() + 1e-9))
            loudest_at = float(np.argmax(rms_win) * hop / rate)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    mag = np.abs(np.fft.rfft(audio * np.hanning(n)))
    bands = {
        "sub": (20, 60),
        "bass": (60, 200),
        "low_mid": (200, 500),
        "mid": (500, 2000),
        "high_mid": (2000, 6000),
        "high": (6000, rate / 2),
    }
    band_energy = {}
    band_db = {}
    for name, (lo, hi) in bands.items():
        mask = (freqs >= lo) & (freqs < hi)
        if mask.any():
            v = float(np.sqrt((mag[mask] ** 2).mean()) / n)
            band_energy[name] = round(v, 6)
            band_db[name] = round(20 * np.log10(v + 1e-9), 1)
        else:
            band_energy[name] = 0.0
            band_db[name] = -180.0
    if mag.size:
        dom = int(freqs[int(np.argmax(mag))])
    else:
        dom = 0
    onset = 0.0
    if n > 4096:
        try:
            import librosa

            onset_frames = librosa.onset.onset_detect(y=audio, sr=rate, hop_length=512)
            onset_times = librosa.frames_to_time(onset_frames, sr=rate, hop_length=512)
            if onset_times.size:
                onset = float(onset_times[0])
        except Exception:
            pass
    return {
        "rms": round(rms, 4),
        "peak": round(peak, 4),
        "rms_db": round(rms_db, 1),
        "signal": signal,
        "loudest_region_db": loudest_db,
        "loudest_region_s": round(loudest_at, 3),
        "dominant_hz": dom,
        "first_onset_s": round(onset, 3),
        "bands": band_energy,
        "bands_db": band_db,
    }


def analyze_audio_file(path, rate=44100):
    """Analiza un archivo de audio en disco (WAV/AIFF, cualquier formato que
    soporte librosa/soundfile) con la misma función que el loopback, pero SIN
    reproducir nada. Devuelve el análisis de analyze_master o un dict con
    error."""
    import librosa
    import os

    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"archivo no encontrado: {path}"}
    try:
        audio, sr = librosa.load(path, sr=rate, mono=True)
    except Exception as exc:
        return {"ok": False, "error": f"no se pudo leer el audio: {exc}"}
    analysis = analyze_master(audio, sr)
    return {"ok": True, "file": os.path.basename(path), "path": path,
            "duration_s": round(len(audio) / float(sr), 3), "rate": sr, **analysis}


# ---------------------------------------------------------------------------
# Análisis musical (key, tempo, compás, acordes, groove) — todo con librosa.
# ---------------------------------------------------------------------------

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Perfiles de Krumhansl-Schmuckler para mayor/menor (detección de tonalidad).
KSP_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KSP_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def _chroma(audio, sr):
    import librosa
    import numpy as np
    c = librosa.feature.chroma_cqt(y=audio, sr=sr, bins_per_octave=36)
    return c.mean(axis=1)


def detect_key(audio, sr):
    """Detecta tonalidad (C, G#, F#m...) y tempo (BPM) de una señal mono."""
    import librosa
    import numpy as np

    chroma = _chroma(audio, sr)
    if chroma is None or not np.any(chroma):
        return {"key": None, "mode": None, "key_confidence": 0.0}
    best = None
    best_score = -1.0
    for tonic in range(12):
        for mode, profile in (("major", KSP_MAJOR), ("minor", KSP_MINOR)):
            corr = float(np.corrcoef(chroma, profile)[0, 1]) if np.std(profile) > 0 else 0.0
            score = corr
            if score > best_score:
                best_score = score
                best = (tonic, mode)
    key_name = NOTE_NAMES[best[0]]
    if best[1] == "minor":
        key_name += "m"
    try:
        tempo, _ = librosa.beat.beat_track(y=audio, sr=sr)
        import numpy as np
        if tempo is not None and len(audio) > 0:
            tempo = float(np.atleast_1d(tempo)[0])
        else:
            tempo = None
    except Exception:
        tempo = None
    return {
        "key": key_name,
        "mode": best[1],
        "key_confidence": round(max(0.0, min(1.0, best_score)), 3),
        "tempo": round(tempo, 2) if tempo else None,
        "time_signature": _guess_time_signature(audio, sr, tempo) if tempo else None,
    }


def _guess_time_signature(audio, sr, tempo):
    """Estima el compás (4/4, 3/4...) midiendo la periodicidad de los onsets
    respecto al pulso. Devuelve una cadena o None si no hay suficiente señal."""
    import librosa
    import numpy as np

    if len(audio) < sr:  # < 1 s: no hay forma
        return None
    try:
        onset_env = librosa.onset.onset_strength(y=audio, sr=sr)
        pulses = librosa.util.peak_pick(onset_env, pre_max=1, post_max=1,
                                        pre_avg=3, post_avg=3, delta=0.5, wait=3)
        if len(pulses) < 4:
            return None
        beat_frames = librosa.frames_to_samples(pulses, hop_length=512)
        beat_dur_s = 60.0 / tempo
        beat_samps = int(beat_dur_s * sr)
        # Acumula el histograma de las distancias entre onsets moduladas al beat.
        hist = np.zeros(8)
        times = librosa.frames_to_time(pulses, sr=sr, hop_length=512)
        for i in range(1, len(times)):
            d = times[i] - times[i - 1]
            beats = d / beat_dur_s
            for k in range(1, 9):
                if abs(beats - k) < 0.5:
                    hist[k - 1] += 1
        total = hist.sum()
        if total < 2:
            return None
        prob = hist / total
        # 4/4 es lo más común; 3/4 tiene picos en el 3.
        score44 = prob[0] + prob[3] + prob[5] + prob[7]
        score34 = prob[2] + prob[5]
        if score34 > score44 + 0.15:
            return "3/4"
        if score44 > 0.3:
            return "4/4"
        return "4/4"
    except Exception:
        return None


def detect_chords(audio, sr, tempo=None, beats_per_bar=4, start_bar=1, max_seconds=240.0):
    """Detecta el acorde que suena en cada barra del proyecto (grid del caller).
    Devuelve una lista de {bar, chord, energy}."""
    import librosa
    import numpy as np

    if tempo is None or tempo <= 0:
        est = detect_key(audio, sr)
        tempo = est.get("tempo") or 120.0
    beat_s = 60.0 / tempo
    bar_s = beat_s * beats_per_bar
    n_bars = int(min(len(audio) / float(sr), max_seconds) / bar_s)
    if n_bars < 1:
        n_bars = 1
    hop = 512
    chroma_full = librosa.feature.chroma_cqt(y=audio, sr=sr, hop_length=hop, bins_per_octave=36)
    times = librosa.times_like(chroma_full, sr=sr, hop_length=hop)
    results = []
    for i in range(n_bars):
        t0 = (i + start_bar - 1) * bar_s
        t1 = t0 + bar_s
        mask = (times >= t0) & (times < t1)
        if not mask.any():
            results.append({"bar": i + start_bar, "chord": None, "energy": 0.0})
            continue
        chroma_bar = chroma_full[:, mask].mean(axis=1)
        energy = float(chroma_bar.max())
        if energy < 0.05:
            results.append({"bar": i + start_bar, "chord": None, "energy": round(energy, 3)})
            continue
        chord = _classify_chord(chroma_bar)
        results.append({"bar": i + start_bar, "chord": chord, "energy": round(energy, 3)})
    return results


def _classify_chord(chroma):
    """Clasifica un chroma vector en acorde (C, Fm, Gsus4, Am...)."""
    import numpy as np

    if np.max(chroma) <= 0:
        return None
    # Normaliza y suaviza (redondea las notas débiles).
    ch = chroma / (np.max(chroma) + 1e-9)
    ch = np.where(ch < 0.25, 0.0, ch)
    best = None
    best_score = -1.0
    for tonic in range(12):
        for kind, template in (
            ("", [0, 4, 7]),
            ("m", [0, 3, 7]),
            ("sus4", [0, 5, 7]),
            ("7", [0, 4, 7, 10]),
            ("m7", [0, 3, 7, 10]),
            ("maj7", [0, 4, 7, 11]),
            ("dim", [0, 3, 6]),
            ("aug", [0, 4, 8]),
        ):
            score = 0.0
            for offset in template:
                score += ch[(tonic + offset) % 12]
            # Penaliza notas fuera del acorde (consonancia).
            outside = [n for n in range(12) if (n - tonic) % 12 not in template]
            score -= 0.5 * sum(ch[n] for n in outside)
            if score > best_score:
                best_score = score
                best = (tonic, kind)
    return NOTE_NAMES[best[0]] + best[1]


def detect_groove(audio, sr, tempo=None, beats_per_bar=4, time_scale=1.0):
    """Mide dónde caen los onsets respecto al grid del proyecto. Devuelve
    los offsets medios (en beats y ms) y cuántos golpes caen fuera de tiempo."""
    import librosa
    import numpy as np

    if tempo is None or tempo <= 0:
        est = detect_key(audio, sr)
        tempo = est.get("tempo") or 120.0
    tempo = tempo * time_scale
    onset_env = librosa.onset.onset_strength(y=audio, sr=sr)
    pulses = librosa.util.peak_pick(onset_env, pre_max=1, post_max=1,
                                    pre_avg=3, post_avg=3, delta=0.4, wait=5)
    if len(pulses) < 4:
        return {"ok": False, "error": "no se detectaron suficientes onsets", "count": int(len(pulses))}
    times = librosa.frames_to_time(pulses, sr=sr, hop_length=512)
    beat_s = 60.0 / tempo
    offsets = []
    for t in times:
        beat_pos = (t / beat_s) % 1.0
        # dista mínima al beat más cercano (0 o 1).
        off = min(beat_pos, 1.0 - beat_pos)
        offsets.append(off)
    arr = np.array(offsets)
    mean_off = float(np.mean(arr))
    on_time = float(np.mean(arr < 0.1))
    swing = _detect_swing(arr, tempo, times)
    return {
        "ok": True,
        "count": int(len(arr)),
        "mean_offset_beats": round(mean_off, 4),
        "mean_offset_ms": round(mean_off * beat_s * 1000, 1),
        "pct_on_time": round(on_time * 100, 1),
        "swing_ratio": swing,
        "note": (
            "sólido" if mean_off < 0.06
            else "ligeramente fuera" if mean_off < 0.12
            else "muy fuera del grid"
        ),
    }


def _detect_swing(offsets, tempo, times):
    """Estima el swing: si los golpes que caen en el 'y' (mitad del beat) se
    retrasan ~2/3, hay swing. Devuelve la proporción o None."""
    import numpy as np

    if len(offsets) < 8:
        return None
    # Golpes en posiciones de corchea (0.0, 0.5): mira la distancia al 0.5.
    off = np.array(offsets)
    late = off[(off > 0.15) & (off < 0.45)]
    if len(late) < 2:
        return None
    return round(float(np.mean(late)) * 2, 2)


def decode_audio(data: bytes, sample_rate=22050):
    """Decodifica webm/wav a float32 mono usando PyAV (dependencia de faster-whisper)."""
    import av
    import numpy as np

    container = av.open(io.BytesIO(data))
    frames = []
    for frame in container.decode(audio=0):
        frame = frame.resample(format="fltp", layout="mono", rate=sample_rate)
        frames.append(frame.to_ndarray())
    if not frames:
        raise ValueError("No hay audio decodificable en el archivo")
    audio = np.concatenate(frames, axis=1)[0].astype(np.float32)
    return audio


def whisper_transcribe(data: bytes) -> str:
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel

            _whisper_model = WhisperModel(STT_MODEL, device="cpu", compute_type="int8")
    segments, _ = _whisper_model.transcribe(
        io.BytesIO(data), beam_size=1, vad_filter=True
    )
    return " ".join(s.text.strip() for s in segments).strip()


def _preload_whisper():
    """Carga el modelo de transcripción en background al arrancar,
    para que el primer uso de voz no tenga que descargarlo ni esperarlo."""
    global _whisper_model
    try:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel

                _whisper_model = WhisperModel(STT_MODEL, device="cpu", compute_type="int8")
        print("[audio] modelo de voz cargado", flush=True)
    except Exception as exc:
        print(f"[audio] aviso: no se pudo precargar modelo de voz: {exc}", flush=True)


def quantize(notes, bpm, grid=0.25):
    """Cuantiza notas a la rejilla (grid en beats) usando el BPM dado."""
    beat = 60.0 / bpm if bpm and bpm > 0 else 0.5
    result = []
    for pitch, start_s, end_s, vel in notes:
        start_b = start_s / beat
        dur_b = max((end_s - start_s) / beat, grid * 0.9)
        start_q = round(start_b / grid) * grid
        dur_q = max(round((start_b + dur_b) / grid) * grid - start_q, grid)
        if start_q < 0:
            dur_q += start_q
            start_q = 0.0
        if dur_q <= 0:
            continue
        result.append({
            "pitch": int(round(pitch)),
            "start_time": round(start_q, 3),
            "duration": round(dur_q, 3),
            "velocity": max(1, min(127, int(round(vel)))),
        })
    return result


def transcribe_midi_basicpitch(data: bytes, bpm):
    """Basic Pitch (Spotify): detección de notas polifónica ligera."""
    global _basic_pitch
    import numpy as np

    audio = decode_audio(data, sample_rate=22050)
    if _basic_pitch is None:
        from basic_pitch import ICASSP_2022_MODEL_PATH
        from basic_pitch.inference import predict as bp_predict

        _basic_pitch = (bp_predict, ICASSP_2022_MODEL_PATH)
    predict, model_path = _basic_pitch
    import wave
    import os

    tmp = os.path.join(tempfile.gettempdir(), "bp_mic.wav")
    pcm = np.clip(audio, -1.0, 1.0)
    with wave.open(tmp, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes((pcm * 32767.0).astype(np.int16).tobytes())
    _, _, note_events = predict(tmp, model_path)
    notes = [
        (n[2], n[0], n[1], max(30, min(110, int(round(n[3] * 120)))))
        for n in note_events
    ]
    return quantize(notes, bpm)


def transcribe_midi_librosa(data: bytes, bpm):
    """Fallback con librosa (pyin): pitch + segmentos de voz."""
    global _librosa
    import numpy as np

    audio = decode_audio(data, sample_rate=22050)
    if _librosa is None:
        import librosa

        _librosa = librosa
    librosa = _librosa
    f0, voiced, _ = librosa.pyin(
        audio, fmin=80, fmax=1200, sr=22050, frame_length=2048
    )
    times = librosa.times_like(f0, sr=22050)
    rms = librosa.feature.rms(y=audio, frame_length=2048, hop_length=512)[0]
    rms_times = librosa.times_like(rms, sr=22050, hop_length=512)

    def rms_at(t):
        idx = int(t / (512 / 22050)) if rms_times[-1] > 0 else 0
        return float(rms[min(idx, len(rms) - 1)])

    notes = []
    i = 0
    min_gap = 0.08
    min_len = 0.08
    n = len(f0)
    while i < n:
        if not voiced[i]:
            i += 1
            continue
        start = i
        while i < n and (voiced[i] or (times[i] - times[max(start, i - 1)] < min_gap and voiced[min(i + 1, n - 1)])):
            i += 1
        if not voiced[min(i - 1, n - 1)]:
            i += 1
        end = i
        if end - start < 2:
            continue
        seg = f0[start:end]
        valid = seg[~np.isnan(seg)]
        if len(valid) < 2:
            continue
        pitch_hz = np.median(valid)
        if pitch_hz < 60:
            continue
        midi = 69 + 12 * np.log2(pitch_hz / 440.0)
        t0, t1 = times[start], times[end - 1]
        if t1 - t0 < min_len:
            continue
        amp = rms_at((t0 + t1) / 2)
        vel = max(30, min(110, int(round(amp * 400))))
        notes.append((float(midi), float(t0), float(t1), vel))
    return quantize(notes, bpm)


def transcribe_midi(data: bytes):
    bpm = get_live_tempo() or 120.0
    try:
        notes = transcribe_midi_basicpitch(data, bpm)
        return notes, "basic-pitch", bpm
    except ImportError:
        pass
    except Exception:
        pass
    try:
        notes = transcribe_midi_librosa(data, bpm)
        return notes, "librosa", bpm
    except ImportError:
        return None, "ninguno (pip install basic-pitch o librosa)", bpm


def transcribe_rhythm(data: bytes, bpm, grid=0.25):
    """Detecta los golpes (onsets) del audio y los convierte en notas de kick
    (pitch 36, C1) cuantizadas a la rejilla. Para tararear o golpear un ritmo."""
    import numpy as np
    import librosa

    audio = decode_audio(data, sample_rate=22050)
    onset_frames = librosa.onset.onset_detect(y=audio, sr=22050, hop_length=512)
    onset_times = librosa.frames_to_time(onset_frames, sr=22050, hop_length=512)
    rms = librosa.feature.rms(y=audio, frame_length=2048, hop_length=512)[0]
    rms_times = librosa.times_like(rms, sr=22050, hop_length=512)

    def vel_at(t):
        idx = int(t / (512 / 22050)) if rms_times[-1] > 0 else 0
        return max(30, min(110, int(round(float(rms[min(idx, len(rms) - 1)]) * 400))))

    beat = 60.0 / bpm if bpm and bpm > 0 else 0.5
    notes = []
    for t in onset_times:
        start_q = round(t / beat / grid) * grid
        if start_q < 0:
            continue
        if notes and notes[-1]["start_time"] == start_q:
            continue
        notes.append({
            "pitch": RHYTHM_PITCH,
            "start_time": round(start_q, 3),
            "duration": grid,
            "velocity": vel_at(t),
        })
    return notes


def transcribe_file(path, rate=44100, grid=0.25):
    """Transcribe un archivo de audio completo (no el micrófono) a notas MIDI
    usando Basic Pitch (polifónico). Si no está instalado, cae a librosa
    (monofónico). Cuantiza al tempo de la sesión. Devuelve la lista de notas
    o un dict con error."""
    import librosa
    import numpy as np
    import os

    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"archivo no encontrado: {path}"}
    audio, sr = librosa.load(path, sr=rate, mono=True)
    bpm = get_live_tempo() or 120.0
    try:
        from basic_pitch import ICASSP_2022_MODEL_PATH
        from basic_pitch.inference import predict as bp_predict

        import numpy as np
        import wave
        import struct as _struct

        # basic-pitch espera una ruta o un array float32 1-D; con el float32
        # directo falla, así que escribimos un WAV temporal.
        tmp = os.path.join(tempfile.gettempdir(), "bp_input.wav")
        pcm = np.clip(audio, -1.0, 1.0)
        with wave.open(tmp, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(sr))
            w.writeframes((pcm * 32767.0).astype(np.int16).tobytes())
        _, _, note_events = bp_predict(tmp, ICASSP_2022_MODEL_PATH)
        raw = [
            (n[2], n[0], n[1], max(30, min(110, int(round(n[3] * 120)))))
            for n in note_events
        ]
        return {"ok": True, "notes": quantize(raw, bpm, grid), "count": len(note_events),
                "backend": "basic-pitch", "bpm": bpm}
    except ImportError:
        pass
    except Exception as exc:
        return {"ok": False, "error": f"basic-pitch falló: {exc}", "fix": "pip install basic-pitch"}
    # Fallback librosa (monofónico, como /midi).
    f0, voiced, _ = librosa.pyin(audio, fmin=80, fmax=1200, sr=sr, frame_length=2048)
    times = librosa.times_like(f0, sr=sr)
    rms = librosa.feature.rms(y=audio, frame_length=2048, hop_length=512)[0]
    rms_times = librosa.times_like(rms, sr=sr, hop_length=512)

    def rms_at(t):
        idx = int(t / (512 / sr)) if rms_times[-1] > 0 else 0
        return float(rms[min(idx, len(rms) - 1)])

    notes = []
    i = 0
    min_gap = 0.08
    min_len = 0.08
    n = len(f0)
    while i < n:
        if not voiced[i]:
            i += 1
            continue
        start = i
        while i < n and (voiced[i] or (times[i] - times[max(start, i - 1)] < min_gap and voiced[min(i + 1, n - 1)])):
            i += 1
        if not voiced[min(i - 1, n - 1)]:
            i += 1
        end = i
        if end - start < 2:
            continue
        seg = f0[start:end]
        valid = seg[~np.isnan(seg)]
        if len(valid) < 2:
            continue
        pitch_hz = np.median(valid)
        if pitch_hz < 60:
            continue
        midi = 69 + 12 * np.log2(pitch_hz / 440.0)
        t0, t1 = times[start], times[end - 1]
        if t1 - t0 < min_len:
            continue
        amp = rms_at((t0 + t1) / 2)
        vel = max(30, min(110, int(round(amp * 400))))
        notes.append((float(midi), float(t0), float(t1), vel))
    q = quantize(notes, bpm, grid)
    return {"ok": True, "notes": q, "count": len(q),
            "backend": "librosa", "bpm": bpm}


STEMS_CACHE_DIR = os.path.join(tempfile.gettempdir(), "vocal_stems")


def separate_stems(path, quality="fast", probe=False):
    """Separa un archivo en stems (drums, bass, vocals, other) con Demucs.
    Con probe=True solo comprueba si ya hay stems cacheados (no arranca Demucs).
    Los stems se guardan en el temp dir para que el bridge los pueda cargar."""
    import os

    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"archivo no encontrado: {path}"}
    base = os.path.splitext(os.path.basename(path))[0]
    out_dir = os.path.join(STEMS_CACHE_DIR, base)
    stems = ["drums", "bass", "vocals", "other"]
    existing = {}
    if os.path.isdir(out_dir):
        for s in stems:
            f = os.path.join(out_dir, f"{s}.wav")
            if os.path.exists(f):
                existing[s] = f
    if probe:
        return {"ok": True, "probe": True, "cached": bool(existing), "stems": existing or None}
    if existing and len(existing) == len(stems):
        return {"ok": True, "cached": True, "out_dir": out_dir, "stems": existing}
    try:
        from demucs.separate import main as demucs_main
        import sys
    except ImportError:
        return {"ok": False, "error": "demucs no está instalado", "fix": "pip install demucs"}
    os.makedirs(out_dir, exist_ok=True)
    try:
        sys.argv = [
            "demucs", "-n", "htdemucs", "--two-stems", "vocals",
            "-o", out_dir, "--filename", "{stem}.wav", path,
        ]
        demucs_main()
    except Exception as exc:
        return {"ok": False, "error": f"demucs falló: {exc}"}
    result_stems = {}
    for s in stems:
        f = os.path.join(out_dir, f"{s}.wav")
        if os.path.exists(f):
            result_stems[s] = f
    return {"ok": True, "cached": False, "out_dir": out_dir, "stems": result_stems}


def describe_sound(path):
    """Describe el timbre de un archivo: tags compactos + queries de búsqueda
    del browser de Ableton para encontrar sonidos parecidos."""
    import librosa
    import numpy as np
    import os

    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"archivo no encontrado: {path}"}
    audio, sr = librosa.load(path, sr=44100, mono=True)
    tags = []
    # Espectral: centroide -> brillo.
    spec = librosa.feature.spectral_centroid(y=audio, sr=sr)[0]
    centroid_hz = float(np.mean(spec))
    if centroid_hz < 800:
        tags.append("dark")
    elif centroid_hz < 2200:
        tags.append("warm")
    elif centroid_hz < 5000:
        tags.append("bright")
    else:
        tags.append("harsh-bright")
    rolloff = librosa.feature.spectral_rolloff(y=audio, sr=sr)[0]
    if float(np.mean(rolloff)) < 4000:
        tags.append("mellow")
    # RMS -> sostenido vs percusivo.
    rms = librosa.feature.rms(y=audio)[0]
    rms_mean = float(np.mean(rms))
    if rms_mean < 0.01:
        tags.append("soft")
    elif rms_mean > 0.1:
        tags.append("loud")
    # Ataque (onset density) -> percusivo/pad.
    onsets = librosa.onset.onset_detect(y=audio, sr=sr, hop_length=512)
    density = len(onsets) / (len(audio) / sr) if len(audio) > 0 else 0
    if density > 4:
        tags.append("rhythmic")
    elif density < 0.5:
        tags.append("sustained")
    # Croma -> brillo armónico (acordes).
    chroma = _chroma(audio, sr)
    has_harmony = bool(np.max(chroma) > 0.2)
    if has_harmony:
        tags.append("harmonic")
    key = detect_key(audio, sr)
    queries = [
        f"{'warm' if 'warm' in tags else 'bright'} {'pad' if 'sustained' in tags else 'pluck'}",
        ("sustained" if "sustained" in tags else "rhythmic"),
    ]
    return {
        "ok": True,
        "tags": tags,
        "key": key.get("key"),
        "browser_queries": queries,
        "centroid_hz": round(centroid_hz, 1),
        "onset_density": round(density, 2),
    }


def compose_accompaniment(tempo, beats_per_bar=4, start_bar=1, bars=8, progression=None,
                          vocal_gap=True):
    """Genera un acompañamiento (bajo + acordes) a partir de una progresión de
    acordes y lo renderiza a .mid + .notes.json. progression: lista de
    {chord: "Am", from_bar, to_bar}. Devuelve la ruta del .mid."""
    import struct
    import os

    if not progression:
        progression = [
            {"chord": "Am", "from_bar": start_bar, "to_bar": start_bar + 1},
            {"chord": "F", "from_bar": start_bar + 2, "to_bar": start_bar + 3},
            {"chord": "C", "from_bar": start_bar + 4, "to_bar": start_bar + 5},
            {"chord": "G", "from_bar": start_bar + 6, "to_bar": start_bar + 7},
        ]
    notes = _progression_to_notes(progression, tempo, beats_per_bar, start_bar, bars)
    out_dir = os.path.join(tempfile.gettempdir(), "vocal_compose")
    os.makedirs(out_dir, exist_ok=True)
    midi_path = os.path.join(out_dir, "accompaniment.mid")
    notes_path = os.path.splitext(midi_path)[0] + ".notes.json"
    _write_simple_midi(midi_path, notes, tempo)
    with open(notes_path, "w", encoding="utf-8") as f:
        json.dump({"tempo": tempo, "beats_per_bar": beats_per_bar,
                   "start_bar": start_bar, "notes": notes}, f, ensure_ascii=False)
    return {"ok": True, "midi_path": midi_path, "notes_path": notes_path,
            "notes_count": len(notes), "tempo": tempo, "progression": progression}


def _progression_to_notes(progression, tempo, beats_per_bar, start_bar, bars):
    """Convierte una progresión en notas de bajo (beat 1) y acordes (block)."""
    notes = []
    beat_s = 60.0 / tempo
    chord_by_bar = {}
    for p in progression:
        for b in range(int(p.get("from_bar", start_bar)), int(p.get("to_bar", start_bar)) + 1):
            chord_by_bar[b] = p["chord"]
    for bar in range(start_bar, start_bar + bars):
        chord = chord_by_bar.get(bar)
        root, quality = _parse_chord(chord or "C")
        start = (bar - 1) * beats_per_bar * beat_s
        # Bajo: nota raíz en el beat 1, corchea.
        notes.append({"pitch": root - 24 if root - 24 >= 12 else root - 12,
                      "start_time": round(start, 3),
                      "duration": round(beat_s / 2, 3),
                      "velocity": 92})
        # Acorde: block de 3 notas.
        intervals = {"": [0, 4, 7], "m": [0, 3, 7], "sus4": [0, 5, 7],
                     "7": [0, 4, 7, 10], "m7": [0, 3, 7, 10], "maj7": [0, 4, 7, 11],
                     "dim": [0, 3, 6], "aug": [0, 4, 8]}.get(quality, [0, 4, 7])
        for i, iv in enumerate(intervals):
            notes.append({"pitch": root + iv + 12,
                          "start_time": round(start, 3),
                          "duration": round(beats_per_bar * beat_s - beat_s / 2, 3),
                          "velocity": 72})
    return notes


def _parse_chord(chord):
    """'Am' -> (57, 'm'); 'C' -> (48, '')."""
    chord = chord.strip()
    for i, name in enumerate(NOTE_NAMES):
        if chord.startswith(name):
            root = 60 + i  # C4 = 60
            rest = chord[len(name):]
            return root, rest
    return 48, ""


def _write_simple_midi(path, notes, tempo):
    """Escribe un MIDI tipo 0 monofónico/polifónico mínimo (bajo + acordes)."""
    import struct

    tpb = 480
    ppq = tpb
    bpm = tempo
    tempo_us = int(60000000.0 / bpm)
    track_events = []
    track_events.append((0, 0xFF, 0x51, struct.pack(">I", tempo_us)[1:]))  # set tempo
    track_events.append((0, 0xFF, 0x58, bytes([4, 2, 24, 8])))  # time signature 4/4
    # Ordena notas por tiempo.
    for n in sorted(notes, key=lambda x: x["start_time"]):
        t = int(n["start_time"] / (60.0 / bpm) * ppq)
        dur = int(n["duration"] / (60.0 / bpm) * ppq)
        vel = n["velocity"]
        track_events.append((t, 0x90, n["pitch"], vel))
        track_events.append((t + dur, 0x80, n["pitch"], vel))
    track_events.sort(key=lambda x: x[0])
    # Delta-times.
    data = bytearray()
    last = 0
    for ev in track_events:
        dt = ev[0] - last
        last = ev[0]
        # Variable-length quantity.
        vlq = bytearray()
        v = dt
        while v >= 0x80:
            vlq.insert(0, (v & 0x7F) | 0x80)
            v >>= 7
        vlq.insert(0, v)
        data += bytes(vlq)
        if ev[1] == 0xFF:
            data += bytes([0xFF, ev[2], len(ev[3])]) + ev[3]
        else:
            data += bytes([ev[1], ev[2], ev[3]])
    track_len = len(data)
    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, ppq)
    track = b"MTrk" + struct.pack(">I", track_len) + bytes(data)
    with open(path, "wb") as f:
        f.write(header + track)


def compare_tracks(mix_path, reference_path):
    """Compara una mezcla contra una referencia: balance de frecuencia,
    loudness (LUFS) y rango dinámico. Devuelve diferencias accionables."""
    import librosa
    import numpy as np
    import os

    def _load(p):
        if not p or not os.path.exists(p):
            return None
        return librosa.load(p, sr=44100, mono=True)

    mix, sr = _load(mix_path) or (None, None)
    ref, _ = _load(reference_path)
    if mix is None or ref is None:
        return {"ok": False, "error": "no se pudieron cargar las dos pistas"}
    # Iguala longitudes.
    n = min(len(mix), len(ref))
    mix, ref = mix[:n], ref[:n]

    def _band_energy(x):
        freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
        mag = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        out = {}
        for name, (lo, hi) in {
            "sub": (20, 60), "bass": (60, 200), "low_mid": (200, 500),
            "mid": (500, 2000), "high_mid": (2000, 6000), "high": (6000, sr / 2),
        }.items():
            mask = (freqs >= lo) & (freqs < hi)
            if mask.any():
                out[name] = float(np.sqrt((mag[mask] ** 2).mean()) / len(x))
            else:
                out[name] = 0.0
        return out

    def _rms_db(x):
        return float(20 * np.log10(np.sqrt((x ** 2).mean()) + 1e-9))

    mix_b = _band_energy(mix)
    ref_b = _band_energy(ref)
    diffs = {}
    for name in mix_b:
        mdb = 20 * np.log10(mix_b[name] + 1e-9)
        rdb = 20 * np.log10(ref_b[name] + 1e-9)
        diffs[name] = round(mdb - rdb, 1)
    # Loudness aproximado (RMS) y rango dinámico (percentiles).
    lufs_mix = _rms_db(mix)
    lufs_ref = _rms_db(ref)
    # Crest factor como proxy de rango dinámico.
    def _crest(x):
        return float(20 * np.log10(np.abs(x).max() / (np.sqrt((x ** 2).mean()) + 1e-9) + 1e-9))
    return {
        "ok": True,
        "band_diff_db": diffs,
        "mix": {"rms_db": round(lufs_mix, 1), "crest_db": round(_crest(mix), 1)},
        "reference": {"rms_db": round(lufs_ref, 1), "crest_db": round(_crest(ref), 1)},
        "loudness_diff_db": round(lufs_mix - lufs_ref, 1),
        "advice": _compare_advice(diffs, lufs_mix, lufs_ref),
    }


def _compare_advice(diffs, mix_db, ref_db):
    """Traduce las diferencias a consejos de mezcla accionables."""
    advice = []
    if abs(mix_db - ref_db) > 2.5:
        target = "subir" if mix_db < ref_db else "bajar"
        advice.append(f"El master está {abs(mix_db - ref_db):.0f} dB por debajo/arriba "
                      f"de la referencia: {target} el master o el bus.")
    for name, label in [("bass", "graves"), ("low_mid", "bajos-medios"),
                        ("mid", "medios"), ("high_mid", "presencia"),
                        ("high", "agudos")]:
        d = diffs.get(name, 0)
        if abs(d) > 3:
            action = "subir" if d < 0 else "bajar"
            advice.append(f"{label.capitalize()} {abs(d):.0f} dB por debajo/arriba "
                          f"de la referencia: {action} esa banda con un Eq8 en el bus.")
    return advice


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _log(self, msg):
        print(f"[audio] {msg}", flush=True)

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- helpers de análisis (parámetros por query string) ------------------
    def _qpath(self):
        import urllib.parse as up
        return up.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")

    def _load_param(self, q):
        import librosa
        path = q.get("path", [""])[0]
        if not path or not os.path.exists(path):
            self._json(400, {"ok": False, "error": f"archivo no encontrado: {path}"})
            return None, None, None
        audio, sr = librosa.load(path, sr=44100, mono=True)
        return path, audio, sr

    def _handle_analyze(self, path_):
        try:
            q = self._qpath()
            path, audio, sr = self._load_param(q)
            if path is None:
                return
            result = detect_key(audio, sr)
            self._log(f"/analyze -> key={result.get('key')} tempo={result.get('tempo')}")
            self._json(200, {"ok": True, "file": os.path.basename(path), **result})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def _handle_harmony(self, path_):
        try:
            q = self._qpath()
            path, audio, sr = self._load_param(q)
            if path is None:
                return
            tempo = float(q.get("tempo", [0])[0]) or None
            beats_per_bar = int(q.get("beats_per_bar", [4])[0])
            start_bar = int(q.get("start_bar", [1])[0])
            max_seconds = float(q.get("max_seconds", [240])[0])
            chords = detect_chords(audio, sr, tempo, beats_per_bar, start_bar, max_seconds)
            self._log(f"/harmony -> {len(chords)} barras, {sum(1 for c in chords if c.get('chord'))} acordes")
            self._json(200, {"ok": True, "file": os.path.basename(path),
                             "tempo": tempo, "beats_per_bar": beats_per_bar,
                             "chords": chords})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def _handle_groove(self, path_):
        try:
            q = self._qpath()
            path, audio, sr = self._load_param(q)
            if path is None:
                return
            tempo = float(q.get("tempo", [0])[0]) or None
            beats_per_bar = int(q.get("beats_per_bar", [4])[0])
            time_scale = float(q.get("time_scale", [1.0])[0])
            result = detect_groove(audio, sr, tempo, beats_per_bar, time_scale)
            result["file"] = os.path.basename(path)
            self._log(f"/groove -> count={result.get('count')} off={result.get('mean_offset_beats')}")
            code = 200 if result.get("ok") else 422
            self._json(code, result)
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def _handle_transcribe_file(self, path_):
        try:
            q = self._qpath()
            path = q.get("path", [""])[0]
            grid = float(q.get("grid", [0.25])[0])
            result = transcribe_file(path, grid=grid)
            self._log(f"/transcribe_file -> ok={result.get('ok')} count={result.get('count')}")
            code = 200 if result.get("ok") else 500
            self._json(code, result)
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def _handle_stems(self, path_):
        try:
            q = self._qpath()
            path = q.get("path", [""])[0]
            quality = q.get("quality", ["fast"])[0]
            probe = q.get("probe", ["false"])[0].lower() == "true"
            result = separate_stems(path, quality, probe)
            self._log(f"/stems -> ok={result.get('ok')} cached={result.get('cached')} "
                      f"stems={len(result.get('stems') or {})}")
            code = 200 if result.get("ok") else 500
            self._json(code, result)
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def _handle_compose(self, path_):
        try:
            q = self._qpath()
            import json as _json_mod
            tempo = float(q.get("tempo", [120])[0])
            beats_per_bar = int(q.get("beats_per_bar", [4])[0])
            start_bar = int(q.get("start_bar", [1])[0])
            bars = int(q.get("bars", [8])[0])
            prog_raw = q.get("progression", [""])[0]
            progression = None
            if prog_raw:
                try:
                    progression = _json_mod.loads(prog_raw)
                except Exception:
                    progression = None
            result = compose_accompaniment(tempo, beats_per_bar, start_bar, bars, progression)
            self._log(f"/compose -> ok={result.get('ok')} notes={result.get('notes_count')}")
            self._json(200, result)
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def _handle_describe(self, path_):
        try:
            q = self._qpath()
            path = q.get("path", [""])[0]
            result = describe_sound(path)
            self._log(f"/describe -> ok={result.get('ok')} tags={result.get('tags')}")
            code = 200 if result.get("ok") else 500
            self._json(code, result)
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def _handle_compare(self, path_):
        try:
            q = self._qpath()
            mix = q.get("mix", [""])[0]
            ref = q.get("reference", [""])[0]
            result = compare_tracks(mix, ref)
            self._log(f"/compare -> ok={result.get('ok')} diff={result.get('loudness_diff_db')}dB")
            code = 200 if result.get("ok") else 500
            self._json(code, result)
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def _authorized(self):
        if not LOCAL_TOKEN:
            return True
        return self.headers.get(LOCAL_TOKEN_HEADER) == LOCAL_TOKEN

    def do_GET(self):
        if not self._authorized():
            self._json(403, {"error": "forbidden"})
            return
        if self.path == "/health":
            bridge = False
            ableton = False
            tempo = None
            instances = []
            active_port = 9878
            # Read persisted active instance from the bridge
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       os.path.pardir, "bridge", "active_instance.json"),
                          "r", encoding="utf-8") as f:
                    active_port = int(json.load(f).get("port", 9878))
            except Exception:
                pass
            # Scan MIDI Remote Script ports (9878..9893)
            for offset in range(16):
                port = 9878 + offset
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    # Live ocupa su hilo principal a menudo: timeouts cortos
                    # hacen parpadear el indicador ("desconectado" fantasma).
                    s.settimeout(1.2)
                    s.connect(("127.0.0.1", port))
                    s.sendall(b'{"type":"ping","params":{}}')
                    s.settimeout(2.5)
                    data = s.recv(4096)
                    s.close()
                    resp = json.loads(data.decode("utf-8"))
                    if resp.get("status") == "success":
                        bridge = True
                        ableton = True
                        inst = {
                            "port": port,
                            "active": port == active_port,
                            "version": resp.get("result", {}).get("version", ""),
                            "live_version": resp.get("result", {}).get("live_version", ""),
                        }
                        # Nombre del set/proyecto de la instancia
                        try:
                            s3 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            s3.settimeout(2.0)
                            s3.connect(("127.0.0.1", port))
                            s3.sendall(b'{"type":"get_session_info","params":{}}')
                            s3.settimeout(2.0)
                            sdata = s3.recv(262144)
                            s3.close()
                            sresp = json.loads(sdata.decode("utf-8"))
                            if sresp.get("status") == "success":
                                inst["song_name"] = str((sresp.get("result") or {}).get("song_name", ""))
                        except Exception:
                            pass
                        # Tempo for the active instance
                        if port == active_port:
                            try:
                                s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                s2.settimeout(2.0)
                                s2.connect(("127.0.0.1", port))
                                s2.sendall(b'{"type":"get_transport_state","params":{}}')
                                s2.settimeout(2.0)
                                tdata = s2.recv(65536)
                                s2.close()
                                tresp = json.loads(tdata.decode("utf-8"))
                                if tresp.get("status") == "success":
                                    tempo = tresp["result"].get("tempo")
                            except Exception:
                                pass
                        instances.append(inst)
                except (socket.timeout, OSError, ValueError, KeyError):
                    pass
            # Fallback to old UDP bridge if nothing responded
            if not instances:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.settimeout(BRIDGE_TEMPO_TIMEOUT)
                    s.sendto(b"tempo", ("127.0.0.1", BRIDGE_TEMPO_PORT))
                    data, _ = s.recvfrom(1024)
                    s.close()
                    bridge = True
                    ableton = bool(data.strip())
                    tempo = float(data) if ableton else None
                except (socket.timeout, OSError):
                    bridge = False
                    ableton = False
                    tempo = None
            self._json(200, {
                "ok": True, "bridge": bridge, "ableton": ableton, "tempo": tempo,
                "active_port": active_port, "instances": instances,
            })
        elif self.path.startswith("/listen"):
            try:
                duration = LISTEN_SECONDS
                q = self.path.split("?", 1)
                if len(q) > 1:
                    import urllib.parse

                    params = urllib.parse.parse_qs(q[1])
                    if params.get("seconds"):
                        duration = max(0.5, min(float(params["seconds"][0]), 8.0))
                try:
                    audio, rate = capture_interface_loopback(duration)
                    source = "interface"
                except Exception:
                    audio, rate = capture_loopback(duration)
                    source = "wasapi"
                analysis = analyze_master(audio, rate)
                self._log(f"/listen -> source={source} señal={analysis.get('signal')} rms={analysis.get('rms_db')}dB dominante={analysis.get('dominant_hz')}Hz")
                self._json(200, {"ok": True, "seconds": duration, "rate": rate, "source": source, **analysis})
            except Exception as exc:
                self._log(f"/listen -> error: {exc}")
                self._json(500, {"ok": False, "error": str(exc)})
        elif self.path.startswith("/analyze_file"):
            try:
                import urllib.parse as up

                q = up.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                path = q.get("path", [""])[0]
                result = analyze_audio_file(path)
                self._log(f"/analyze_file -> {result.get('file')} ok={result.get('ok')} "
                          f"señal={result.get('signal')} rms={result.get('rms_db')}dB")
                code = 200 if result.get("ok") else 500
                self._json(code, result)
            except Exception as exc:
                self._log(f"/analyze_file -> error: {exc}")
                self._json(500, {"ok": False, "error": str(exc)})
        elif self.path.startswith("/analyze"):
            self._handle_analyze(self.path)
        elif self.path.startswith("/harmony"):
            self._handle_harmony(self.path)
        elif self.path.startswith("/groove"):
            self._handle_groove(self.path)
        elif self.path.startswith("/transcribe_file"):
            self._handle_transcribe_file(self.path)
        elif self.path.startswith("/stems"):
            self._handle_stems(self.path)
        elif self.path.startswith("/compose"):
            self._handle_compose(self.path)
        elif self.path.startswith("/describe"):
            self._handle_describe(self.path)
        elif self.path.startswith("/compare"):
            self._handle_compare(self.path)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._json(403, {"error": "forbidden"})
            return
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        if not data:
            self._json(400, {"error": "sin audio"})
            return
        self._log(f"POST {self.path} -> {length} bytes")
        try:
            if self.path == "/stt":
                text = whisper_transcribe(data)
                self._log(f"/stt -> texto: {text!r}")
                self._json(200, {"text": text})
            elif self.path == "/midi":
                notes, backend, bpm = transcribe_midi(data)
                self._log(f"/midi -> backend={backend} count={len(notes) if notes else 0}")
                if notes is None:
                    self._json(500, {
                        "error": f"No hay backend de transcripción instalado ({backend}).",
                        "fix": "pip install basic-pitch",
                    })
                    return
                self._json(200, {"notes": notes, "backend": backend, "count": len(notes), "bpm": bpm})
            elif self.path.startswith("/midi?mode=rhythm"):
                bpm = get_live_tempo() or 120.0
                notes = transcribe_rhythm(data, bpm)
                self._log(f"/midi?mode=rhythm -> count={len(notes)}")
                self._json(200, {
                    "notes": notes,
                    "backend": "librosa-onsets",
                    "mode": "rhythm",
                    "count": len(notes),
                    "bpm": bpm,
                    "pitch": RHYTHM_PITCH,
                })
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:
            self._json(500, {"error": str(exc)})


def main():
    threading.Thread(target=_preload_whisper, daemon=True, name="whisper-preload").start()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        print(f"[audio] escuchando en http://{HOST}:{PORT}", flush=True)
        server.serve_forever()
    except OSError as e:
        if e.errno == 10048 or "Address already in use" in str(e):
            print(f"[audio] ERROR: port {PORT} already in use", flush=True)
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
