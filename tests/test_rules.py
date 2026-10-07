import unittest
from datetime import datetime

from growcontroller import rules

CL = {"temp_day": 25, "temp_night": 20, "temp_hyst": 1, "hum_day": 60, "hum_night": 55, "hum_hyst": 5, "fan_min_per_hour": 5}
OFF = {"heater": False, "humidifier": False, "fan": False}


class T(unittest.TestCase):
    def test_light_over_midnight(self):
        c = {"on": "06:00", "off": "00:00"}
        self.assertTrue(rules.light_on(c, datetime(2026, 1, 1, 23, 30)))
        self.assertFalse(rules.light_on(c, datetime(2026, 1, 1, 3, 0)))

    def test_heater_hysteresis(self):
        now = datetime(2026, 1, 1, 12, 30)
        self.assertTrue(rules.climate(CL, True, 23.5, 60, OFF, now)["heater"])
        self.assertTrue(rules.climate(CL, True, 24.5, 60, {**OFF, "heater": True}, now)["heater"])
        self.assertFalse(rules.climate(CL, True, 25.0, 60, {**OFF, "heater": True}, now)["heater"])

    def test_fan_hot_and_humidifier_blocked(self):
        r = rules.climate(CL, True, 27, 40, OFF, datetime(2026, 1, 1, 12, 30))
        self.assertTrue(r["fan"]); self.assertFalse(r["humidifier"])

    def test_fan_cycle(self):
        self.assertTrue(rules.climate(CL, True, 25, 60, OFF, datetime(2026, 1, 1, 12, 2))["fan"])

    def test_irrigation(self):
        c = {"interval_min": 60, "duration_s": 10, "from_hour": 6, "to_hour": 22}
        self.assertTrue(rules.irrigation_due(c, None, datetime(2026, 1, 1, 12)))
        self.assertFalse(rules.irrigation_due(c, datetime(2026, 1, 1, 11, 30), datetime(2026, 1, 1, 12)))
        self.assertFalse(rules.irrigation_due(c, None, datetime(2026, 1, 1, 3)))


if __name__ == "__main__":
    unittest.main()


class TestDehum(unittest.TestCase):
    def test_dehumidifier_replaces_wet_fan(self):
        now = datetime(2026, 1, 1, 12, 30)
        r = rules.climate(CL, True, 25, 70, {**OFF, "dehumidifier": False}, now, has_dehum=True)
        self.assertTrue(r["dehumidifier"]); self.assertFalse(r["fan"]); self.assertFalse(r["humidifier"])
        r = rules.climate(CL, True, 25, 70, OFF, now, has_dehum=False)
        self.assertTrue(r["fan"])
