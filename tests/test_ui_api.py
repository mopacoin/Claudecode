"""Server-Schnittstellen für die Oberfläche (Verlauf gestern, Ereignisse im Zeitraum, …)."""
import tempfile
import unittest
from datetime import datetime, timedelta

from growcontroller import controller


def make(tmp, **extra):
    cfg = {"interval_s": 5, "sensors": {"air": {"driver": "sim"}}, "outputs": {}, "light": {"on": "06:00", "off": "00:00"},
           "climate": {}, "irrigation": {}, **extra}
    return controller.Controller(cfg, tmp)


class TestHistoryOffset(unittest.TestCase):
    def test_yesterday_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = make(tmp)
            now = datetime.now()
            for h in (31, 26, 23.5, 1, 0.5):  # gestern-Fenster 6h: 30…24 h zurück
                ctl.minutes.append({"t": (now - timedelta(hours=h)).isoformat(timespec="seconds"), "temp": h})
            ctl.minutes = type(ctl.minutes)(sorted(ctl.minutes, key=lambda r: r["t"]), maxlen=ctl.minutes.maxlen)
            today = [r["temp"] for r in ctl.history("6h")]
            yesterday = [r["temp"] for r in ctl.history("6h", 24)]
            self.assertEqual(today, [1, 0.5])
            self.assertEqual(yesterday, [26])                 # 31 h und 23,5 h liegen außerhalb


class TestEventsSince(unittest.TestCase):
    def test_events_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = make(tmp)
            ctl.event("alarm", "Temperatur zu hoch")
            again = make(tmp)
            since = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
            self.assertIn("Temperatur zu hoch", [e["msg"] for e in again._events_since(since)])


if __name__ == "__main__":
    unittest.main()


class TestHealthAndReport(unittest.TestCase):
    def test_health_lists_checks_and_report_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = make(tmp)
            names = [h["name"] for h in ctl.health()]
            self.assertIn("Sensoren", names)
            self.assertIn("Geräte", names)
            self.assertIn("Pi", names)
            self.assertTrue(all(h["state"] in ("ok", "warn", "bad", "off") for h in ctl.health()))
            ctl.notifier.ntfy = {"topic": "x"}
            ctl.notifier.send_now = lambda title, msg, prio="default": None
            self.assertIsNone(ctl.send_report_now())
            self.assertIn("Grow-Tagesbericht", make(tmp).report_last["title"])   # übersteht Neustart


class TestStatsAndEnergy(unittest.TestCase):
    def write_day(self, tmp, day, rows, fields):
        import csv, os
        with open(os.path.join(tmp, f"log-{day}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    def test_daily_stats_and_energy(self):
        from growcontroller import outputs
        with tempfile.TemporaryDirectory() as tmp:
            day = (datetime.now().date() - timedelta(days=1)).isoformat()
            start = datetime.fromisoformat(day + "T00:00:00")
            rows = []
            for i in range(1440):   # ganzer Tag: Temperatur 12 h im Ziel, 12 h zu kalt; Entfeuchter 6 h an
                t = start + timedelta(minutes=i)
                rows.append({"t": t.isoformat(timespec="seconds"), "temp": 25 if i < 720 else 18, "hum": 60, "vpd": 1.0,
                             "dehumidifier": 1 if i < 360 else 0, "exhaust_w": 30})
            self.write_day(tmp, day, rows, ["t", "temp", "hum", "vpd", "dehumidifier", "exhaust_w"])
            ctl = make(tmp, light={"on": "00:00", "off": "00:00"},
                       climate={"control": "temp_hum", "temp_day": 25, "temp_hyst": 1, "hum_day": 60, "hum_hyst": 5,
                                "vpd_day": 1.0, "vpd_hyst": 0.1})

            class Rec:
                def set(self, v): pass
                def close(self): pass
            ctl.outs["dehumidifier"] = outputs.Output("dehumidifier", Rec(), role="dehumidifier")
            st = {d["day"]: d for d in ctl.daily_stats(2)["days"]}[day]
            self.assertEqual((st["temp"]["in"], st["temp"]["lo"], st["hum"]["in"]), (50, 50, 100))
            self.assertEqual(st["all"], 50)
            en = ctl.energy(2)
            dev = {d["name"]: d for d in en["devices"]}["dehumidifier"]
            i = en["days"].index(day)
            self.assertEqual((dev["w"], dev["source"], dev["on_min"][i]), (250.0, "Annahme", 360))
            self.assertAlmostEqual(dev["kwh"][i], 1.5)                     # 6 h × 250 W
            self.assertIsNone(ctl.set_energy({"price_ct": 40, "power": {"dehumidifier": 300}}))
            en = ctl.energy(2)
            dev = {d["name"]: d for d in en["devices"]}["dehumidifier"]
            self.assertEqual((dev["w"], dev["source"]), (300.0, "eingetragen"))
            self.assertAlmostEqual(en["cost_eur"][i], round(1.8 * 0.40, 2))
            self.assertIn("0…200", ctl.set_energy({"price_ct": 999}))
