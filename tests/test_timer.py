"""Zeitschaltung (Rolle "timer") und Meross-Steckdose mit Wiederholung."""
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from growcontroller import backends, controller, rules, settings

LIGHT = {"enabled": True, "on": "13:00", "off": "05:00"}


def at(hh, mm):
    return datetime(2026, 1, 1, hh, mm)


class TestTimerRules(unittest.TestCase):
    def test_clock_window_over_midnight(self):
        t = {"enabled": True, "windows": [{"start": "22:00", "end": "02:00"}]}
        self.assertTrue(rules.timer_on(t, LIGHT, at(23, 0)))
        self.assertTrue(rules.timer_on(t, LIGHT, at(1, 59)))
        self.assertFalse(rules.timer_on(t, LIGHT, at(2, 0)))
        self.assertFalse(rules.timer_on(t, LIGHT, at(12, 0)))

    def test_relative_to_light(self):
        t = {"enabled": True, "windows": [{"start": "off-15", "end": "off+15"}]}   # Abend-IR um 05:00
        self.assertTrue(rules.timer_on(t, LIGHT, at(4, 45)))
        self.assertTrue(rules.timer_on(t, LIGHT, at(5, 10)))
        self.assertFalse(rules.timer_on(t, LIGHT, at(5, 15)))
        self.assertFalse(rules.timer_on(t, {**LIGHT, "enabled": False}, at(4, 50)))  # Lichtplan aus
        moved = {**LIGHT, "off": "07:00"}                                             # Zyklus geändert
        self.assertTrue(rules.timer_on(t, moved, at(6, 50)))

    def test_interval_and_disabled(self):
        t = {"enabled": True, "windows": [{"start": "on+0", "end": "on+120", "on_min": 10, "off_min": 20}]}
        self.assertTrue(rules.timer_on(t, LIGHT, at(13, 5)))
        self.assertFalse(rules.timer_on(t, LIGHT, at(13, 15)))
        self.assertTrue(rules.timer_on(t, LIGHT, at(13, 31)))
        self.assertFalse(rules.timer_on(t, LIGHT, at(15, 5)))
        self.assertFalse(rules.timer_on({**t, "enabled": False}, LIGHT, at(13, 5)))
        self.assertTrue(rules.timer_on({"windows": [{"start": "08:00", "end": "08:00"}]}, LIGHT, at(3, 0)))  # ganzer Tag
        self.assertFalse(rules.timer_on(None, LIGHT, at(13, 5)))

    def test_validation(self):
        ok, err = settings.validate_timer({"enabled": True, "windows": [{"start": "on+30", "end": "off-90", "on_min": 5, "off_min": 10}]})
        self.assertIsNone(err)
        self.assertEqual(ok["windows"][0]["end"], "off-90")
        for bad in ({"windows": [{"start": "25:00", "end": "off"}]}, {"windows": [{"start": "on+2000", "end": "on+10"}]},
                    {"windows": [{"start": "on+0", "end": "on+10", "on_min": 5}]}, {"windows": "x"},
                    {"windows": [{"start": "on+0", "end": "on+1"}] * 9}, {"enabled": "ja"}):
            self.assertIsNotNone(settings.validate_timer(bad)[1], bad)


class TestMerossRepeat(unittest.TestCase):
    def _backend(self, script):
        """script: Liste der Zustände, die der Stecker nacheinander meldet (None = nicht lesbar)."""
        calls = []

        def fake(ip, key, ns, method="SET", payload=None, timeout=5):
            calls.append((ns, method, payload))
            if method == "GET":
                st = script.pop(0) if script else None
                if st is None:
                    raise RuntimeError("kein Zustand")
                return {"all": {"digest": {"togglex": [{"channel": 0, "onoff": int(st)}]}}}
            return {}
        p = mock.patch.object(backends, "meross_request", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        be = backends.MerossLocalBackend("1.2.3.4", "k", repeat=3, verify_s=0, resend_s=999)
        self.addCleanup(be.close)
        return be, calls

    def sets(self, calls):
        return [c for c in calls if c[1] == "SET"]

    def test_repeats_until_confirmed(self):
        be, calls = self._backend([False, False, True])   # vorher aus, 1. Versuch verschluckt, 2. klappt
        be._send(True)
        self.assertEqual(len(self.sets(calls)), 2)
        self.assertTrue(be.actual())

    def test_no_switch_when_already_right(self):
        be, calls = self._backend([True])
        be._send(True)
        self.assertEqual(self.sets(calls), [])

    def test_unreadable_sends_repeat_times(self):
        be, calls = self._backend([None, None, None, None])
        be._send(False)
        self.assertEqual(len(self.sets(calls)), 3)

    def test_gives_up_with_error(self):
        be, calls = self._backend([False, False, False, False])
        with self.assertRaises(RuntimeError):
            be._send(True)
        self.assertEqual(len(self.sets(calls)), 3)


class TestControllerTimer(unittest.TestCase):
    def test_timer_output_and_api(self):
        class Rec:
            healthy = True
            def __init__(self): self.sent = []
            def set(self, v): self.sent.append(v)
            def close(self): pass
        with tempfile.TemporaryDirectory() as d:
            cfg = {"interval_s": 5, "sensors": {"air": {"driver": "sim"}},
                   "outputs": {"uva": {"type": "gpio", "pin": 5, "role": "timer"}},
                   "light": {"on": "13:00", "off": "05:00"}, "climate": {},
                   "irrigation": {"enabled": False, "interval_min": 60, "duration_s": 5}}
            c = controller.Controller(cfg, d)
            rec = c.outs["uva"].backend = Rec()
            c.step(at(14, 0))
            self.assertFalse(c.outs["uva"].state)                     # ohne Fenster aus
            c.set_timer("uva", {"enabled": True, "windows": [{"start": "on+60", "end": "on+300"}]})
            c.step(at(14, 0))
            self.assertTrue(c.outs["uva"].state)
            c.step(at(18, 30))
            self.assertFalse(c.outs["uva"].state)
            self.assertEqual(rec.sent, [True, False])
            info = c.status()["outputs"]["uva"]["timer"]
            self.assertEqual(info["spans"], [[840, 1080, 0, 0]])
            with self.assertRaises(ValueError):
                c.set_timer("uva", {"windows": [{"start": "x", "end": "y"}]})
            with self.assertRaises(ValueError):
                c.set_timer("air", {"windows": []})
            c2 = controller.Controller(cfg, d)                          # bleibt nach Neustart
            self.assertEqual(c2.timers["uva"]["windows"][0]["start"], "on+60")


if __name__ == "__main__":
    unittest.main()
