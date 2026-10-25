"""Bluetooth-Selbstheilung: Hänger erkennen, zurücksetzen, nicht in Schleifen geraten."""
import tempfile
import unittest

from growcontroller import btwatch, controller


class Clock:
    def __init__(self): self.last = 0.0


def make(clock, **kw):
    ev, resets, restarted = [], [], []
    w = btwatch.BluetoothWatchdog(lambda k, m: ev.append((k, m)), activity=lambda: clock.last,
                                  reset=lambda a: resets.append(a), on_reset=lambda: restarted.append(1),
                                  start=False, **kw)
    w.started = 0.0
    return w, ev, resets, restarted


class TestWatchdog(unittest.TestCase):
    def test_quiet_bluetooth_is_reset_once_and_recovery_reported(self):
        c = Clock()
        w, ev, resets, restarted = make(c)
        c.last = 100
        w.check(200)                                   # 100 s still -> ok
        self.assertEqual(resets, [])
        w.check(400)                                   # 300 s ohne Funkpaket -> Reset
        self.assertEqual((resets, restarted), (["hci0"], [1]))
        self.assertEqual(ev[0][0], "alarm")
        self.assertIn("automatisch zurückgesetzt", ev[0][1])
        w.check(430)                                   # noch stumm, aber Sperrzeit -> kein zweiter Reset
        self.assertEqual(len(resets), 1)
        c.last = 440
        w.check(450)                                   # Funkpakete kommen wieder
        self.assertEqual(ev[-1], ("system", "Bluetooth läuft wieder – Geräte werden neu verbunden"))
        self.assertFalse(w.waiting)

    def test_gives_up_after_max_resets_per_hour(self):
        c = Clock()
        w, ev, resets, _ = make(c, cooldown_s=600, max_per_hour=3)
        for t in range(300, 4000, 300):                # bleibt dauerhaft stumm
            w.check(t)
        self.assertEqual(len(resets), 3)               # höchstens 3 je Stunde
        alarms = [m for k, m in ev if k == "alarm"]
        self.assertEqual(sum("bitte den Pi neu starten" in m for m in alarms), 1)   # nur einmal melden

    def test_probe_mode_without_govee(self):
        found = {"n": 0}
        ev, resets = [], []
        w = btwatch.BluetoothWatchdog(lambda k, m: ev.append((k, m)), activity=None, reset=lambda a: resets.append(a),
                                      probe=lambda: found["n"], probe_every_s=300, start=False)
        w.started = 0.0
        w.check(100)                                   # Probe findet nichts, aber erst 100 s
        self.assertEqual(resets, [])
        w.check(400)                                   # neue Probe: weiterhin 0 Geräte, > 180 s -> Reset
        self.assertEqual(resets, ["hci0"])
        found["n"] = 12
        w.check(1100)                                  # Probe findet wieder Geräte -> erholt
        self.assertEqual(ev[-1][0], "system")


class TestController(unittest.TestCase):
    def test_no_watchdog_without_bluetooth_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = controller.Controller({"interval_s": 5, "sensors": {"air": {"driver": "sim"}}, "outputs": {},
                                         "light": {"on": "06:00", "off": "00:00"}, "climate": {}, "irrigation": {}}, tmp)
            self.assertIsNone(ctl.btwatch)
            self.assertIsNone(ctl.status()["bluetooth"])


if __name__ == "__main__":
    unittest.main()
