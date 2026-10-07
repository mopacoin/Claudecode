import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INDEX = os.path.join(os.path.dirname(__file__), "index.html")


def make_handler(ctl, token):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/api/status":
                self._send(200, ctl.status())
            elif self.path == "/api/history":
                self._send(200, list(ctl.history)[-720:])
            elif self.path in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            # POST /api/output/<name>  {"mode": "auto|on|off"}
            if token and self.headers.get("X-Token") != token:
                return self._send(401, {"error": "token"})
            parts = self.path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "output"]:
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    ctl.set_mode(parts[2], json.loads(self.rfile.read(n))["mode"])
                    return self._send(200, ctl.status())
                except (ValueError, KeyError):
                    return self._send(400, {"error": "bad request"})
            self._send(404, {"error": "not found"})

    return H


def serve(ctl, host, port, token=None):
    srv = ThreadingHTTPServer((host, port), make_handler(ctl, token))
    srv.daemon_threads = True
    return srv
