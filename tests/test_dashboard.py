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
