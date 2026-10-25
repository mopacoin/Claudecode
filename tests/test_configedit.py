"""Geräte-Editor und Sicherung: Geheimnisse bleiben verborgen, Prüfung, Sicherungskopien, Wiederherstellen."""
import json
import os
import tempfile
import unittest

from growcontroller import configedit as ce

CFG = {"interval_s": 5, "web": {"port": 8080},
       "outputs": {"exhaust": {"type": "meross_local", "ip": "1.2.3.4", "key": "geheim", "role": "fan"},
                   "dehumidifier": {"type": "tuya", "dev_id": "abc", "local_key": "${TUYA_LOCAL_KEY}"},
                   "light": {"pin": 17}},
       "sensors": {"air": {"driver": "govee", "mac": "AA:BB"}, "ggs": {"driver": "spiderfarmer", "mac": "CC", "prefix": "ggs_"}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self.tmp.name, "config.json")
        self.data = os.path.join(self.tmp.name, "data")
        os.makedirs(self.data)
        with open(self.cfg, "w") as f:
            json.dump(CFG, f)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self):
        with open(self.cfg) as f:
            return json.load(f)


class TestDevices(Base):
    def test_secrets_masked_placeholders_visible(self):
        d = ce.read_devices(self.cfg)
        self.assertEqual(d["outputs"]["exhaust"]["key"], ce.MASK)
        self.assertEqual(d["outputs"]["dehumidifier"]["local_key"], "${TUYA_LOCAL_KEY}")
        self.assertNotIn("geheim", json.dumps(d))

    def test_save_keeps_masked_secret_and_backs_up(self):
        d = ce.read_devices(self.cfg)
        d["outputs"]["exhaust"]["ip"] = "1.2.3.5"
        self.assertIsNone(ce.write_devices(self.cfg, d))
        c = self.load()
        self.assertEqual(c["outputs"]["exhaust"], {"type": "meross_local", "ip": "1.2.3.5", "key": "geheim", "role": "fan"})
        self.assertEqual(c["web"], {"port": 8080})                     # Rest der Config unangetastet
        self.assertTrue(any(".bak-ui-" in n for n in os.listdir(self.tmp.name)))

    def test_new_device_cannot_borrow_mask(self):
        d = ce.read_devices(self.cfg)
        d["outputs"]["fan2"] = {"type": "meross_local", "ip": "1.2.3.9", "key": ce.MASK}
        self.assertIn("key", ce.write_devices(self.cfg, d))           # Pflichtfeld fehlt -> Fehler, nichts geschrieben
        self.assertNotIn("fan2", self.load()["outputs"])

    def test_validation(self):
        o, s = CFG["outputs"], CFG["sensors"]
        self.assertIsNone(ce.validate(o, s))
        self.assertIn("Name", ce.validate({"Böse Name": {"pin": 1}}, s))
        self.assertIn("Typ", ce.validate({"x": {"type": "zigbee"}}, s))
        self.assertIn("pin", ce.validate({"x": {"type": "gpio"}}, s))
        self.assertIn("gleichzeitig", ce.validate({"air": {"pin": 1}}, s))


class TestBackup(Base):
    def test_roundtrip(self):
        with open(os.path.join(self.data, "settings.json"), "w") as f:
            json.dump({"climate": {"temp_day": 26}}, f)
        b = json.loads(json.dumps(ce.make_backup(self.cfg, self.data)))
        self.assertEqual(b["config"], CFG)
        self.assertIsNone(b["state"])
        with open(os.path.join(self.data, "settings.json"), "w") as f:
            json.dump({"climate": {"temp_day": 20}}, f)
        b["config"]["outputs"]["light"]["pin"] = 22
        self.assertIsNone(ce.restore_backup(self.cfg, self.data, b))
        with open(os.path.join(self.data, "settings.json")) as f:
            self.assertEqual(json.load(f)["climate"]["temp_day"], 26)
        self.assertEqual(self.load()["outputs"]["light"]["pin"], 22)
        self.assertTrue(any(".bak-restore-" in n for n in os.listdir(self.data)))

    def test_rejects_foreign_file(self):
        self.assertIsNotNone(ce.restore_backup(self.cfg, self.data, {"hallo": 1}))
        self.assertIsNotNone(ce.restore_backup(self.cfg, self.data, {"growcontroller_backup": 1, "config": {"outputs": []}}))
        self.assertEqual(self.load(), CFG)


if __name__ == "__main__":
    unittest.main()
