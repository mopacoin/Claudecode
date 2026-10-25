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

        def _same_origin(self):
            """Schutz gegen Fremdseiten (CSRF): Browser schicken bei fremden Seiten einen anderen Origin mit,
            und JSON-Anfragen von fremden Seiten erzwingen einen CORS-Preflight, den wir nie erlauben."""
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                self._send(403, {"error": "fremde Herkunft"})
                return False
            if self.command == "POST" and "application/json" not in (self.headers.get("Content-Type") or ""):
                self._send(415, {"error": "Content-Type application/json erforderlich"})
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
            elif u.path in ("/api/stats", "/api/energy"):
                try:
                    days = int(q.get("days", ["30" if u.path == "/api/stats" else "7"])[0])
                except ValueError:
                    days = 7
                self._send(200, ctl.daily_stats(days) if u.path == "/api/stats" else ctl.energy(days))
            elif u.path == "/api/report":  # letzter gesendeter Bericht + aktuelle Auswertung
                title, text = ctl.daily_report()
                self._send(200, {"last": ctl.report_last, "preview": {"title": title, "text": text}})
            elif u.path == "/api/history":
                try:
                    off = max(0, min(24 * 30, int(q.get("offset", ["0"])[0])))
                except ValueError:
                    off = 0
                self._send(200, ctl.history(rng, off))
            elif u.path == "/api/settings":
                self._send(200, {"values": ctl.get_settings(), "spec": settings.SPEC,
                                 "presets": ctl.presets(), "colors": settings.PALETTE, "calibration": ctl.cal, "auth": bool(token)})
            elif u.path == "/api/events":
                if "range" in q:  # für den Verlauf: alle Ereignisse des Zeitraums aus dem dauerhaften Protokoll
                    from datetime import datetime, timedelta
                    from .controller import RANGES
                    since = (datetime.now() - timedelta(hours=RANGES.get(rng, 1))).isoformat(timespec="seconds")
                    self._send(200, ctl._events_since(since)[-1000:])
                else:
                    self._send(200, list(ctl.events)[-200:][::-1])
            elif u.path == "/api/export.csv":
                self._send(200, ctl.export_csv(rng).encode(), "text/csv; charset=utf-8",
                           {"Content-Disposition": f'attachment; filename="grow-{rng}.csv"'})
            elif u.path == "/api/ping":  # prüft nur den Token (für den Login der Oberfläche)
                if self._auth():
                    self._send(200, {"ok": True})
            elif u.path == "/ui_extra.js":
                with open(os.path.join(os.path.dirname(__file__), "ui_extra.js"), "rb") as f:
                    self._send(200, f.read(), "text/javascript; charset=utf-8")
            elif u.path in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._same_origin() or not self._auth():
                return
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "ungültiges JSON"})
            if body is None:
                return self._send(413, {"error": "zu groß"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "JSON-Objekt erwartet"})
            parts = urlparse(self.path).path.strip("/").split("/")
            if parts == ["api", "settings"]:
                new, errors = ctl.update_settings(body)
                return self._send(400, {"errors": errors}) if errors else self._send(200, {"values": new})
            if parts == ["api", "presets"]:
                pid, errors = ctl.save_preset(body)
                return self._send(400, {"errors": errors}) if errors else self._send(200, {"id": pid, "presets": ctl.presets()})
            if len(parts) == 4 and parts[:2] == ["api", "presets"] and parts[3] == "apply":
                new, errors = ctl.apply_preset(parts[2])
                return self._send(400, {"errors": errors}) if errors else self._send(200, {"values": new, "presets": ctl.presets()})
            if parts == ["api", "calibration"]:
                pts, err = ctl.set_calibration(body.get("points"))
                return self._send(400, {"error": err}) if err else self._send(200, {"calibration": pts})
            if parts == ["api", "calibration", "sweep"]:
                try:
                    ctl.stop_sweep() if body.get("action") == "stop" else ctl.start_sweep()
                    return self._send(200, ctl.status())
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if len(parts) == 4 and parts[:2] == ["api", "servo"] and parts[3] == "angle":
                try:
                    ctl.servo_manual(parts[2], body.get("angle"))
                    return self._send(200, ctl.status())
                except ValueError:
                    return self._send(400, {"error": "Winkel 0…180 und stufenloser Servo erforderlich"})
            if parts == ["api", "effect", "reset"]:
                ctl.reset_effect()
                return self._send(200, ctl.status())
            if parts == ["api", "irrigation", "run"]:
                try:
                    ctl.water_now()
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, ctl.status())
            if parts == ["api", "report", "send"]:  # Tagesbericht sofort senden
                err = ctl.send_report_now()
                return self._send(400, {"error": err}) if err else self._send(200, {"ok": True})
            if parts == ["api", "energy"]:
                err = ctl.set_energy(body)
                return self._send(400, {"error": err}) if err else self._send(200, ctl.energy(int(body.get("days", 7))))
            if parts == ["api", "notify", "test"]:
                err = ctl.notifier.send_now("Growcontroller", "Test-Benachrichtigung – kommt an ✓")
                return self._send(400, {"error": err}) if err else self._send(200, {"ok": True})
            if len(parts) == 3 and parts[:2] == ["api", "ggs"]:
                try:
                    ctl.ggs_mode(parts[2], body)
                    return self._send(200, ctl.status())
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if len(parts) == 3 and parts[:2] == ["api", "timer"]:
                try:
                    ctl.set_timer(parts[2], body)
                    return self._send(200, ctl.status())
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if len(parts) == 3 and parts[:2] == ["api", "dim"]:
                try:
                    ctl.set_dim(parts[2], body.get("pct"))
                    return self._send(200, ctl.status())
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if len(parts) == 3 and parts[:2] == ["api", "output"]:
                try:
                    ctl.set_mode(parts[2], body["mode"])
                    return self._send(200, ctl.status())
                except (ValueError, KeyError, TypeError):
                    return self._send(400, {"error": "bad request"})
            self._send(404, {"error": "not found"})

        def do_DELETE(self):
            if not self._same_origin() or not self._auth():
                return
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "presets"]:
                if ctl.delete_preset(parts[2]):
                    return self._send(200, {"presets": ctl.presets(), "values": ctl.get_settings()})
                return self._send(404, {"error": "Zyklus nicht gefunden oder eingebaut"})
            self._send(404, {"error": "not found"})

    return H


def serve(ctl, host, port, token=None):
    srv = ThreadingHTTPServer((host, port), make_handler(ctl, token))
    srv.daemon_threads = True
    return srv
