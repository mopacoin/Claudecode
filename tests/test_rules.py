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


class TestGovee(unittest.TestCase):
    def test_formats(self):
        from growcontroller.sensors import decode_govee
        v = 224 * 1000 + 605  # 22.4 °C, 60.5 %
        self.assertEqual(decode_govee(0xEC88, b"\x00" + v.to_bytes(3, "big") + bytes([87, 0])), {"temp": 22.4, "hum": 60.5, "batt": 87})
        neg = (52 * 1000 + 400) | 0x800000  # -5.2 °C, 40 %
        self.assertEqual(decode_govee(0xEC88, b"\x00" + neg.to_bytes(3, "big") + bytes([50, 0]))["temp"], -5.2)
        self.assertEqual(decode_govee(0x0001, b"\x01\x01" + v.to_bytes(3, "big") + bytes([64])), {"temp": 22.4, "hum": 60.5, "batt": 64})
        import struct
        self.assertEqual(decode_govee(0xEC88, b"\x00" + struct.pack("<hHB", 2345, 5678, 90) + b"\x02\x00\x00"), {"temp": 23.4, "hum": 56.8, "batt": 90})
        self.assertEqual(decode_govee(0x8801, b"\x01\x00\x01\x01" + struct.pack("<hHB", 2105, 4890, 77)), {"temp": 21.1, "hum": 48.9, "batt": 77})
        self.assertIsNone(decode_govee(0x004C, b"\x02\x15" + b"\x00" * 21))  # Apple-iBeacon o. ä.


class TestMqttSensor(unittest.TestCase):
    def test_parse_and_read(self):
        from types import SimpleNamespace
        from growcontroller.sensors import MqttSensor
        s = MqttSensor.__new__(MqttSensor)  # ohne Broker
        s.topics = {"temp": "grow/GGS/sensor/temp", "light_on": "grow/GGS/light/on"}
        s.by_topic = {t: n for n, t in s.topics.items()}
        s.vals, s.connected, s.max_age, s.json_key = {}, True, 300, None
        with self.assertRaises(RuntimeError):
            s.read()
        s._on_message(None, None, SimpleNamespace(topic="grow/GGS/sensor/temp", payload=b"24.6"))
        s._on_message(None, None, SimpleNamespace(topic="grow/GGS/light/on", payload=b"true"))
        s._on_message(None, None, SimpleNamespace(topic="grow/GGS/sensor/temp", payload=b"kaputt"))  # ignoriert
        s._on_message(None, None, SimpleNamespace(topic="anderes/topic", payload=b"1"))
        self.assertEqual(s.read(), {"temp": 24.6, "light_on": 1.0})
        s.json_key = "value"
        self.assertEqual(s.parse(b'{"value": 3}'), 3.0)
