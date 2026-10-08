"""Tests: vorausschauende, nach Wirkung und Kosten abgewogene Klima-Logik."""
import tempfile
import unittest
from datetime import datetime, timedelta

from growcontroller import outputs, rules, settings
from test_fixes import Rec, make

CL = dict(settings.DEFAULTS["climate"], temp_day=25, temp_hyst=1, hum_day=60, hum_hyst=5, fan_min_per_hour=0)
NOW = datetime(2026, 1, 1, 12, 30)
DEH = {"dehumidifier": {"power_w": 250, "t": 1.0, "ah": -2.0, "n": 3}}


def dec(temp, hum, cur=None, trend=None, devs=None, ex=None, waited=0):
    ctx = {"trend": trend or {}, "devices": DEH if devs is None else devs, "exhaust": ex or {}, "waited_min": waited}
    return rules.smart_climate(CL, True, temp, hum, cur or {}, NOW, ctx)


class TestSmartRules(unittest.TestCase):
    def test_forecast_switches_earlier_or_not_at_all(self):
        self.assertFalse(dec(25, 63)[0]["dehumidifier"])                                # ruhig: wie bisher erst ab 65 %
        self.assertTrue(dec(25, 63, trend={"dT": 0, "dAH": 3.0})[0]["dehumidifier"])     # steigt schnell: schon jetzt an
        self.assertFalse(dec(25, 66, trend={"dT": 0, "dAH": -3.0})[0]["dehumidifier"])   # über Grenze, fällt aber schon
        self.assertTrue(dec(25, 71, trend={"dT": 0, "dAH": -3.0})[0]["dehumidifier"])    # weit drüber: sicher an

    def test_predictive_off(self):
        on = {"dehumidifier": True}
        self.assertTrue(dec(25, 62, on)[0]["dehumidifier"])
        self.assertFalse(dec(25, 62, on, trend={"dT": 0, "dAH": -3.0})[0]["dehumidifier"])  # erreicht Ziel gleich -> aus

    def test_exhaust_preferred_when_cheaper(self):
        w, info = dec(25, 67, ex={"can_dry": True, "extra_w": 20})
        self.assertFalse(w["dehumidifier"])
        self.assertTrue(info["prefer_exhaust"])
        self.assertIn("Abluft", info["reasons"]["dehumidifier"])
        w, info = dec(25, 67, ex={"can_dry": True, "extra_w": 20}, waited=16)               # Abluft schafft es nicht
        self.assertTrue(w["dehumidifier"])
        self.assertTrue(dec(25, 67, ex={"can_dry": False})[0]["dehumidifier"])           # Raumluft zu feucht

    def test_temperature_weighs_in(self):
        ex = {"can_dry": True, "extra_w": 150}
        self.assertTrue(dec(23.0, 67, ex=ex)[0]["dehumidifier"])     # zu kalt: Entfeuchter-Abwärme willkommen
        self.assertFalse(dec(27.0, 67, ex=ex)[0]["dehumidifier"])    # zu warm: Abluft kühlt und trocknet

    def test_heater_and_humidifier(self):
        devs = {"heater": {"power_w": 300}, "humidifier": {"power_w": 30}}
        self.assertFalse(dec(24.2, 60, devs=devs)[0]["heater"])
        self.assertTrue(dec(24.2, 60, devs=devs, trend={"dT": -3.0, "dAH": 0})[0]["heater"])  # kühlt schnell ab
        self.assertFalse(dec(24.6, 60, {"heater": True}, devs=devs, trend={"dT": 3.0, "dAH": 0})[0]["heater"])
        self.assertTrue(dec(25, 54, devs=devs)[0]["humidifier"])
        self.assertFalse(dec(27, 54, devs=devs)[0]["humidifier"])    # zu warm: Abluft läuft, nicht dagegen befeuchten


class TestDeviceLearning(unittest.TestCase):
    def test_learns_dehumidifier_effect(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            c.outs = {"entf": outputs.Output("entf", Rec(), role="dehumidifier")}
            t_on = datetime(2026, 1, 1, 12, 0)
            c.minutes.clear()
            for m in range(-10, 16):
                ah = 14.0 + 0.02 * m if m < 0 else 14.0 - 0.03 * m   # vorher +1,2 g/m³/h, danach −1,8 g/m³/h
                temp = 25.0 + (0.01 * m if m >= 0 else 0)             # Abwärme +0,6 °C/h
                hum = rules.rh_at(temp, ah)
                c.minutes.append({"t": (t_on + timedelta(minutes=m)).isoformat(timespec="seconds"),
                                  "temp": temp, "hum": hum, "entf": int(m >= 0)})
            c._dev_sample("entf", c.outs["entf"], t_on, t_on + timedelta(minutes=15))
            e = c.dev_eff["entf"]
            self.assertAlmostEqual(e["ah"], -3.0, delta=0.15)
            self.assertAlmostEqual(e["t"], 0.6, delta=0.05)
            self.assertEqual(settings.load(f"{d}/settings.json")["effect"]["devices"]["entf"]["n"], 1)
            c.shutdown()

    def test_skips_when_other_device_switched(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            c.outs = {"entf": outputs.Output("entf", Rec(), role="dehumidifier"),
                      "heiz": outputs.Output("heiz", Rec(), role="heater")}
            t_on = datetime(2026, 1, 1, 12, 0)
            for m in range(-10, 16):
                c.minutes.append({"t": (t_on + timedelta(minutes=m)).isoformat(timespec="seconds"),
                                  "temp": 25.0, "hum": 60.0, "entf": int(m >= 0), "heiz": int(m >= 5)})
            c._dev_sample("entf", c.outs["entf"], t_on, t_on + timedelta(minutes=15))
            self.assertEqual(c.dev_eff["entf"]["n"], 0)
            self.assertIn("anderes Gerät", c.dev_eff["entf"]["skip"])
            c.shutdown()

    def test_trend_from_live(self):
        with tempfile.TemporaryDirectory() as d:
            c = make(d)
            c.live.clear()
            t0 = datetime(2026, 1, 1, 12, 0)
            for i in range(121):  # 10 min im 5-s-Takt, Temperatur +1 °C in 10 min
                c.live.append({"t": (t0 + timedelta(seconds=5 * i)).isoformat(timespec="seconds"), "temp": 24 + i / 120, "hum": 60.0})
            self.assertAlmostEqual(c._trend()["dT"], 6.0, delta=0.05)
            c.shutdown()


if __name__ == "__main__":
    unittest.main()
