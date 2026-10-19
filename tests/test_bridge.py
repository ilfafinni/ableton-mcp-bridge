#!/usr/bin/env python3
"""Prueba del bridge MCP con un AbletonOSC simulado (sin Live).

Levanta un falso AbletonOSC en el 11000/11001 y un falso helper M4L en el
9001/9002, lanza mcp_server.py por stdin, y verifica: initialize, tools/list,
ping, list_song, set_tempo y la memoria de sesión.

Uso:  python tests/test_bridge.py
"""

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "..", "bridge", "mcp_server.py")
PY = sys.executable

FAKE_TRACKS = [
    {"name": "Voz", "devices": [{"name": "EQ Eight", "class": "Eq8", "params": 8}]},
    {"name": "Bajo", "devices": [{"name": "Compressor", "class": "Compressor", "params": 12}]},
]


def osc_reply(address, *args):
    """Construye una respuesta OSC bien formada (dirección + typetag + datos)."""
    b = bytearray(address.encode("utf-8") + b"\0")
    while len(b) % 4:
        b += b"\0"
    tt = b","
    for a in args:
        if isinstance(a, bool):
            tt += b"i"
        elif isinstance(a, int):
            tt += b"i"
        elif isinstance(a, float):
            tt += b"f"
        else:
            tt += b"s"
    b += tt + b"\0"
    while len(b) % 4:
        b += b"\0"
    for a in args:
        if isinstance(a, bool):
            b += struct.pack(">i", 1 if a else 0)
        elif isinstance(a, int):
            b += struct.pack(">i", a)
        elif isinstance(a, float):
            b += struct.pack(">f", a)
        else:
            s = a.encode("utf-8") + b"\0"
            while len(s) % 4:
                s += b"\0"
            b += s
    return bytes(b)


class FakeAbletonOSC:
    """Responde a las consultas de AbletonOSC que usa el bridge."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 11000))
        self.sock.settimeout(0.5)
        self.tempo = 123.0
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            from pythonosc.osc_message import OscMessage

            msg = OscMessage(data)
            addr_str = msg.address
            reply_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            target = ("127.0.0.1", 11001)
            if addr_str == "/live/test":
                reply_sock.sendto(osc_reply("/live/test", 1), target)
            elif addr_str == "/live/song/get/tempo":
                reply_sock.sendto(osc_reply("/live/song/get/tempo", self.tempo), target)
            elif addr_str == "/live/song/get/num_tracks":
                reply_sock.sendto(osc_reply("/live/song/get/num_tracks", len(FAKE_TRACKS)), target)
            elif addr_str == "/live/song/get/track_names":
                reply_sock.sendto(osc_reply("/live/song/get/track_names",
                                            *[t["name"] for t in FAKE_TRACKS]), target)
            elif addr_str == "/live/track/get/num_devices":
                t = int(msg.params[0])
                reply_sock.sendto(osc_reply("/live/track/get/num_devices",
                                            t, len(FAKE_TRACKS[t]["devices"])), target)
            elif addr_str == "/live/device/get/name":
                t, d = int(msg.params[0]), int(msg.params[1])
                reply_sock.sendto(osc_reply("/live/device/get/name", t, d,
                                            FAKE_TRACKS[t]["devices"][d]["name"]), target)
            elif addr_str == "/live/device/get/class_name":
                t, d = int(msg.params[0]), int(msg.params[1])
                reply_sock.sendto(osc_reply("/live/device/get/class_name", t, d,
                                            FAKE_TRACKS[t]["devices"][d]["class"]), target)
            elif addr_str in ("/live/device/get/parameters/name",
                              "/live/device/get/parameters/value",
                              "/live/device/get/parameters/min",
                              "/live/device/get/parameters/max",
                              "/live/device/get/parameters/is_quantized"):
                t, d = int(msg.params[0]), int(msg.params[1])
                n = FAKE_TRACKS[t]["devices"][d]["params"]
                if addr_str.endswith("/name"):
                    values = [f"Param {i}" for i in range(n)]
                elif addr_str.endswith("/min"):
                    values = [0.0] * n
                elif addr_str.endswith("/max"):
                    values = [1.0] * n
                elif addr_str.endswith("/is_quantized"):
                    values = [0] * n
                else:
                    values = [0.5] * n
                reply_sock.sendto(osc_reply(addr_str, t, d, *values), target)
            elif addr_str == "/live/application/get/version":
                reply_sock.sendto(osc_reply("/live/application/get/version", 12, 3), target)
            elif addr_str == "/live/browser/load_by_query":
                query = str(msg.params[1]).lower()
                found = "Serum VST" if "serum" in query else ""
                reply_sock.sendto(osc_reply("/live/browser/load_by_query",
                                            int(msg.params[0]), str(msg.params[1]), found), target)
            elif addr_str == "/live/browser/list_categories":
                reply_sock.sendto(osc_reply("/live/browser/list_categories",
                                            "instruments", "plugins", "audio_effects"), target)
            elif addr_str == "/live/inspect/track":
                t = int(msg.params[0])
                # Árbol simulado: rack con cadena y un Eq8 con bandas.
                eq8_params = [
                    ("1 Frequency", 200.0, 20.0, 20000.0, 0),
                    ("1 Gain", 0.0, -15.0, 15.0, 0),
                    ("1 Q", 1.0, 0.1, 10.0, 0),
                    ("1 On", 1, 0, 1, 1),
                    ("2 Frequency", 1000.0, 20.0, 20000.0, 0),
                    ("2 Gain", -3.0, -15.0, 15.0, 0),
                ]
                tree = {
                    "name": FAKE_TRACKS[t]["name"],
                    "is_midi": t == 1,
                    "devices": [
                        {"index": 0, "name": "Audio Effect Rack",
                         "class_name": "AudioEffectGroupDevice", "type": 0,
                         "can_have_chains": True, "parameters": [],
                         "chains": [
                             {"index": 0, "name": "Chain 1",
                              "devices": [
                                  {"index": 0, "name": "EQ Eight",
                                   "class_name": "Eq8", "type": 0,
                                   "can_have_chains": False,
                                   "parameters": [
                                       {"index": i, "name": n, "value": v,
                                        "min": mn, "max": mx, "is_quantized": q}
                                       for i, (n, v, mn, mx, q) in enumerate(eq8_params)
                                   ],
                                   "chains": []}
                              ]}
                         ]},
                        {"index": 1, "name": "Compressor",
                         "class_name": "Compressor", "type": 0,
                         "can_have_chains": False, "parameters": [], "chains": []},
                    ],
                }
                payload = json.dumps(tree)
                reply_sock.sendto(osc_reply("/live/inspect/track", t, payload), target)
            reply_sock.close()

    def close(self):
        self.stop = True
        self.sock.close()


class FakeHelper:
    """Falso helper M4L: responde /ok a todo en el 9001 -> 9002."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 9001))
        self.sock.settimeout(0.5)
        self.stop = False
        self.received = []

    def run(self):
        from pythonosc.osc_message import OscMessage

        while not self.stop:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            reply = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                msg = OscMessage(data)
                addr = msg.address
                parts = " ".join(str(p) for p in msg.params)
                self.received.append(f"{addr} {parts}")
            except Exception:
                addr_str = data.decode("utf-8", errors="replace")
                self.received.append(addr_str)
            op = addr.split(" ", 1)[0].lstrip("/")
            reply.sendto(f"/ok {op}".encode(), ("127.0.0.1", 9002))
            reply.close()

    def close(self):
        self.stop = True
        self.sock.close()


class MCPClient:
    def __init__(self, proc):
        self.proc = proc

    def send(self, obj):
        body = json.dumps(obj).encode()
        self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.proc.stdin.flush()
        return self.read()

    def read(self):
        headers = {}
        while True:
            line = self.proc.stdout.readline()
            if line == b"\r\n":
                break
            key, _, val = line.decode().strip().partition(":")
            headers[key.lower()] = val
        body = self.proc.stdout.read(int(headers["content-length"]))
        return json.loads(body)


def main():
    fake_osc = FakeAbletonOSC()
    fake_helper = FakeHelper()
    threading.Thread(target=fake_osc.run, daemon=True).start()
    threading.Thread(target=fake_helper.run, daemon=True).start()
    time.sleep(0.3)

    env = dict(os.environ)
    env["ABLETON_OSC_PORT"] = "11000"
    proc = subprocess.Popen(
        [PY, BRIDGE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env,
    )
    client = MCPClient(proc)
    failures = []

    def check(label, cond, extra=""):
        status = "OK " if cond else "FAIL"
        print(f"  [{status}] {label} {extra}")
        if not cond:
            failures.append(label)

    try:
        resp = client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05", "capabilities": {}}})
        check("initialize", resp["result"]["serverInfo"]["name"] == "ableton-assistant-bridge")

        resp = client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        expected = {"ping", "list_song", "set_parameter", "insert_device", "create_rack",
                    "chain_create_device", "map_macro", "load_plugin", "set_follow_action",
                    "session_summary", "session_note", "session_clear"}
        missing = expected - set(tools)
        check("tools/list", not missing, f"faltan: {missing or 'ninguna'}")

        resp = client.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "ping", "arguments": {}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("ping", out["connected"] is True, str(out.get("reply")))

        resp = client.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "list_song", "arguments": {}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("list_song", len(out["tracks"]) == 2 and out["tracks"][0]["name"] == "Voz",
              f"pistas: {[t['name'] for t in out['tracks']]}")

        resp = client.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "list_song", "arguments": {"depth": 2}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        track0 = out["tracks"][0]
        rack = track0["devices"][0]
        chain_dev = rack["chains"][0]["devices"][0]
        check("list_song depth (árbol anidado)",
              track0["name"] == "Voz"
              and rack["class_name"] == "AudioEffectGroupDevice"
              and chain_dev["class_name"] == "Eq8",
              f"rack={rack.get('name')} chain_dev={chain_dev.get('name')}")
        check("list_song depth (eq8 compacto)",
              track0.get("eq8") and track0["eq8"][0]["bands"][0]["band"] == 1
              and track0["eq8"][0]["bands"][0]["gain"] == 0.0,
              f"eq8={track0.get('eq8')}")

        resp = client.send({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                            "params": {"name": "get_tempo", "arguments": {}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("get_tempo", abs(out["bpm"] - 123.0) < 0.01, f"bpm={out['bpm']}")

        resp = client.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": "set_parameter", "arguments": {
                                "track_index": 0, "device_index": 0, "parameter_index": 2, "value01": 0.8}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("set_parameter", out.get("ok") is True and "parameter_name" in out,
              f"-> {out.get('value_normalized')}")

        resp = client.send({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                            "params": {"name": "insert_device", "arguments": {
                                "track_index": 0, "device_class": "Reverb"}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("insert_device (con respuesta del helper)", out.get("ok") is True,
              str(out.get("helper")))

        resp = client.send({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                            "params": {"name": "create_rack", "arguments": {
                                "track_index": 0, "kind": "audio"}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("create_rack", out.get("ok") is True and out.get("kind") == "audio")

        resp = client.send({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                            "params": {"name": "create_rack", "arguments": {
                                "track_index": 0, "kind": "bogus"}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("create_rack kind inválido", "error" in out)

        resp = client.send({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                            "params": {"name": "chain_create_device", "arguments": {
                                "track_index": 0, "rack_index": 2, "device_class": "Compressor"}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("chain_create_device", out.get("ok") is True)

        resp = client.send({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                            "params": {"name": "map_macro", "arguments": {
                                "track_index": 0, "rack_index": 2,
                                "chain_device_index": 0, "parameter_index": 0, "macro_index": 0}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("map_macro", out.get("ok") is True)

        resp = client.send({"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                            "params": {"name": "load_plugin", "arguments": {
                                "track_index": 0, "query": "Serum"}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("load_plugin", out.get("ok") is True and out.get("loaded") == "Serum VST")

        fake_helper.received.clear()
        resp = client.send({"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                            "params": {"name": "set_follow_action", "arguments": {
                                "track_index": 0, "clip_index": 1, "action": 3, "time": 8.0}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("set_follow_action (action+time)", out.get("ok") is True and out.get("action") == 3,
              f"-> {out.get('action')} time={out.get('time')}")
        check("set_follow_action args al helper",
              any(a.startswith("/clip_follow_action 0 1 follow_action 3") for a in fake_helper.received)
              and any(a.startswith("/clip_follow_action 0 1 follow_action_time 8.0") for a in fake_helper.received),
              f"recibidos: {fake_helper.received}")

        fake_helper.received.clear()
        resp = client.send({"jsonrpc": "2.0", "id": 15, "method": "tools/call",
                            "params": {"name": "set_follow_action", "arguments": {
                                "track_index": 0, "clip_index": 0, "action": 7}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("set_follow_action rango inválido", "error" in out)

        resp = client.send({"jsonrpc": "2.0", "id": 16, "method": "tools/call",
                            "params": {"name": "session_note", "arguments": {
                                "text": "La voz es de Laura"}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("session_note", out.get("ok") is True)

        resp = client.send({"jsonrpc": "2.0", "id": 17, "method": "tools/call",
                            "params": {"name": "session_summary", "arguments": {}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        actions = [a["action"] for a in out["log"]]
        check("session_summary (memoria)", "set_parameter" in actions and "insert_device" in actions
              and "set_follow_action" in actions
              and any("Laura" in n["text"] for n in out["notes"]),
              f"log={actions}")

        resp = client.send({"jsonrpc": "2.0", "id": 18, "method": "tools/call",
                            "params": {"name": "session_clear", "arguments": {}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("session_clear", out.get("ok") is True)

        resp = client.send({"jsonrpc": "2.0", "id": 19, "method": "tools/call",
                            "params": {"name": "session_summary", "arguments": {}}})
        out = json.loads(resp["result"]["content"][0]["text"])
        check("session vacía tras clear", len(out["log"]) == 0 and len(out["notes"]) == 0)

        # tempo server (9003): el audio_server pide el BPM aquí
        ts = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ts.settimeout(2.0)
        ts.sendto(b"tempo", ("127.0.0.1", 9003))
        data, _ = ts.recvfrom(1024)
        ts.close()
        check("tempo server 9003", abs(float(data) - 123.0) < 0.01, f"bpm={data}")

    finally:
        proc.kill()
        fake_osc.close()
        fake_helper.close()

    print()
    if failures:
        print(f"FALLARON: {failures}")
        return 1
    print("TODAS LAS PRUEBAS PASARON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
