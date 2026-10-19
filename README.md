# AbletonMCP

Puente **MCP** (Model Context Protocol) para controlar **Ableton Live 12** desde
cualquier cliente MCP: [opencode](https://opencode.ai), Claude Desktop, Cursor,
o tu propio agente.

No es una app cerrada. Es el **protocolo + el puente**: tú conectas tu agente y
él maneja Live — pistas, clips, notas, devices, mixer, transporte, browser,
automatización y arreglo.

```
┌──────────────────────────────────────────┐
│  Tu cliente MCP (opencode, Claude, …)    │
└────────────────┬─────────────────────────┘
                 │  MCP sobre HTTP  (127.0.0.1:9004/mcp)
┌────────────────▼─────────────────────────┐
│  bridge/mcp_http.py                      │
│    └── bridge/mcp_server.py  (56 tools)  │
└───────┬──────────────────────┬───────────┘
        │ MIDI Remote (TCP 9878)          │ UDP 9001→9002 (helper M4L)
┌───────▼──────────────┐   ┌───────────────▼────────────┐
│ midi-remote-script/  │   │ bridge/device_helper.amxd  │
│ AbletonMCP (en Live) │   │ (devices nativos, racks)   │
└──────────────────────┘   └────────────────────────────┘
```

## Qué puedes hacer

**56 herramientas**, agrupadas:

| Grupo | Herramientas |
|---|---|
| Conexión | `ping`, `list_instances`, `select_instance` |
| Sesión | `list_song`, `get_device_parameters`, `set_parameter`, `set_track` |
| Transporte | `transport`, `set_tempo`, `get_tempo`, `set_metronome` |
| Escenas y clips | `get_scenes`, `fire_scene`, `fire_clip`, `create_clip` |
| Pistas | `create_track`, `create_midi_track`, `delete_track` |
| Devices | `insert_device`, `load_plugin`, `create_rack`, `chain_create_device`, `map_macro` |
| Notas MIDI | `add_notes`, `create_arrangement_clip`, `clip_to_arrangement` |
| Arreglo | `list_arrangement_clips`, `delete_arrangement_clip`, `set_follow_action` |
| Automatización | `automate_clip_param`, `clear_clip_param`, `get_clip_param_envelope` |
| Browser | `browser_list`, `browser_load` |
| Análisis de audio | `ableton_listen`, `analyze_clip`, `analyze_key`, `get_chords`, `get_groove`, `transcribe_file`, `separate_stems`, `compose_accompaniment`, `describe_sound`, `compare_mix` |
| Memoria | `session_summary`, `session_note`, `session_preference`, `global_memory_*` |

## Requisitos

| Pieza | Versión |
|---|---|
| Ableton Live | 11 o 12 (probado en 12 Suite) |
| Python | 3.11 o 3.12 (**evita 3.13**) |
| Un cliente MCP | opencode, Claude Desktop, etc. |

## Instalación

### 1. El puente

```bash
git clone https://github.com/ilfafinni/ableton-mcp-bridge.git
cd ableton-mcp-bridge
pip install -r requirements.txt
```

Solo `python-osc` es imprescindible. El resto (librosa, basic-pitch, demucs)
habilita las herramientas de análisis de audio; si no las instalas, el puente
funciona igual y esas tools concretas devolverán error.

### 2. El MIDI Remote Script (la pieza clave)

Este script corre **dentro** de Live y es lo que permite crear clips y escribir
notas (AbletonOSC no puede).

**Windows:**
```powershell
.\install_midi_script.ps1
```

**macOS / Linux:**
```bash
cp -r midi-remote-script/AbletonMCP \
  ~/Music/Ableton/User\ Library/Remote\ Scripts/
```

Después, en Live: **Preferencias → Link/Tempo/MIDI**, y en *Control Surface*
elige **AbletonMCP**. Debe aparecer el mensaje de que escucha en el puerto 9878.

### 3. (Opcional) El helper Max for Live

Solo necesario para devices **nativos** (`Eq8`, `Compressor`, `DrumRack`…).
Para VSTs y plugins del browser no hace falta.

Arrastra `bridge/device_helper.amxd` a cualquier pista de Live.

### 4. Conectar tu cliente MCP

El puente habla **MCP sobre HTTP**. El endpoint es:

```
http://127.0.0.1:9004/mcp
```

Arranca el servidor:

```bash
python bridge/mcp_http.py
```

**opencode** — añade a tu `opencode.json`:

```json
{
  "mcp": {
    "ableton": {
      "type": "remote",
      "url": "http://127.0.0.1:9004/mcp",
      "enabled": true
    }
  }
}
```

**Claude Desktop** — en `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ableton": {
      "type": "http",
      "url": "http://127.0.0.1:9004/mcp"
    }
  }
}
```

También funciona por **stdio** si tu cliente no soporta HTTP:

```json
{
  "mcpServers": {
    "ableton": { "command": "python", "args": ["bridge/mcp_server.py"] }
  }
}
```

## Autenticación

Por defecto el puente **no pide token** y escucha solo en `127.0.0.1`, así que
solo tu máquina puede hablarle.

Si quieres exigir token (recomendado si expones el puerto o usas la API móvil),
define la variable de entorno antes de arrancar:

```bash
export ABLETON_MCP_TOKEN="tu-token-largo-aqui"
python bridge/mcp_http.py
```

Y manda el header `x-ableton-token` en cada petición.

> El MIDI Remote Script busca el mismo token en `bridge_token` dentro de
> `~/.config/ableton-mcp/` (Linux/macOS) o `%APPDATA%\ableton-mcp\`
> (Windows). Si no lo encuentra, no exige autenticación.

## Servidor de análisis de audio (opcional)

`python/audio_server.py` expone análisis musical sobre HTTP (puerto 8787):
tonalidad, acordes por compás, groove, transcripción a MIDI, separación de
stems, y escucha del máster vía WASAPI loopback.

```bash
python python/audio_server.py
```

Es lo que alimenta las tools `analyze_key`, `get_chords`, `ableton_listen`, etc.
Si no lo arrancas, el resto del puente funciona igual.

## Ejemplos

En `examples/` hay fragmentos listos para copiar: crear una canción completa,
escribir notas relativas al clip (el detalle que más confunde), y automatizar
un parámetro.

## Notas importantes

- **`add_notes` usa tiempos relativos al inicio del clip**, no absolutos. Es el
  error más común: pasar tiempos absolutos estira el clip en vez de rellenarlo.
- **Varias instancias de Live**: cada una usa un puerto distinto (9878, 9879…).
  Usa `list_instances` y `select_instance`.
- **`list_song` tiene caché de 10 s** y opciones para ahorrar tokens
  (`track=N`, `compact=true`, `skip_values=true`, `depth=N`).

## Tests

```bash
python tests/test_bridge.py     # puente MCP con AbletonOSC simulado
python tests/test_quantize.py   # cuantización
python tests/test_amxd.py       # cableado del helper M4L
```

Los tests no necesitan Live abierto: levantan un AbletonOSC falso en
11000/11001 y un helper M4L falso en 9001/9002.

> El chequeo final de `test_bridge.py` consulta el servidor de tempo (UDP 9003)
> del propio puente. Si el puerto está ocupado por otra instancia, ese último
> assert puede expirar; el resto pasa. Cierra cualquier `mcp_http.py` que
> tengas corriendo antes de lanzar los tests.

## Arquitectura

| Archivo | Rol |
|---|---|
| `bridge/mcp_http.py` | Servidor MCP sobre HTTP. Punto de entrada. |
| `bridge/mcp_server.py` | Las 56 tools y la traducción a OSC/TCP. |
| `bridge/midi_remote_tcp.py` | Cliente TCP hacia el script dentro de Live. |
| `bridge/build_helper.py` | Genera el helper M4L (`device_helper.amxd`). |
| `midi-remote-script/AbletonMCP/` | El Control Surface que corre dentro de Live. |
| `python/audio_server.py` | Análisis de audio y escucha del máster (opcional). |


## Licencia

MIT — ver [LICENSE](LICENSE).
