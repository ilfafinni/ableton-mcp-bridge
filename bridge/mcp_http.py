import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_server as m

HOST = "127.0.0.1"
PORT = 9004
SESSION_ID = "ableton-bridge-1"
LOCAL_TOKEN = os.environ.get("ABLETON_MCP_TOKEN", "")
LOCAL_TOKEN_HEADER = "x-ableton-token"


class QuietServer(ThreadingHTTPServer):
    """Silencia los tracebacks cuando el cliente cierra la conexión
    (openmode/opencode corta el keep-alive al terminar)."""

    def handle_error(self, request, client_address):
        import traceback

        _typ, exc, _tb = sys.exc_info()
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        if isinstance(exc, OSError) and exc.errno in (10054, 10053, 10038):
            return
        if isinstance(exc, socket.timeout):
            return
        sys.stderr.write(f"[mcp-http] error con {client_address}:\n")
        traceback.print_exc(file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[mcp-http] " + (fmt % args) + "\n")

    def _send_json(self, status, payload):
        if payload == "" or payload is None:
            body = b""
        else:
            body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Content-Length", "0")
        self.send_header("Mcp-Session-Id", SESSION_ID)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self):
        if not LOCAL_TOKEN:
            return True
        return self.headers.get(LOCAL_TOKEN_HEADER) == LOCAL_TOKEN

    def do_POST(self):
        if not self._authorized():
            self._send_json(403, {"jsonrpc": "2.0", "error": {"code": -32001, "message": "forbidden"}})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}})
            return

        method = msg.get("method")
        if method == "initialize":
            reply = m._handle_request(msg)
            self._send_json(200, reply)
        elif method == "notifications/initialized":
            self._send_json(202, "")
        elif method and (method.startswith("notifications/") or method.startswith("$/")):
            self._send_json(202, "")
        else:
            reply = m._handle_request(msg)
            if reply is None:
                self._send_json(202, "")
            else:
                self._send_json(200, reply)

    def do_GET(self):
        if not self._authorized():
            self._send_json(403, {"jsonrpc": "2.0", "error": {"code": -32001, "message": "forbidden"}})
            return
        self._send_json(405, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "GET not supported"}})


def main():
    threading.Thread(target=m._tempo_server, daemon=True, name="tempo-server").start()
    # Detect MIDI Remote Script (must be called here since mcp_http is the entry point)
    if m.MIDI_REMOTE_AVAILABLE:
        m._detect_midi_remote()
    try:
        server = QuietServer((HOST, PORT), Handler)
        sys.stderr.write(f"[mcp-http] serving MCP HTTP on http://{HOST}:{PORT}/mcp\n")
        sys.stderr.flush()
        server.serve_forever()
    except OSError as e:
        if e.errno == 10048 or "Address already in use" in str(e):
            sys.stderr.write(f"[mcp-http] ERROR: port {PORT} already in use\n")
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()