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
