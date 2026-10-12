"""Push-Benachrichtigungen, Bodenwerte im Verlauf, Ersatzsensor, Crop Steering."""
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from growcontroller import controller, notify, rules

LIGHT = {"enabled": True, "on": "06:00", "off": "00:00"}
SC = {"target": 30, "p1_delay_min": 60, "shot_s": 20, "gap_min": 20, "p2_drop": 3, "stop_min": 120, "max_shots": 5}


class Fixed:
    """Sensor mit festen Werten; error=True -> Ausfall."""
    def __init__(self, **v): self.v, self.error = v, False
    def read(self):
        if self.error:
            raise RuntimeError("weg")
        return dict(self.v)


def make(tmp, **extra):
    cfg = {"interval_s": 5, "sensors": {"air": {"driver": "sim"}}, "outputs": {}, "light": dict(LIGHT),
           "climate": {}, "irrigation": {"enabled": False, "interval_min": 60, "duration_s": 5}, **extra}
    return controller.Controller(cfg, tmp)


class TestNotify(unittest.TestCase):
    def test_ntfy_delivery_and_dedupe(self):
        got = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                got.append((self.path, self.headers["Title"], self.rfile.read(int(self.headers["Content-Length"])).decode()))
                self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        n = notify.Notifier({"ntfy": {"url": f"http://127.0.0.1:{srv.server_port}", "topic": "grow-test"}, "repeat_min": 30})
        self.assertTrue(n.send("Growcontroller", "Temperatur 33 °C über 32 °C"))
        self.assertFalse(n.send("Growcontroller", "Temperatur 33 °C über 32 °C"))   # gleiche Meldung: nicht nochmal
        for _ in range(60):
            if got: break
            time.sleep(0.05)
        srv.shutdown()
        self.assertEqual(got[0], ("/grow-test", "Growcontroller", "Temperatur 33 °C über 32 °C"))
        self.assertEqual(n.channels(), ["ntfy"])

    def test_not_configured(self):
        n = notify.Notifier({})
        self.assertFalse(n.send("x", "y"))
        self.assertIn("Keine Benachrichtigung", n.send_now("x", "y"))

    def test_alarm_event_triggers_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = make(tmp)
            sent = []
            ctl.notifier.send = lambda title, msg, prio="high": sent.append((msg, prio))
            ctl.event("alarm", "Gerät zuluft nicht erreichbar")
            ctl.event("alarm", "Behoben: Gerät zuluft nicht erreichbar")
            ctl.event("output", "fan: EIN")
            self.assertEqual(sent, [("Gerät zuluft nicht erreichbar", "high"), ("Behoben: Gerät zuluft nicht erreichbar", "default")])


class TestSoilHistory(unittest.TestCase):
    def test_soil_columns_in_csv_and_after_restart(self):
        import csv, glob, os
        with tempfile.TemporaryDirectory() as tmp:
            ctl = make(tmp, sensors={"air": {"driver": "sim"}, "ggs": {"driver": "sim", "prefix": "ggs_"}})
            ctl.sensors["ggs"] = ("ggs_", Fixed(soil_hum=12.5, soil_ec=1.9))
            ctl.step(datetime(2026, 10, 13, 12, 0))
            path = sorted(glob.glob(os.path.join(tmp, "log-*.csv")))[-1]
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual((rows[0]["ggs_soil_hum"], rows[0]["ggs_soil_ec"]), ("12.5", "1.9"))
            again = make(tmp)
            self.assertEqual(again._soil_cols, ["ggs_soil_ec", "ggs_soil_hum"])   # Spalten weiterführen
            self.assertIn("ggs_soil_ec", again.export_csv("7d").splitlines()[0])


class TestFallback(unittest.TestCase):
    def test_takes_over_and_hands_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = make(tmp, sensors={"air": {"driver": "sim"}, "ggs": {"driver": "sim", "prefix": "ggs_", "fallback": True, "label": "GGS 1"}})
            main, ggs = Fixed(temp=24.0, hum=60.0), Fixed(temp=22.0, hum=55.0)
            ctl.sensors = {"air": ("", main), "ggs": ("ggs_", ggs)}
            ctl.started -= 1000
            ctl.read_sensors()
            self.assertEqual((ctl.readings["temp"], ctl.fallback), (24.0, None))
            main.error = True
            ctl.last_ok -= controller.FALLBACK_S + 1           # Hauptsensor seit > 90 s weg
            ctl.read_sensors()
            self.assertEqual((ctl.readings["temp"], ctl.readings["hum"], ctl.fallback), (22.0, 55.0, "ggs"))
            self.assertFalse(ctl.stale())
            ctl._check_alarms()
            self.assertIn("sensor_fallback", ctl.alarms)
            main.error = False
            ctl.read_sensors()
            self.assertEqual((ctl.readings["temp"], ctl.fallback), (24.0, None))
            msgs = [e["msg"] for e in ctl.events]
            self.assertTrue(any("Ersatzsensor GGS 1 regelt" in m for m in msgs))
            self.assertTrue(any("Hauptsensor wieder aktiv" in m for m in msgs))

    def test_no_fallback_right_after_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = make(tmp)
            main, ggs = Fixed(temp=24.0, hum=60.0), Fixed(temp=22.0, hum=55.0)
            main.error = True
            ctl._fallbacks = ["ggs"]
            ctl.sensors = {"air": ("", main), "ggs": ("ggs_", ggs)}
            ctl.read_sensors()                                  # Govee braucht nach dem Start etwas -> kein Ersatz
            self.assertIsNone(ctl.fallback)


class TestSteering(unittest.TestCase):
    def at(self, h, m=0, day=13):
        return datetime(2026, 10, day, h, m)

    def test_light_window(self):
        self.assertEqual(rules.light_window(LIGHT, self.at(7)), (60, 1020))
        self.assertIsNone(rules.light_window(LIGHT, self.at(3)))
        self.assertEqual(rules.light_window({"on": "20:00", "off": "08:00"}, self.at(2)), (360, 360))

    def test_phases(self):
        st = {}
        self.assertEqual(rules.steering(SC, LIGHT, self.at(3), 25, st)[:2], (False, "P3"))         # Nacht
        self.assertEqual(rules.steering(SC, LIGHT, self.at(6, 30), 25, st)[:2], (False, "P0"))     # warten
        self.assertEqual(rules.steering(SC, LIGHT, self.at(7, 5), 25, st)[:2], (True, "P1"))       # aufsättigen
        st["last_shot"] = self.at(7, 5)
        self.assertEqual(rules.steering(SC, LIGHT, self.at(7, 15), 26, st)[:2], (False, "P1"))     # Mindestabstand
        self.assertEqual(rules.steering(SC, LIGHT, self.at(7, 30), 31, st)[:2], (False, "P2"))     # Ziel erreicht
        self.assertTrue(st["reached"])
        self.assertEqual(rules.steering(SC, LIGHT, self.at(12), 28, st)[:2], (False, "P2"))        # halten (> 27)
        self.assertEqual(rules.steering(SC, LIGHT, self.at(13), 26.5, st)[:2], (True, "P2"))       # nachgießen
        self.assertEqual(rules.steering(SC, LIGHT, self.at(22, 30), 20, st)[:2], (False, "P3"))    # Gießstopp
        rules.steering(SC, LIGHT, self.at(23, 50), 24, st)                                        # Abendwert
        rules.steering(SC, LIGHT, self.at(6, 1, day=14), 19, st)                                  # neuer Tag
        self.assertEqual((st["dryback"], st["shots"], st["reached"]), (5, 0, False))

    def test_safety(self):
        st = {}
        self.assertEqual(rules.steering(SC, LIGHT, self.at(9), None, st)[:2], (False, "?"))       # kein Sensorwert
        st.update(shots=5)
        self.assertFalse(rules.steering(SC, LIGHT, self.at(9, 1), 10, st)[0])                     # Tageslimit

    def test_controller_uses_shot_size_and_persists(self):
        from growcontroller import outputs

        class Rec:
            healthy = True
            def __init__(self): self.calls = []
            def set(self, on): self.calls.append(on)
            def close(self): pass

        with tempfile.TemporaryDirectory() as tmp:
            ctl = make(tmp, sensors={"air": {"driver": "sim"}})
            ctl.outs["pump"] = outputs.Output("pump", Rec(), role="pump")
            ctl._prev_state["pump"] = False
            ctl.cfg["steering"].update(enabled=True, sensor="ggs_soil_hum", target=30)
            ctl.sensors["ggs"] = ("ggs_", Fixed(soil_hum=20.0))
            ctl.step(self.at(9))
            self.assertTrue(ctl.outs["pump"].state)
            self.assertEqual(ctl.status()["steering"]["phase"], "P1")
            self.assertEqual(ctl.steer["shots"], 1)
            self.assertTrue(any("Crop Steering P1: Schuss 1 (20 s)" in e["msg"] for e in ctl.events))
            again = make(tmp)
            self.assertEqual((again.steer["shots"], again.steer["last_shot"]), (1, self.at(9)))


if __name__ == "__main__":
    unittest.main()
