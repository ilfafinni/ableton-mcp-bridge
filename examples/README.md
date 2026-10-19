# Ejemplos

Fragmentos para copiar en tu agente. Todos asumen que el puente está corriendo
y tu cliente MCP ya ve las tools.

---

## 1. Crear una canción desde cero

Pídele esto a tu agente:

> Crea una canción de 8 compases a 120 BPM: una pista de batería con un beat
> básico, una de bajo siguiendo la fundamental, y una de acordes. Luego
> muévelas al Arrangement.

El agente encadenará algo así:

```python
set_tempo(bpm=120)

# Batería
bat = create_midi_track(name="Bateria")
insert_device(track_index=bat, device_class="DrumRack")
create_clip(track_index=bat, clip_index=0, length_beats=32)
add_notes(track_index=bat, clip_index=0, notes=[
    {"pitch": 36, "start_time": 0.0,  "duration": 0.25, "velocity": 110},
    {"pitch": 38, "start_time": 1.0,  "duration": 0.25, "velocity": 100},
    {"pitch": 36, "start_time": 2.0,  "duration": 0.25, "velocity": 110},
    {"pitch": 38, "start_time": 3.0,  "duration": 0.25, "velocity": 100},
])

# Bajo
bajo = create_midi_track(name="Bajo")
insert_device(track_index=bajo, device_class="Operator")
create_clip(track_index=bajo, clip_index=0, length_beats=32)
add_notes(track_index=bajo, clip_index=0, notes=[
    {"pitch": 40, "start_time": 0.0, "duration": 1.5, "velocity": 100},
    {"pitch": 40, "start_time": 2.0, "duration": 1.5, "velocity": 90},
])
```

> **Truco:** el agente no necesita saber los índices de pista de antemano —
> `create_midi_track` los devuelve. Y `list_song(compact=true)` da el mapa
> completo de la sesión si se pierde.

---

## 2. El detalle que más confunde: `add_notes`

`add_notes` usa tiempos **relativos al inicio del clip**, no absolutos.

```
Clip que empieza en el compás 9 (beat 32), con 4 compases de largo:

  MAL:  start_time=32   ->  el clip se ESTIRA hasta el beat 32
  BIEN: start_time=0    ->  la nota va al principio del clip
```

Regla práctica: **el primer `start_time` de cada clip es 0**, y el último nunca
supera `length_beats`.

Lo mismo aplica a `create_arrangement_clip`: `start_time` sí es absoluto ahí
(dónde empieza el clip en la línea de tiempo), pero las notas dentro van
relativas.

---

## 3. Armar una cadena de FX con macros

> Arma un rack de voz: Eq8, Compressor, Eq8 otra vez, y mapea el threshold y el
> ratio a las dos primeras macros.

```python
rack = create_rack(track_index=voz, kind="audio")
chain_create_device(track_index=voz, rack_index=rack, device_class="Eq8")
chain_create_device(track_index=voz, rack_index=rack, device_class="Compressor")
map_macro(track_index=voz, rack_index=rack,
          chain_device_index=1, parameter_index=1, macro_index=0)
```

---

## 4. Automatizar un parámetro

> Automatiza el volumen de la pista 2 para que baje de 0.8 a 0.4 en 8 compases.

```python
automate_clip_param(
    track_index=2,
    clip_index=0,
    parameter_index=0,   # volume en la pista
    points=[
        {"time": 0.0,  "value": 0.8},
        {"time": 32.0, "value": 0.4},
    ],
)
```

Luego `get_clip_param_envelope` para verificar lo que quedó escrito.

---

## 5. Analizar el audio (si arrancaste `audio_server.py`)

> ¿En qué tonalidad está lo que suena? ¿Y qué acordes hace?

```python
ableton_listen(seconds=4)   # escucha el máster
analyze_key()               # tonalidad + tempo
get_chords()                # acordes por compás
```

Para un clip concreto **sin darle play**:

```python
analyze_clip(track_index=0, clip_index=0)
```

---

## 6. VSTs de terceros

> Carga Serum en la pista 3.

```python
load_plugin(track_index=3, query="Serum")
```

El nombre puede ser parcial y busca en todo el browser (VST, AU, samples,
packs). Después verifica qué expone:

```python
list_song(track=3, depth=1)
```

> Ojo: muchos VSTs no exponen parámetros al host, así que `set_parameter`
> puede no tener nada que mover. Para EQ y dinámica, los nativos (`Eq8`,
> `Compressor`) siempre funcionan.
