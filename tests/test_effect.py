"""Tests: Wirksamkeit der Abluft lernen und als Vorsteuerung nutzen."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta

from growcontroller import outputs, rules, settings
from test_fixes import Rec, make

EX = dict(settings.DEFAULTS["exhaust"], room_aware=False)


class TestEffectRules(unittest.TestCase):
    def test_curve_is_monotone(self):
        bins = {"25": [25, 4.0, 3.0, 5], "40": [40, 4.3, 2.0, 5], "60": [60, 2.0, 1.2, 5], "85": [85, 1.0, 0.5, 5],
                "70": [70, 9.9, 9.9, 1]}                       # n < 2 zählt nicht
        c = rules.effect_curve(bins, 1)
        self.assertEqual([p[0] for p in c], [25, 40, 60, 85])
        self.assertTrue(all(b[1] <= a[1] for a, b in zip(c, c[1:])))   # Ausreißer 4.3 geglättet
        self.assertEqual(rules.effect_curve({"25": [25, 4, 3, 5]}, 1), [])

    def test_watts_for_effect(self):
        c = [[22, 4.0], [50, 2.5], [85, 1.0]]
        self.assertAlmostEqual(rules.watts_for_effect(c, 2.5, 22), 50)
        self.assertAlmostEqual(rules.watts_for_effect(c, 1.75, 22), 67.5)
        self.assertEqual(rules.watts_for_effect(c, 5.0, 22), 22)          # Grundlast reicht
        self.assertIsNone(rules.watts_for_effect(c, 0.5, 22))             # selbst Maximum reicht nicht -> P-Regler allein
        self.assertIsNone(rules.watts_for_effect([[40, 3], [60, 2]], 5.0, 22))   # unter dem Gelernten: unbekannt
        self.assertIsNone(rules.watts_for_effect([[40, 3], [45, 2]], 2.5, 22))   # zu schmaler Bereich

    def test_plan_without_ff_unchanged(self):
        for temp in (24.0, 25.0, 26.5, 30.0):
            self.assertEqual(rules.exhaust_plan(EX, True, temp, 50, 25, 60)[0],
                             rules.exhaust_plan(EX, True, temp, 50, 25, 60, ff={})[0])
        self.assertEqual(rules.exhaust_plan(EX, True, 24.0, 50, 25, 60)[0], EX["min_w"])

    def test_plan_with_ff(self):
        w = lambda t, **kw: rules.exhaust_plan(EX, True, t, 50, 25, 60, ff={"temp": 50}, **kw)[0]
        self.assertEqual(w(25.0), 50)                     # im Ziel: gelernte Halteleistung statt Grundlast
        self.assertGreater(w(26.0), 50)                   # zu warm: mehr
        self.assertLess(w(24.0), 50)                      # zu kalt: weniger (auch unter die Halteleistung)
        self.assertEqual(w(10.0), EX["min_w"])            # nie unter Grundlast
        self.assertEqual(w(40.0), EX["max_w"])            # nie über Maximum
        self.assertEqual(w(25.0, active={"heater"}), EX["min_w"])   # Heizung läuft: keine Vorsteuerung

    def test_room_floor_drops_ff(self):
        ex = dict(EX, room_aware=True)
        w, notes = rules.exhaust_plan(ex, True, 26, 50, 25, 60, room_t=27, room_h=40, ff={"temp": 85})
        self.assertEqual(w, ex["min_w"])                  # Raum zu warm: nicht sinnlos auf Maximum
        self.assertTrue(any(n["key"] == "temp" for n in notes))


def ctl(d):
    c = make(d)
    o = outputs.Output("vent", Rec(), role="vent", proportional=True)
    c.outs = {"vent": o}
    c._prev_state = {"vent": False}
    c.update_settings({"climate": {"temp_day": 25, "hum_day": 60, "fan_min_per_hour": 0},
                       "exhaust": {"ramp_w_min": 0, "room_aware": False}})
    c.read_sensors = lambda: None
    c._eff["phase_t"] = time.monotonic() - 3600
    return c, o


def run(c, readings, minutes, start=datetime(2026, 1, 1, 12, 0), active_dehum=False):
    for i in range(minutes):
        c.last_ok = time.monotonic()
        c.readings = dict(readings)
        c.step(start + timedelta(minutes=i))


class TestEffectLearning(unittest.TestCase):
    R = {"temp": 26.0, "hum": 55.0, "room_temp": 22.0, "room_hum": 50.0, "exhaust_w": 40.0}

    def test_learns_after_quiet_window(self):
        with tempfile.TemporaryDirectory() as d:
            c, _ = ctl(d)
            run(c, self.R, 9)
            self.assertEqual(c.effect["day"], {})
            self.assertIn("9/10", c._eff["why"])
            run(c, self.R, 1, datetime(2026, 1, 1, 12, 9))
            b = c.effect["day"]["40"]
            self.assertEqual((b[0], b[1], b[3]), (40.0, 4.0, 1))
            self.assertGreater(b[2], 0)                   # Zelt feuchter als Raum
            self.assertEqual(settings.load(f"{d}/settings.json")["effect"]["exhaust"]["day"]["40"], b)
            c.shutdown()

    def test_no_learning_with_dehumidifier_or_unsteady(self):
        with tempfile.TemporaryDirectory() as d:
            c, _ = ctl(d)
            c.outs["dehum"] = outputs.Output("dehum", Rec(), role="dehumidifier")
            c._prev_state["dehum"] = False
            run(c, dict(self.R, hum=70.0), 15)            # Feuchte hoch -> Entfeuchter an
            self.assertTrue(c.outs["dehum"].state)
            self.assertEqual(c.effect["day"], {})
            self.assertIn("Entfeuchter", c._eff["why"])
            c.shutdown()
        with tempfile.TemporaryDirectory() as d:
            c, _ = ctl(d)
            for i in range(12):                           # Leistung springt -> nicht eingeschwungen
                run(c, dict(self.R, exhaust_w=30.0 + (i % 2) * 20), 1, datetime(2026, 1, 1, 12, i))
            self.assertEqual(c.effect["day"], {})
            self.assertIn("schwankt", c._eff["why"])
            c.shutdown()

    def test_waits_after_light_change_and_needs_room(self):
        with tempfile.TemporaryDirectory() as d:
            c, _ = ctl(d)
            run(c, self.R, 1, datetime(2026, 1, 1, 12, 0))
            run(c, self.R, 15, datetime(2026, 1, 2, 0, 1))  # Licht aus um 00:00
            self.assertEqual(c.effect["night"], {})
            self.assertIn("Licht", c._eff["why"])
            c.shutdown()
        with tempfile.TemporaryDirectory() as d:
            c, _ = ctl(d)
            run(c, {"temp": 26.0, "hum": 55.0, "exhaust_w": 40.0}, 12)
            self.assertIn("Raum", c._eff["why"])
            c.shutdown()

    def test_feedforward_and_reset(self):
        with tempfile.TemporaryDirectory() as d:
            c, o = ctl(d)
            for w, dt in ((22, 6.0), (50, 3.0), (85, 1.5)):
                for _ in range(3):
                    c._effect_add("day", w, dt, 2.0)
            # Raum 22 °C, Ziel 25 °C -> Abstand 3 °C nötig -> laut Lernkurve 50 W, auch wenn das Zelt genau im Ziel ist
            run(c, dict(self.R, temp=25.0, exhaust_w=50.0), 1)
            st = c.status()["exhaust"]["effect"]
            self.assertAlmostEqual(st["ff"]["temp"]["w"], 50)
            self.assertAlmostEqual(o.watts, 50, delta=0.5)
            c.update_settings({"exhaust": {"use_effect": False}})
            run(c, dict(self.R, temp=25.0), 1, datetime(2026, 1, 1, 12, 1))
            self.assertEqual(o.watts, c.cfg["exhaust"]["min_w"])      # ohne Vorsteuerung: Grundlast
            c.reset_effect()
            self.assertEqual(c.effect, {"day": {}, "night": {}})
            c.shutdown()


if __name__ == "__main__":
    unittest.main()
