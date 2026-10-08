import tempfile
import time
import unittest
from datetime import datetime

from growcontroller import controller, rules, settings


def make(tmp):
    cfg = {"interval_s": 5, "sensors": {"air": {"driver": "sim"}},
           "outputs": {"light": {"type": "gpio", "pin": 17}, "fan": {"type": "gpio", "pin": 27}},
           "light": {"on": "06:00", "off": "00:00"}, "climate": {}, "irrigation": {"enabled": False, "interval_min": 60, "duration_s": 5}}
    return controller.Controller(cfg, tmp)


class TestSettings(unittest.TestCase):
    def test_validate(self):
        ok, err = settings.validate({"climate": {"temp_day": 24.5}, "light": {"on": "07:30"}})
        self.assertFalse(err); self.assertEqual(ok["climate"]["temp_day"], 24.5)
        _, err = settings.validate({"climate": {"temp_day": 99, "nope": 1}, "light": {"on": "7:30"}, "x": {}})
        self.assertEqual(set(err), {"climate.temp_day", "climate.nope", "light.on", "x"})
        _, err = settings.validate({"climate": {"temp_day": True}})
        self.assertIn("climate.temp_day", err)  # bool ist keine Zahl

    def test_persist_and_cross_check(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            new, err = c.update_settings({"alarms": {"temp_min": 30, "temp_max": 20}})
            self.assertIsNone(new); self.assertIn("alarms.temp_min", err)
            self.assertEqual(c.cfg["alarms"]["temp_min"], 15)  # nichts übernommen
            new, err = c.update_settings({"climate": {"temp_day": 27}})
            self.assertEqual(new["climate"]["temp_day"], 27)
            c.set_mode("fan", "on")
            c.shutdown()
            c2 = make(d)  # Neustart: Einstellungen + Modus bleiben
            self.assertEqual(c2.cfg["climate"]["temp_day"], 27); self.assertEqual(c2.outs["fan"].mode, "on")
            c2.shutdown()

    def test_presets_are_valid(self):
        for k, p in settings.PRESETS.items():
            _, err = settings.validate({"climate": p["climate"], "light": p["light"], "grow": {"stage": k}})
            self.assertFalse(err, k)


class TestVpd(unittest.TestCase):
    def test_roundtrip(self):
        for t in (20, 25, 30):
            rh = rules.rh_for_vpd(t, 1.0, -2)
            self.assertAlmostEqual(rules.vpd(t, rh, -2), 1.0, delta=0.02)

    def test_vpd_mode_drives_humidity(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            c.update_settings({"climate": {"control": "vpd", "vpd_day": 1.0}})
            c.readings = {"temp": 25.0}
            self.assertAlmostEqual(c.effective_climate(True)["hum_day"], 57.1, delta=0.5)
            c.shutdown()


class TestAlarms(unittest.TestCase):
    def test_delay_and_clear(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            c.update_settings({"alarms": {"delay_min": 0, "temp_max": 30}})
            c.last_ok = time.monotonic(); c.readings = {"temp": 35.0, "hum": 60.0}
            c._check_alarms()
            self.assertIn("temp_high", c.alarms)
            c.readings["temp"] = 24.0; c._check_alarms()
            self.assertNotIn("temp_high", c.alarms)
            self.assertTrue(any(e["kind"] == "alarm" for e in c.events))
            c.shutdown()

    def test_history_downsample(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            now = datetime.now()
            for i in range(3000):
                c.minutes.append({"t": now.isoformat(timespec="seconds"), "temp": 20 + i % 5, "fan": i % 2})
            self.assertLessEqual(len(c.history("24h")), controller.MAX_POINTS)
            c.shutdown()


if __name__ == "__main__":
    unittest.main()


class TestCycles(unittest.TestCase):
    BODY = {"label": "Mein Zyklus", "color": "#ff5d8f", "days": 10, "notes": "Test",
            "climate": {"temp_day": 23.5, "temp_night": 19}, "light": {"enabled": True, "on": "07:00", "off": "19:00"},
            "irrigation": {"interval_min": 300, "duration_s": 20, "from_hour": 7, "to_hour": 19}}

    def test_crud_and_apply(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            pid, err = c.save_preset(dict(self.BODY)); self.assertFalse(err); self.assertTrue(pid)
            self.assertFalse(c.presets()[pid]["builtin"]); self.assertTrue(c.presets()["veg"]["builtin"])
            new, err = c.apply_preset(pid)
            self.assertFalse(err); self.assertEqual(new["climate"]["temp_day"], 23.5)
            self.assertEqual(c.cfg["grow"]["stage"], pid); self.assertEqual(c.status()["grow"]["label"], "Mein Zyklus")
            pid2, err = c.save_preset({**self.BODY, "id": pid, "label": "Umbenannt"}); self.assertEqual(pid2, pid)
            self.assertEqual(c.presets()[pid]["label"], "Umbenannt")
            self.assertTrue(c.delete_preset(pid))
            self.assertEqual(c.cfg["grow"]["stage"], "")  # aktiver Zyklus gelöscht -> keiner aktiv
            c.shutdown()
            c2 = make(d); self.assertNotIn(pid, c2.presets()); c2.shutdown()

    def test_validation_and_protection(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            _, err = c.save_preset({**self.BODY, "label": " ", "color": "rot", "climate": {"temp_day": 99}})
            self.assertEqual({"label", "color", "climate.temp_day"}, set(err))
            _, err = c.save_preset({**self.BODY, "id": "veg"}); self.assertIn("id", err)  # eingebaut: schreibgeschützt
            self.assertFalse(c.delete_preset("veg"))
            _, err = c.save_preset({**self.BODY, "days": 0, "next": "veg"}); self.assertIn("next", err)
            _, err = c.update_settings({"grow": {"stage": "gibtsnicht"}}); self.assertIn("grow.stage", err)
            c.shutdown()

    def test_auto_advance(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            a, _ = c.save_preset({**self.BODY, "label": "A", "days": 7, "next": "veg"})
            c.apply_preset(a, "2026-01-01")
            c._advance(datetime(2026, 1, 5, 12, 0)); self.assertEqual(c.cfg["grow"]["stage"], a)  # noch nicht fällig
            c._advance(datetime(2026, 1, 9, 12, 1))
            self.assertEqual(c.cfg["grow"]["stage"], "veg"); self.assertEqual(c.cfg["grow"]["start_date"], "2026-01-08")
            c.shutdown()
