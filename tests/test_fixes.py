"""Tests für die Korrekturen aus der Gesamtprüfung."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime

from growcontroller import controller, outputs, rules, settings, web


class Rec:
    healthy = True

    def __init__(self):
        self.calls = []

    def set(self, v):
        self.calls.append(v)

    def close(self):
        pass


def make(tmp, **extra):
    cfg = {"interval_s": 5, "sensors": {"air": {"driver": "sim"}},
           "outputs": {}, "light": {"on": "06:00", "off": "00:00"}, "climate": {},
           "irrigation": {"enabled": True, "interval_min": 60, "duration_s": 5}, **extra}
    return controller.Controller(cfg, tmp)


class TestOutputs(unittest.TestCase):
    def test_max_on_latches(self):
        o = outputs.Output("pump", Rec(), max_on_s=0.05)
        o.apply(True); time.sleep(0.08); o.apply(True)
        self.assertFalse(o.state)            # Laufzeitgrenze erreicht -> aus
        o.apply(True)
        self.assertFalse(o.state)            # vorher schaltete sie hier sofort wieder ein
        o.apply(False); o.apply(True)
        self.assertTrue(o.state)             # nach Ende der Anforderung wieder frei

    def test_servo_no_initial_zero(self):
        be = Rec()
        outputs.Output("vent", be, proportional=True)
        self.assertEqual(be.calls, [])       # kein Sprung auf 0° beim Start
        be2 = Rec()
        outputs.Output("light", be2)
        self.assertEqual(be2.calls, [False])


class TestIrrigation(unittest.TestCase):
    CFG = {"enabled": True, "interval_min": 600, "from_hour": 6, "to_hour": 22}

    def test_next_respects_window(self):
        last = datetime(2026, 1, 1, 20, 0)   # +10 h = 06:00 nächster Tag
        self.assertEqual(rules.irrigation_next(self.CFG, last, datetime(2026, 1, 1, 21, 0)), datetime(2026, 1, 2, 6, 0))
        last = datetime(2026, 1, 1, 15, 0)   # +10 h = 01:00 -> außerhalb -> 06:00
        self.assertEqual(rules.irrigation_next(self.CFG, last, datetime(2026, 1, 1, 16, 0)), datetime(2026, 1, 2, 6, 0))
        last = datetime(2026, 1, 1, 7, 0)
        self.assertEqual(rules.irrigation_next(self.CFG, last, datetime(2026, 1, 1, 8, 0)), datetime(2026, 1, 1, 17, 0))
        self.assertIsNone(rules.irrigation_next({**self.CFG, "enabled": False}, last, datetime(2026, 1, 1, 8)))

    def test_no_pump_no_watering(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            self.assertFalse(c._pump(datetime(2026, 1, 1, 12)))
            self.assertIsNone(c.last_irrigation)                 # kein Schein-Eintrag
            with self.assertRaises(ValueError):
                c.water_now()
            self.assertFalse(c.status()["irrigation"]["available"])
            c.shutdown()


class TestReadings(unittest.TestCase):
    def test_offsets_and_stale_values_removed(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d, sensors={"air": {"driver": "sim", "temp_offset": -1.0, "hum_offset": 50}})

            class Fixed:
                def __init__(s, v): s.v = v
                def read(s): return s.v
            c.sensors = {"air": ("", Fixed({"temp": 25.0, "hum": 70.0})), "raum": ("room_", Fixed({"temp": 20.0}))}
            c._offsets = {"air": (-1.0, 50.0)}
            c._sensor_ok = {"air": time.monotonic(), "raum": time.monotonic()}
            c.read_sensors()
            self.assertEqual(c.readings["temp"], 24.0)
            self.assertEqual(c.readings["hum"], 100.0)          # auf 100 % begrenzt
            self.assertEqual(c.readings["room_temp"], 20.0)

            class Broken:
                def read(s): raise RuntimeError("weg")
            c.sensors["raum"] = ("room_", Broken())
            c._rt["room_temp"] -= 1000                           # Raumwert ist alt
            c.read_sensors()
            self.assertNotIn("room_temp", c.readings)            # wird nicht mehr angezeigt/geregelt
            c._sensor_ok["raum"] -= 1000
            c._check_alarms()
            self.assertIn("sensor:raum", c.alarms)
            c.shutdown()

    def test_history_averages_room_values(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            now = datetime.now().isoformat(timespec="seconds")
            c.outs = {"fan": outputs.Output("fan", Rec())}
            for i in range(1300):
                c.minutes.append({"t": now, "room_temp": 20 + (i % 2) * 2, "fan": i % 2})
            h = c.history("24h")
            self.assertTrue(20.0 < h[0]["room_temp"] < 22.0)              # Mittel statt Maximum (22)
            self.assertEqual(h[0]["fan"], 1)                                # Gerät: an, wenn je an
            c.shutdown()


class TestIntakeFollowsExhaust(unittest.TestCase):
    def test_intake(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            vent, intake = Rec(), Rec()
            c.outs = {"zuluft": outputs.Output("zuluft", intake, role="intake"),
                      "vent": outputs.Output("vent", vent, role="vent", proportional=True)}
            c._prev_state = {n: False for n in c.outs}
            c.update_settings({"climate": {"temp_day": 25, "temp_night": 25, "fan_min_per_hour": 0},
                               "exhaust": {"ramp_w_min": 0, "room_aware": False}})
            c.read_sensors = lambda: None
            c.last_ok = time.monotonic()
            c.readings = {"temp": 25.2, "hum": 50.0}             # kaum über Soll -> Abluft schwach
            c.step(datetime(2026, 1, 1, 12, 30))
            self.assertFalse(c.outs["zuluft"].state)
            c.readings = {"temp": 27.5, "hum": 50.0}             # deutlich zu warm -> Abluft kräftig
            c.step(datetime(2026, 1, 1, 12, 31))
            self.assertTrue(c.outs["zuluft"].state)
            c.shutdown()


class TestCalibrationSafety(unittest.TestCase):
    def test_sweep_needs_power_and_restores_mode(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            o = outputs.Output("vent", Rec(), role="vent", proportional=True)
            c.outs = {"vent": o}; c._prev_state = {"vent": False}
            c.fresh = {"exhaust_w": 0.0}
            with self.assertRaises(ValueError):
                c.start_sweep()                                   # Steckdose aus -> kein Start
            o.mode = "on"
            c.fresh = {"exhaust_w": 30.0}
            c.start_sweep()
            self.assertEqual(o.mode, "manual")
            c.stop_sweep()
            self.assertEqual(o.mode, "on")                        # vorheriger Modus zurück
            c.shutdown()

    def test_zero_watts_never_learned(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            o = outputs.Output("vent", Rec(), role="vent", proportional=True); o.angle = 90
            c._lrn = {"angle": 90, "since": 0, "last": 0, "saved": time.monotonic()}
            for i in range(10):
                c.fresh = {"exhaust_w": 0.0, "exhaust_seq": i}
                c._learn_step(o)
            self.assertEqual(c.learn, {})
            c.shutdown()

    def test_manual_test_times_out(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            o = outputs.Output("vent", Rec(), role="vent", proportional=True)
            c.outs = {"vent": o}; c._prev_state = {"vent": False}
            c.servo_manual("vent", 120)
            self.assertEqual(o.mode, "manual")
            o._manual_t -= controller.MANUAL_MAX_S + 1
            c._apply_servo(o, {}, True, 30.0)
            self.assertEqual(o.mode, "auto")
            c.shutdown()


class TestWebSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = make(self.tmp.name)
        self.srv = web.serve(self.c, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown(); self.c.shutdown(); self.tmp.cleanup()

    def post(self, body, headers):
        h = http.client.HTTPConnection("127.0.0.1", self.port)
        h.request("POST", "/api/settings", body, headers)
        r = h.getresponse(); r.read(); return r.status

    def test_csrf_and_body(self):
        ok = json.dumps({"climate": {"temp_day": 24}})
        self.assertEqual(self.post(ok, {"Content-Type": "text/plain"}), 415)             # Formular/Fremdseite
        self.assertEqual(self.post(ok, {"Content-Type": "application/json", "Origin": "http://evil.example"}), 403)
        self.assertEqual(self.post("[1,2]", {"Content-Type": "application/json"}), 400)
        self.assertEqual(self.post(ok, {"Content-Type": "application/json"}), 200)
        self.assertEqual(self.post(ok, {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}), 200)


if __name__ == "__main__":
    unittest.main()
