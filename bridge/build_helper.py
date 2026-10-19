#!/usr/bin/env python3
"""
Genera bridge/device_helper.amxd — un device M4L que extiende lo que
AbletonOSC no puede hacer, recibiendo OSC en el puerto 9001 y respondiendo
en el 9002 (el bridge MCP escucha ahí para VERIFICAR cada operación):

  /create_device <track> <ui_name>
      Inserta un device NATIVO en una pista (ui_name: el nombre como aparece
      en la UI de Live, p.ej. "EQ Eight", "Compressor", "Glue Compressor").
      Respuesta: /ok create_device  |  /error <motivo>  |  /log <traza>

  /create_rack <track> <rack_name>
      Crea un rack: rack_name = "Audio Effect Rack" | "Instrument Rack" |
      "Drum Rack" (el bridge traduce kind audio/instrument/drum).
      Respuesta: /ok create_rack  |  /error <motivo>  |  /log <traza>

  /chain_create_device <track> <rack_index> <ui_name>
      Inserta un device DENTRO de la cadena del rack.
      Respuesta: /ok chain_device  |  /error <motivo>  |  /log <traza>

  /browser_create <track> <query>
      NO implementado en el helper (browser no expuesto a M4L). /error.

  /map_macro <track> <rack_index> <chain_device_index> <parameter_index> <macro_index>
      NO implementado todavía. /error.

  /clip_follow_action <track> <clip_slot> <property> <value>
      Programa la follow action de un clip (transición automática A→B).
      property: follow_action | follow_action_time | follow_action_index.
      Navega al clip con live.path (live_set tracks N clip_slots M clip) y
      setea la propiedad con `set <propiedad> <valor>`. Como `set` NO tiene
      salida en live.object, se verifica con `get <propiedad>` (emite
      "<propiedad> <valor>") y route la reenvía a /ok.
      Respuesta: /ok clip_follow_action  |  /error <motivo>  |  /log <traza>

  /load_audio <track> <slot> <file_path>
      Carga un archivo de audio en un clip slot (session) o en el arrangement
      (view='arrangement', start_time en beats). Usa Track.create_audio_clip /
      clip_slot.create_audio_clip (Live 11+). El bridge apaga el warp después
      con AbletonOSC (/live/clip/set/warping 0) para que los stems de la misma
      fuente queden alineados.
      Respuesta: /ok load_audio  |  /error <motivo>  |  /log <traza>

Instalación:  python bridge/build_helper.py   (genera device_helper.amxd)
y arrastra device_helper.amxd a CUALQUIER pista de Ableton (p.ej. Master).

NOTA: se genera como AudioEffect para poder colocarlo en el Master / pistas
de audio (los MidiEffect solo van en pistas MIDI).

La inserción de devices nativos se hace con Track.insert_device(name) /
Chain.insert_device(name) (Live 12.3+). El name es el nombre TAL COMO
APARECE en la UI de Live ("EQ Eight", no "Eq8").

Patrón M4L (documentado por Cycling '74):
  [message path live_set tracks $1] -> [live.path] -> [t b l]
        t.l -> [live.object] (inlet derecho: "id nn")
        t.b -> [message call ...] -> [live.object] (inlet izquierdo)
  Un message box con $1/$2 almacena el mensaje al recibir la lista y lo
  re-emite al recibir un bang. La API de Live se procesa en el hilo principal
  (diferida), así que el id queda fijado ANTES de ejecutar la llamada.
  El outlet de live.object devuelve el resultado de cada `call`; con
  [select insert_device] distinguimos /ok de /error.

IMPORTANTE (gotcha de Max): las comillas dobles ESCAPAN los $n de un message
box ("To escape multiple characters, enclose them in double quotes"), así
que NUNCA escribas 'call insert_device "$2"' — el $2 saldría literal. Se
escribe 'call insert_device $2' y el símbolo con espacios se sustituye como
un solo átomo.

DIAGNÓSTICO: cada ruta añade taps /log que reenvían al 9002 lo que pasa por
los puntos clave (mensaje de path, id resuelto por live.path, mensaje call,
y la salida de live.object). El bridge IGNORA los mensajes /log (solo espera
/ok o /error), así que sirven para depurar sin interferir.
"""

import os
from m4l_builder import AudioEffect, MIDNIGHT

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "device_helper.amxd")


def add_msg(device, id, text, numoutlets=1):
    """Añade un message box real de Max (maxclass "message").

    Los $n se almacenan con el mensaje `setargs <args>` (vía un [prepend
    setargs]) que NO emite y NO reemplaza el texto del box; el bang del
    trigger emite el mensaje ya formateado. Un message box que recibe la
    lista por el inlet izquierdo la sustituye y emite de inmediato (doble
    disparo), y el inlet derecho reemplaza el contenido completo del box.
    """
    device.add_box({
        "box": {
            "id": id,
            "maxclass": "message",
            "fontname": "Arial",
            "fontsize": 10.0,
            "numinlets": 1,
            "numoutlets": numoutlets,
            "patching_rect": [0, 0, 80, 19],
            "text": text,
        }
    })


def build() -> str:
    device = AudioEffect("Device Helper", width=420, height=360,
                         theme=MIDNIGHT)

    # --- entrada OSC y respuestas -------------------------------------------
    device.add_newobj("udp", "udpreceive 9001", numinlets=0, numoutlets=1)
    device.add_newobj("route",
                      "route /create_device /create_rack /chain_create_device "
                      "/browser_create /map_macro /diag /diag2 /arr_add /arr_clear "
                      "/clip_follow_action /load_audio",
                      numinlets=1, numoutlets=12)
    # udpsend solo admite UNA conexión a su inlet: todo pasa por "merger".
    device.add_newobj("merger", "prepend", numinlets=1, numoutlets=1)
    device.add_newobj("udpsend", "udpsend 127.0.0.1 9002",
                      numinlets=1, numoutlets=0)
    device.add_newobj("rerr", "prepend /error", numinlets=1, numoutlets=1)
    device.add_newobj("rok", "prepend /ok", numinlets=1, numoutlets=1)

    # ========================================================================
    # /create_device <track> <ui_name>
    #   route 0 -> t l l -> [path live_set tracks $1] -> live.path
    #   live.path -> t b l -> (l) live.object right ; (b) call stored -> left
    #   Con taps /log en: mensaje path, id resuelto, call emitido, salida.
    # ========================================================================
    device.add_newobj("t1a", "t l l", numinlets=1, numoutlets=2)
    add_msg(device, "mp1", "path live_set tracks $1")
    device.add_newobj("pth1", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("trg1", "t b l", numinlets=1, numoutlets=2)
    device.add_newobj("obj1", "live.object", numinlets=2, numoutlets=1)
    add_msg(device, "mc1", "call insert_device $2")
    device.add_newobj("sa1", "prepend setargs", numinlets=1, numoutlets=1)
    device.add_newobj("sl1", "select insert_device", numinlets=1, numoutlets=2)
    add_msg(device, "mok1", "create_device")
    device.add_newobj("logi1", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logt1", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logc1", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("log1", "prepend /log", numinlets=1, numoutlets=1)

    # ========================================================================
    # /create_rack <track> <rack_name>
    #   Igual que create_device pero el call inserta el rack.
    # ========================================================================
    device.add_newobj("t1r", "t l l", numinlets=1, numoutlets=2)
    add_msg(device, "mpr", "path live_set tracks $1")
    device.add_newobj("pthr", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("trgr", "t b l", numinlets=1, numoutlets=2)
    device.add_newobj("objr", "live.object", numinlets=2, numoutlets=1)
    add_msg(device, "mcr", "call insert_device $2")
    device.add_newobj("sar", "prepend setargs", numinlets=1, numoutlets=1)
    device.add_newobj("slr", "select insert_device", numinlets=1, numoutlets=2)
    add_msg(device, "mokr", "create_rack")
    device.add_newobj("logir", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logcr", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logr", "prepend /log", numinlets=1, numoutlets=1)

    # ========================================================================
    # /chain_create_device <track> <zl_idx> <ui_name>
    #   live.path NO puede navegar dentro de `devices` (devuelve id 0), así que
    #   se navega con live.object: track -> get devices (ids) -> zl nth
    #   (extrae el rack por zl_idx = 2*rack+3, lo manda el bridge) -> rack ->
    #   get chains -> cadena -> call insert_device.
    # ========================================================================
    device.add_newobj("tc4", "t l l l", numinlets=1, numoutlets=3)
    add_msg(device, "mcp4", "path live_set tracks $1")
    device.add_newobj("pth4", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("trg4", "t b l", numinlets=1, numoutlets=2)
    device.add_newobj("obj4a", "live.object", numinlets=2, numoutlets=1)
    add_msg(device, "mc4d", "get devices")
    device.add_newobj("t4", "t l b", numinlets=1, numoutlets=2)
    device.add_newobj("rt4", "route devices", numinlets=1, numoutlets=1)
    add_msg(device, "mz4", "$2")
    device.add_newobj("zl4", "zl.nth", numinlets=2, numoutlets=2)
    device.add_newobj("pr4", "prepend id", numinlets=1, numoutlets=1)
    device.add_newobj("logrt4", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("dl4", "delay 1", numinlets=1, numoutlets=1)
    device.add_newobj("obj4b", "live.object", numinlets=2, numoutlets=1)
    device.add_newobj("t4c", "t l b", numinlets=1, numoutlets=2)
    device.add_newobj("rt4c", "route insert_chain x", numinlets=1, numoutlets=2)
    device.add_newobj("r4c", "route id x", numinlets=1, numoutlets=2)
    device.add_newobj("pr4c", "prepend id", numinlets=1, numoutlets=1)
    device.add_newobj("dl4c", "delay 1", numinlets=1, numoutlets=1)
    device.add_newobj("obj4c", "live.object", numinlets=2, numoutlets=1)
    add_msg(device, "ic4", "call insert_chain")
    device.add_newobj("logrtc", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logr4c", "prepend /log", numinlets=1, numoutlets=1)
    add_msg(device, "mc4", "call insert_device $3")
    device.add_newobj("sa4", "prepend setargs", numinlets=1, numoutlets=1)
    device.add_newobj("sl4", "select insert_device", numinlets=1, numoutlets=2)
    add_msg(device, "mok4", "chain_device")
    device.add_newobj("logi4", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logt4", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logd4", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logm4", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logz4", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logb4", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logc4", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("log4", "prepend /log", numinlets=1, numoutlets=1)

    # ========================================================================
    # /arr_add <track> <time_beats> <value01>
    #   Escribe automatización de Arrangement en el volumen del mixer de la
    #   pista. Navega directo con live.path al parámetro volumen:
    #       live_set tracks N mixer_device volume
    #   -> id del parámetro -> call get_automation_envelope -> id del envelope
    #   -> call add $2 $3 (time_beats, value01).
    #   time en beats, value 0-1. Un punto por mensaje.
    # ========================================================================
    device.add_newobj("ta3", "t l l l", numinlets=1, numoutlets=3)
    add_msg(device, "map3", "path live_set tracks $1 mixer_device volume")
    device.add_newobj("pth3", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("trg3", "t b l", numinlets=1, numoutlets=2)
    device.add_newobj("obj3v", "live.object", numinlets=2, numoutlets=1)
    add_msg(device, "m3e", "call get_automation_envelope")
    device.add_newobj("t3e", "t l b", numinlets=1, numoutlets=2)
    device.add_newobj("rt3e", "route get_automation_envelope x", numinlets=1, numoutlets=2)
    device.add_newobj("pr3e", "prepend id", numinlets=1, numoutlets=1)
    device.add_newobj("dl3e", "delay 1", numinlets=1, numoutlets=1)
    device.add_newobj("obj3e", "live.object", numinlets=2, numoutlets=1)
    add_msg(device, "m3a", "call add $2 $3")
    device.add_newobj("sa3", "prepend setargs", numinlets=1, numoutlets=1)
    device.add_newobj("sl3", "select add", numinlets=1, numoutlets=2)
    add_msg(device, "mok3", "arr_add")
    device.add_newobj("log3p", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("log3t", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("log3v", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("log3e", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("log3a", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("log3", "prepend /log", numinlets=1, numoutlets=1)

    # ========================================================================
    # /arr_clear <track>  -> mixer_device volume, delete_automation
    # ========================================================================
    device.add_newobj("tac", "t l", numinlets=1, numoutlets=1)
    add_msg(device, "mapc", "path live_set tracks $1 mixer_device volume")
    device.add_newobj("pthc", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("trgc", "t b l", numinlets=1, numoutlets=2)
    device.add_newobj("objcv", "live.object", numinlets=2, numoutlets=1)
    add_msg(device, "mcd", "call delete_automation")
    device.add_newobj("slc", "select delete_automation", numinlets=1, numoutlets=2)
    add_msg(device, "mokc", "arr_clear")
    device.add_newobj("logcp", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logct", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logcv", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logc", "prepend /log", numinlets=1, numoutlets=1)

    # ========================================================================
    # /clip_follow_action <track> <clip_slot> <property> <value>
    #   Programa la follow action de un clip (transición automática A→B).
    #   property: follow_action | follow_action_time | follow_action_index.
    #   Navega al clip con live.path y setea la propiedad con `set`. Como `set`
    #   NO tiene salida en live.object, se verifica con `get <propiedad>`
    #   (emite "<propiedad> <valor>") y route la reenvía a /ok.
    # ========================================================================
    device.add_newobj("tfa", "t l l l l", numinlets=1, numoutlets=4)
    add_msg(device, "mfa", "path live_set tracks $1 clip_slots $2 clip")
    device.add_newobj("pthfa", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("trgfa", "t b l", numinlets=1, numoutlets=2)
    device.add_newobj("objfa", "live.object", numinlets=2, numoutlets=1)
    add_msg(device, "mcfa", "set $3 $4")
    device.add_newobj("safa", "prepend setargs", numinlets=1, numoutlets=1)
    add_msg(device, "mgfa", "get $3")
    device.add_newobj("saga", "prepend setargs", numinlets=1, numoutlets=1)
    device.add_newobj("dlfa", "delay 1", numinlets=1, numoutlets=1)
    device.add_newobj("rtfa", "route follow_action follow_action_time follow_action_index",
                      numinlets=1, numoutlets=2)
    add_msg(device, "mokfa", "clip_follow_action")
    device.add_newobj("logfap", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logfat", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logfac", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logfag", "prepend /log", numinlets=1, numoutlets=1)

    # ========================================================================
    # /load_audio <track> <slot> <file_path>
    #   Carga un archivo de audio en un clip slot de session:
    #   live.path live_set tracks N clip_slots M -> call create_audio_clip
    #   (con el file_path como $3, símbolo con espacios OK). El bridge apaga
    #   warp después por OSC (/live/clip/set/warping 0).
    # ========================================================================
    device.add_newobj("tla", "t l l l", numinlets=1, numoutlets=3)
    add_msg(device, "mlap", "path live_set tracks $1 clip_slots $2")
    device.add_newobj("pthla", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("trgla", "t b l", numinlets=1, numoutlets=2)
    device.add_newobj("objla", "live.object", numinlets=2, numoutlets=1)
    add_msg(device, "mcla", "call create_audio_clip $3")
    device.add_newobj("sala", "prepend setargs", numinlets=1, numoutlets=1)
    device.add_newobj("slala", "select create_audio_clip", numinlets=1, numoutlets=2)
    add_msg(device, "mokla", "load_audio")
    device.add_newobj("loglap", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("loglat", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("loglac", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("logla", "prepend /log", numinlets=1, numoutlets=1)

    # ========================================================================
    # /browser_create y /map_macro -> errores claros (no expuestos a M4L)
    # ========================================================================
    add_msg(device, "merrb",
            "browser no expuesto a Max for Live: arrastra el plugin a mano "
            "en Live o usa un nombre exacto con create_device")
    add_msg(device, "merrm", "map_macro pendiente de implementar en el helper")

    # ========================================================================
    # /diag <track> <device>  (diagnóstico: resuelve 3 paths en paralelo)
    #   Averigua hasta dónde puede navegar live.path en esta instalación.
    # ========================================================================
    device.add_newobj("tdl", "t l l l", numinlets=1, numoutlets=3)
    add_msg(device, "dmp1", "path live_set tracks $1")
    add_msg(device, "dmp2", "path live_set tracks $1 devices $2")
    add_msg(device, "dmp3", "path live_set tracks $1 mixer_device")
    device.add_newobj("dpth1", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("dpth2", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("dpth3", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("dlog1", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("dlog2", "prepend /log", numinlets=1, numoutlets=1)
    device.add_newobj("dlog3", "prepend /log", numinlets=1, numoutlets=1)

    # ========================================================================
    # /diag2 <track> <device>  (prueba `goto` y `get devices` vía live.object)
    # ========================================================================
    device.add_newobj("t2", "t l l", numinlets=1, numoutlets=2)
    add_msg(device, "e2mp1", "goto live_set tracks $1 devices $2")
    device.add_newobj("e2pth1", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("e2log1", "prepend /log", numinlets=1, numoutlets=1)
    add_msg(device, "e2mp2", "path live_set tracks $1")
    device.add_newobj("e2pth2", "live.path", numinlets=1, numoutlets=3)
    device.add_newobj("e2trg", "t b l", numinlets=1, numoutlets=2)
    device.add_newobj("e2obj", "live.object", numinlets=2, numoutlets=1)
    add_msg(device, "e2mc", "get devices")
    device.add_newobj("e2log2", "prepend /log", numinlets=1, numoutlets=1)

    # --- cableado -----------------------------------------------------------
    device.add_line("udp", 0, "route", 0)
    # Paso de audio directo (plugin~ -> plugout~) para no silenciar el Master.
    device.add_line("obj-plugin", 0, "obj-plugout", 0)
    device.add_line("obj-plugin", 1, "obj-plugout", 1)

    # ---------------- create_device ----------------
    device.add_line("route", 0, "t1a", 0)
    device.add_line("t1a", 1, "mp1", 0)          # l (derecho) -> path msg
    device.add_line("t1a", 0, "sa1", 0)          # l (izq.) -> setargs (almacena $n sin emitir)
    device.add_line("sa1", 0, "mc1", 0)
    device.add_line("mp1", 0, "pth1", 0)
    device.add_line("mp1", 0, "logi1", 0)        # traza del mensaje path
    device.add_line("pth1", 0, "trg1", 0)        # id nn -> trigger
    device.add_line("pth1", 0, "logt1", 0)       # traza del id resuelto
    device.add_line("trg1", 1, "obj1", 1)        # l -> inlet derecho (id)
    device.add_line("trg1", 0, "mc1", 0)         # b -> EMITE el call almacenado
    device.add_line("mc1", 0, "obj1", 0)         # call -> inlet izquierdo
    device.add_line("mc1", 0, "logc1", 0)        # traza del call emitido
    device.add_line("obj1", 0, "sl1", 0)         # resultado del call
    device.add_line("obj1", 0, "log1", 0)        # traza de salida de live.object
    device.add_line("sl1", 0, "mok1", 0)         # insert_device OK -> /ok
    device.add_line("sl1", 1, "rerr", 0)         # cualquier otra salida -> /error
    device.add_line("mok1", 0, "rok", 0)

    # ---------------- create_rack ----------------
    device.add_line("route", 1, "t1r", 0)
    device.add_line("t1r", 1, "mpr", 0)
    device.add_line("t1r", 0, "sar", 0)
    device.add_line("sar", 0, "mcr", 0)
    device.add_line("mpr", 0, "pthr", 0)
    device.add_line("mpr", 0, "logir", 0)
    device.add_line("pthr", 0, "trgr", 0)
    device.add_line("trgr", 1, "objr", 1)
    device.add_line("trgr", 0, "mcr", 0)
    device.add_line("mcr", 0, "objr", 0)
    device.add_line("mcr", 0, "logcr", 0)
    device.add_line("objr", 0, "slr", 0)
    device.add_line("objr", 0, "logr", 0)
    device.add_line("slr", 0, "mokr", 0)
    device.add_line("slr", 1, "rerr", 0)
    device.add_line("mokr", 0, "rok", 0)

    # ---------------- chain_create_device ----------------
    device.add_line("route", 2, "tc4", 0)
    device.add_line("tc4", 0, "mcp4", 0)
    device.add_line("tc4", 1, "mz4", 0)          # extrae $2 (el índice)
    device.add_line("mz4", 0, "zl4", 1)          # índice para zl nth (2*rack+3)
    device.add_line("mz4", 0, "logm4", 0)        # traza del índice extraído
    device.add_line("tc4", 2, "sa4", 0)
    device.add_line("sa4", 0, "mc4", 0)          # setargs call insert_device
    device.add_line("mcp4", 0, "pth4", 0)
    device.add_line("mcp4", 0, "logi4", 0)
    device.add_line("pth4", 0, "trg4", 0)
    device.add_line("pth4", 0, "logt4", 0)
    device.add_line("trg4", 1, "obj4a", 1)       # id pista
    device.add_line("trg4", 0, "mc4d", 0)        # bang -> get devices
    device.add_line("mc4d", 0, "obj4a", 0)
    device.add_line("obj4a", 0, "t4", 0)         # devices id.. -> t l b
    device.add_line("obj4a", 0, "logd4", 0)      # traza lista de devices
    device.add_line("t4", 0, "rt4", 0)           # l (lista) -> route devices
    device.add_line("t4", 1, "dl4", 0)           # b (bang) -> delay -> insert_chain
    device.add_line("rt4", 0, "zl4", 0)          # id 4 id 5 -> zl.nth
    device.add_line("rt4", 0, "logrt4", 0)       # traza salida de route
    device.add_line("zl4", 0, "pr4", 0)          # número del rack
    device.add_line("zl4", 0, "logz4", 0)        # traza del id extraído
    device.add_line("pr4", 0, "obj4b", 1)        # id rack
    device.add_line("dl4", 0, "ic4", 0)          # 1ms después -> call insert_chain
    device.add_line("ic4", 0, "obj4b", 0)        # crea una cadena en el rack
    device.add_line("obj4b", 0, "t4c", 0)        # insert_chain id C -> t l b
    device.add_line("obj4b", 0, "logb4", 0)      # traza salida de obj4b
    device.add_line("t4c", 0, "rt4c", 0)         # l -> route insert_chain
    device.add_line("rt4c", 0, "r4c", 0)         # -> route id x
    device.add_line("rt4c", 0, "logrtc", 0)      # traza salida route insert_chain
    device.add_line("r4c", 0, "pr4c", 0)         # id cadena -> prepend id
    device.add_line("r4c", 1, "logr4c", 0)       # traza no-id (descarte)
    device.add_line("pr4c", 0, "obj4c", 1)       # id cadena
    device.add_line("t4c", 1, "dl4c", 0)         # b (bang) -> delay -> call
    device.add_line("dl4c", 0, "mc4", 0)         # bang -> emite call
    device.add_line("mc4", 0, "obj4c", 0)        # call insert_device
    device.add_line("obj4c", 0, "sl4", 0)
    device.add_line("obj4c", 0, "log4", 0)
    device.add_line("sl4", 0, "mok4", 0)
    device.add_line("sl4", 1, "rerr", 0)
    device.add_line("mok4", 0, "rok", 0)

    # ---------------- browser_create / map_macro -> error claro -------------
    device.add_line("route", 3, "merrb", 0)
    device.add_line("merrb", 0, "rerr", 0)
    device.add_line("route", 4, "merrm", 0)
    device.add_line("merrm", 0, "rerr", 0)

    # ---------------- arr_add (route 7) ----------------
    device.add_line("route", 7, "ta3", 0)
    device.add_line("ta3", 0, "map3", 0)          # track -> path msg
    device.add_line("ta3", 1, "sa3", 0)          # time value -> setargs (sin emitir)
    device.add_line("ta3", 2, "log3p", 0)        # traza de la lista recibida
    device.add_line("sa3", 0, "m3a", 0)          # setargs call add
    device.add_line("map3", 0, "pth3", 0)
    device.add_line("pth3", 0, "trg3", 0)        # id param -> trigger
    device.add_line("pth3", 0, "log3t", 0)       # traza id param
    device.add_line("trg3", 1, "obj3v", 1)       # l -> inlet derecho (id param)
    device.add_line("trg3", 0, "m3e", 0)         # b -> call get_automation_envelope
    device.add_line("m3e", 0, "obj3v", 0)
    device.add_line("obj3v", 0, "t3e", 0)        # envelope id -> t l b
    device.add_line("obj3v", 0, "log3v", 0)
    device.add_line("t3e", 0, "rt3e", 0)         # l -> route get_automation_envelope
    device.add_line("t3e", 1, "dl3e", 0)         # b -> delay -> call add
    device.add_line("rt3e", 0, "pr3e", 0)        # id -> prepend
    device.add_line("rt3e", 0, "log3e", 0)       # traza salida route
    device.add_line("pr3e", 0, "obj3e", 1)       # id envelope
    device.add_line("dl3e", 0, "m3a", 0)         # call add time value
    device.add_line("m3a", 0, "obj3e", 0)
    device.add_line("obj3e", 0, "sl3", 0)
    device.add_line("obj3e", 0, "log3a", 0)
    device.add_line("sl3", 0, "mok3", 0)
    device.add_line("sl3", 1, "rerr", 0)
    device.add_line("mok3", 0, "rok", 0)
    device.add_line("log3", 0, "merger", 0)

    # ---------------- arr_clear (route 8) ----------------
    device.add_line("route", 8, "tac", 0)
    device.add_line("tac", 0, "mapc", 0)
    device.add_line("tac", 0, "logcp", 0)
    device.add_line("mapc", 0, "pthc", 0)
    device.add_line("pthc", 0, "trgc", 0)
    device.add_line("pthc", 0, "logct", 0)
    device.add_line("trgc", 1, "objcv", 1)
    device.add_line("trgc", 0, "mcd", 0)
    device.add_line("mcd", 0, "objcv", 0)
    device.add_line("objcv", 0, "slc", 0)
    device.add_line("objcv", 0, "logcv", 0)
    device.add_line("slc", 0, "mokc", 0)
    device.add_line("slc", 1, "rerr", 0)
    device.add_line("mokc", 0, "rok", 0)
    device.add_line("logc", 0, "merger", 0)

    # ---------------- clip_follow_action (route 9) ----------------
    device.add_line("route", 9, "tfa", 0)
    device.add_line("tfa", 0, "mfa", 0)           # track slot -> path msg
    device.add_line("tfa", 1, "safa", 0)          # prop value -> setargs
    device.add_line("tfa", 2, "saga", 0)          # prop -> setargs (para el get)
    device.add_line("tfa", 3, "logfap", 0)        # traza de la lista recibida
    device.add_line("safa", 0, "mcfa", 0)         # setargs -> set follow_action
    device.add_line("saga", 0, "mgfa", 0)         # setargs -> get follow_action
    device.add_line("mfa", 0, "pthfa", 0)
    device.add_line("mfa", 0, "logfat", 0)        # traza del mensaje path
    device.add_line("pthfa", 0, "trgfa", 0)       # id clip -> trigger
    device.add_line("pthfa", 0, "logfap", 0)      # traza del id resuelto
    device.add_line("trgfa", 1, "objfa", 1)       # l -> inlet derecho (id clip)
    device.add_line("trgfa", 0, "mcfa", 0)        # b -> EMITE el set almacenado
    device.add_line("mcfa", 0, "objfa", 0)        # set -> inlet izquierdo
    device.add_line("mcfa", 0, "logfac", 0)       # traza del set emitido
    device.add_line("trgfa", 0, "dlfa", 0)        # b -> delay -> get
    device.add_line("dlfa", 0, "mgfa", 0)         # después de setear, get
    device.add_line("mgfa", 0, "objfa", 0)        # get -> inlet izquierdo
    device.add_line("objfa", 0, "rtfa", 0)        # "<propiedad> <valor>"
    device.add_line("objfa", 0, "logfag", 0)      # traza de salida de live.object
    device.add_line("rtfa", 0, "mokfa", 0)        # propiedad OK -> /ok
    device.add_line("rtfa", 1, "rerr", 0)         # cualquier otra salida -> /error
    device.add_line("mokfa", 0, "rok", 0)

    # ---------------- load_audio (route 10) ----------------
    device.add_line("route", 10, "tla", 0)
    device.add_line("tla", 0, "mlap", 0)          # track slot -> path msg
    device.add_line("tla", 1, "sala", 0)          # file_path -> setargs (sin emitir)
    device.add_line("tla", 2, "loglap", 0)        # traza de la lista recibida
    device.add_line("sala", 0, "mcla", 0)         # setargs -> call create_audio_clip
    device.add_line("mlap", 0, "pthla", 0)
    device.add_line("pthla", 0, "trgla", 0)       # id clip_slot -> trigger
    device.add_line("pthla", 0, "loglat", 0)      # traza del id resuelto
    device.add_line("trgla", 1, "objla", 1)       # l -> inlet derecho (id)
    device.add_line("trgla", 0, "mcla", 0)        # b -> EMITE el call almacenado
    device.add_line("mcla", 0, "objla", 0)        # call -> inlet izquierdo
    device.add_line("mcla", 0, "loglac", 0)       # traza del call emitido
    device.add_line("objla", 0, "slala", 0)       # resultado del call
    device.add_line("objla", 0, "logla", 0)       # traza de salida de live.object
    device.add_line("slala", 0, "mokla", 0)       # create_audio_clip OK -> /ok
    device.add_line("slala", 1, "rerr", 0)        # cualquier otra salida -> /error
    device.add_line("mokla", 0, "rok", 0)

    # ---------------- diag ----------------
    device.add_line("route", 5, "tdl", 0)
    device.add_line("tdl", 2, "dmp1", 0)
    device.add_line("tdl", 1, "dmp2", 0)
    device.add_line("tdl", 0, "dmp3", 0)
    device.add_line("dmp1", 0, "dpth1", 0)
    device.add_line("dmp2", 0, "dpth2", 0)
    device.add_line("dmp3", 0, "dpth3", 0)
    device.add_line("dpth1", 0, "dlog1", 0)
    device.add_line("dpth2", 0, "dlog2", 0)
    device.add_line("dpth3", 0, "dlog3", 0)

    # ---------------- diag2 ----------------
    device.add_line("route", 6, "t2", 0)
    device.add_line("t2", 1, "e2mp1", 0)
    device.add_line("t2", 0, "e2mp2", 0)
    device.add_line("e2mp1", 0, "e2pth1", 0)
    device.add_line("e2pth1", 0, "e2log1", 0)
    device.add_line("e2mp2", 0, "e2pth2", 0)
    device.add_line("e2pth2", 0, "e2trg", 0)
    device.add_line("e2trg", 1, "e2obj", 1)
    device.add_line("e2trg", 0, "e2mc", 0)
    device.add_line("e2mc", 0, "e2obj", 0)
    device.add_line("e2obj", 0, "e2log2", 0)

    # ---------------- salida de respuestas (una sola conexión a udpsend) ----
    device.add_line("rok", 0, "merger", 0)
    device.add_line("rerr", 0, "merger", 0)
    device.add_line("logi1", 0, "merger", 0)
    device.add_line("logt1", 0, "merger", 0)
    device.add_line("logc1", 0, "merger", 0)
    device.add_line("log1", 0, "merger", 0)
    device.add_line("logir", 0, "merger", 0)
    device.add_line("logcr", 0, "merger", 0)
    device.add_line("logr", 0, "merger", 0)
    device.add_line("logi4", 0, "merger", 0)
    device.add_line("logt4", 0, "merger", 0)
    device.add_line("logd4", 0, "merger", 0)
    device.add_line("logrt4", 0, "merger", 0)
    device.add_line("logm4", 0, "merger", 0)
    device.add_line("logz4", 0, "merger", 0)
    device.add_line("logb4", 0, "merger", 0)
    device.add_line("logrtc", 0, "merger", 0)
    device.add_line("logr4c", 0, "merger", 0)
    device.add_line("logc4", 0, "merger", 0)
    device.add_line("log4", 0, "merger", 0)
    device.add_line("dlog1", 0, "merger", 0)
    device.add_line("dlog2", 0, "merger", 0)
    device.add_line("dlog3", 0, "merger", 0)
    device.add_line("e2log1", 0, "merger", 0)
    device.add_line("e2log2", 0, "merger", 0)
    device.add_line("log3p", 0, "merger", 0)
    device.add_line("log3t", 0, "merger", 0)
    device.add_line("log3v", 0, "merger", 0)
    device.add_line("log3e", 0, "merger", 0)
    device.add_line("log3a", 0, "merger", 0)
    device.add_line("logcp", 0, "merger", 0)
    device.add_line("logct", 0, "merger", 0)
    device.add_line("logcv", 0, "merger", 0)
    device.add_line("logfap", 0, "merger", 0)
    device.add_line("logfat", 0, "merger", 0)
    device.add_line("logfac", 0, "merger", 0)
    device.add_line("logfag", 0, "merger", 0)
    device.add_line("loglap", 0, "merger", 0)
    device.add_line("loglat", 0, "merger", 0)
    device.add_line("loglac", 0, "merger", 0)
    device.add_line("logla", 0, "merger", 0)
    device.add_line("merger", 0, "udpsend", 0)

    for w in device.validate():
        print("WARN:", w)
    device.build(OUT)
    return OUT


if __name__ == "__main__":
    print("wrote", build())