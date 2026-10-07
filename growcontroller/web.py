import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import settings

INDEX = os.path.join(os.path.dirname(__file__), "index.html")


def make_handler(ctl, token):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, code, body, ctype="application/json", headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _auth(self):
            if token and self.headers.get("X-Token") != token:
                self._send(401, {"error": "Token erforderlich"})
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n <= 65536 else None

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            rng = q.get("range", ["1h"])[0]
            if u.path == "/api/status":
                self._send(200, ctl.status())
            elif u.path == "/api/history":
                self._send(200, ctl.history(rng))
            elif u.path == "/api/settings":
                self._send(200, {"values": ctl.get_settings(), "spec": settings.SPEC,
                                 "presets": settings.PRESETS, "auth": bool(token)})
            elif u.path == "/api/events":
                self._send(200, list(ctl.events)[-200:][::-1])
            elif u.path == "/api/export.csv":
                self._send(200, ctl.export_csv(rng).encode(), "text/csv; charset=utf-8",
                           {"Content-Disposition": f'attachment; filename="grow-{rng}.csv"'})
            elif u.path == "/api/ping":  # prüft nur den Token (für den Login der Oberfläche)
                if self._auth():
                    self._send(200, {"ok": True})
            elif u.path in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "ungültiges JSON"})
            if body is None:
                return self._send(413, {"error": "zu groß"})
            parts = urlparse(self.path).path.strip("/").split("/")
            if parts == ["api", "settings"]:
                new, errors = ctl.update_settings(body)
                return self._send(400, {"errors": errors}) if errors else self._send(200, {"values": new})
            if parts == ["api", "irrigation", "run"]:
                ctl.water_now()
                return self._send(200, ctl.status())
            if len(parts) == 3 and parts[:2] == ["api", "output"]:
                try:
                    ctl.set_mode(parts[2], body["mode"])
                    return self._send(200, ctl.status())
                except (ValueError, KeyError, TypeError):
                    return self._send(400, {"error": "bad request"})
            self._send(404, {"error": "not found"})

    return H


def serve(ctl, host, port, token=None):
    srv = ThreadingHTTPServer((host, port), make_handler(ctl, token))
    srv.daemon_threads = True
    return srv
