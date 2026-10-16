"""SANlight-Dimmer: Mesh-Krypto gegen Testvektoren der Mesh-Profile-Spec, Loopback, Dimmen im Controller."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

from growcontroller import backends, controller, outputs, rules
from growcontroller import sanlight as sl


class FakeClient:
    def __init__(self):
        self.is_connected = True
        self.sent = []

    async def write_gatt_char(self, _c, data, response=False):
        self.sent.append(bytes(data))


class TestCrypto(unittest.TestCase):
    N = bytes.fromhex("f7a2a44f8e8a8029064f173ddc1e2b00")

    def test_key_derivation(self):
        nid, ek, pk = sl.k2(self.N)
        self.assertEqual(nid, 0x7F)
        self.assertEqual(ek.hex(), "9f589181a0f50de73c8070c7a6d27f46")
        self.assertEqual(pk.hex(), "4c715bd4a64b938f99b453351653124f")
        self.assertEqual(sl.k3(self.N).hex(), "ff046958233db014")
        self.assertEqual(sl.k4(bytes.fromhex("3216d1509884b533248541792b877f98")), 0x38)

    def test_spec_message_1(self):
        with tempfile.TemporaryDirectory() as d:
            m = sl.SanlightMesh(bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6"), os.urandom(16),
                                0x1201, os.path.join(d, "s.json"), iv_index=0x12345678)
            pdu = m.encode_network(1, 0, 1, 0xFFFD, bytes.fromhex("034b50057e400000010000"))
            self.assertEqual(pdu.hex(), "68eca487516765b5e5bfdacbaf6cb7fb6bff871f035444ce83a670df")
            self.assertEqual(m.decode_network(pdu)["transport"].hex(), "034b50057e400000010000")

    def test_segmented_loopback_and_sar(self):
        async def run(d):
            nk, ak = os.urandom(16), os.urandom(16)
            a = sl.SanlightMesh(nk, ak, 0x0010, os.path.join(d, "a.json"), iv_index=1)
            b = sl.SanlightMesh(nk, ak, 0x0003, os.path.join(d, "b.json"), iv_index=1)
            a.client = FakeClient()
            got = []
            b.on_message = lambda *m: got.append(m)
            cfg = sl.DaylightConfig(4, "Veg", [(0, 0), (360, 100), (1320, 100), (1380, 0)])
            await a.send_access(0xC000, sl.vendor_opcode(sl.OP_SET_DAYLIGHT_CONFIG).to_bytes(3, "big") + cfg.to_bytes())
            for frame in a.client.sent:
                b._on_notify(None, bytearray(frame))
            return a, got, cfg
        with tempfile.TemporaryDirectory() as d:
            a, got, cfg = asyncio.run(run(d))
            self.assertEqual(len(got), 1)
            src, dst, op, params = got[0]
            self.assertEqual((src, dst, op), (0x10, 0xC000, sl.vendor_opcode(1)))
            self.assertEqual(sl.DaylightConfig.from_bytes(params), cfg)
            self.assertLessEqual(max(map(len, a.client.sent)), 20)   # MTU 23

    def test_seq_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "seq.json")
            s = sl.SeqStore(p)
            self.assertEqual([s.next(), s.next()], [1, 2])
            self.assertEqual(sl.SeqStore(p).next(), 3)

    def test_manufacturer_data(self):
        info = sl.parse_manufacturer_data(bytes.fromhex("01d30b050003009e39ad498c8d6786"))
        self.assertEqual((info["firmware"], info["node_address"], info["network_id"]), (5, 3, "9e39ad498c8d6786"))


class TestLightLevel(unittest.TestCase):
    CFG = {"enabled": True, "on": "13:00", "off": "07:00", "sunrise": [[30, 100]], "sunset": [[60, 100]]}

    def at(self, hh, mm):
        return rules.light_level(self.CFG, datetime(2026, 1, 1, hh, mm))

    def test_ramp_over_midnight(self):
        self.assertEqual(self.at(12, 59), 0)
        self.assertEqual(self.at(13, 0), 0)
        self.assertEqual(self.at(13, 15), 50)       # halber Sonnenaufgang
        self.assertEqual(self.at(18, 0), 100)
        self.assertEqual(self.at(2, 0), 100)        # nach Mitternacht
        self.assertEqual(self.at(6, 30), 50)        # halber Sonnenuntergang
        self.assertEqual(self.at(7, 0), 0)

    def test_curve_points(self):
        # Sonnenaufgang schnell auf 50 %, dann langsam; Untergang: 30 min vor Aus 100 %, 10 min vor Aus 20 %
        cfg = {**self.CFG, "sunrise": [[10, 50], [40, 100]], "sunset": [[10, 20], [30, 100]]}
        at = lambda hh, mm: rules.light_level(cfg, datetime(2026, 1, 1, hh, mm))
        self.assertEqual(at(13, 5), 25)             # schnell: nach 10 min schon 50 %
        self.assertEqual(at(13, 10), 50)
        self.assertEqual(at(13, 25), 75)            # dann langsamer bis 100 % nach 40 min
        self.assertEqual(at(13, 40), 100)
        self.assertEqual(at(6, 40), 60)             # 20 min vor Aus: zwischen 100 % und 20 %
        self.assertEqual(at(6, 55), 10)
        self.assertEqual(rules.curve_at([], 5), 1.0)

    def test_curve_validation_and_migration(self):
        from growcontroller import settings
        ok, err = settings.validate({"light": {"sunrise": [[10, 30], [45, 100]], "sunset": []}})
        self.assertFalse(err)
        for bad in ([[10, 30]], [[20, 50], [10, 100]], [[0, 100]], [[700, 100]], [[10, 101]], "x", [[1, 2, 3]]):
            _, err = settings.validate({"light": {"sunrise": bad}})
            self.assertIn("light.sunrise", err, bad)
        d = settings.migrate_light({"on": "06:00", "sunrise_min": 45, "sunset_min": 0})
        self.assertEqual(d, {"on": "06:00", "sunrise": [[45, 100]]})

    def test_without_ramp_and_disabled(self):
        self.assertEqual(rules.light_level({"enabled": True, "on": "06:00", "off": "00:00"}, datetime(2026, 1, 1, 6, 0)), 100)
        self.assertEqual(rules.light_level({**self.CFG, "enabled": False}, datetime(2026, 1, 1, 18, 0)), 0)


class FakeLink:
    def __init__(self, reachable=True):
        self.reachable, self.levels, self.addresses = reachable, [], set()

    def register(self, a):
        self.addresses.add(a)

    def set_level(self, a, pct):
        if not self.reachable:
            raise sl.Unreachable("aus")
        self.levels.append((a, pct))

    def info(self, a):
        return {"brightness": 51.0, "uptime": "11:30:00"}

    def close(self):
        pass


class TestBackend(unittest.TestCase):
    def test_level_scaling_and_dark_unreachable(self):
        link = FakeLink()
        be = backends.SanlightBackend(link, "0x0003", max=51, retry_s=0.05)
        self.assertEqual((be.level(True), be.level(50), be.level(False)), (51, 26, 0))
        self.assertEqual((be.level(38), be.level(39)), (0, 20))   # 38 % von 51 = 19 % -> unter 20 % aus
        be._send(50)
        self.assertEqual(link.levels, [(3, 26)])
        link.reachable = False
        be._send(0)                                   # stromlos und soll dunkel sein -> kein Fehler
        with self.assertRaises(sl.Unreachable):
            be._send(100)
        be.close()

    def test_minimum_output_during_sunrise(self):
        be = backends.SanlightBackend(FakeLink(), 3, max=60, retry_s=0.05)
        sent = []
        be.set = lambda v: sent.append(be.level(v))
        o = outputs.Output("lampe", be, role="light")
        o.apply_dim(10); o.apply_dim(25); o.apply_dim(30)   # 6 %, 15 %, 18 % tatsächlich: aus
        self.assertFalse(o.state)
        o.apply_dim(34)                                     # 20.4 % -> an mit 20 %
        self.assertTrue(o.state)
        o.apply_dim(10)                                     # Sonnenuntergang: unter 20 % -> aus
        self.assertFalse(o.state)
        self.assertEqual(sent, [0, 20, 0])                  # 0 nicht bei jedem Prozent erneut
        be.close()

    def test_output_dim_modes_and_shutdown(self):
        sent = []

        class Rec:
            initial, keep_on_shutdown, healthy, max = False, True, True, 100
            def level(self, v): return v
            def set(self, v): sent.append(v)
            def close(self): pass
        o = outputs.Output("lampe", Rec(), role="light")
        self.assertEqual(sent, [])                    # kein Ausschalten beim Start
        o.apply_dim(40.4); o.apply_dim(40.2)
        self.assertEqual(sent, [40])                  # nur bei Änderung senden
        o.mode = "on"; o.apply_dim(40)
        o.mode = "off"; o.apply_dim(40)
        self.assertEqual(sent, [40, 100, 0])
        self.assertFalse(o.state)
        o.shutdown()
        self.assertEqual(sent, [40, 100, 0])          # Beenden lässt die Lampen in Ruhe


class TestController(unittest.TestCase):
    def test_controller_dims_light_outputs(self):
        link = FakeLink()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sl.SanlightLink, "get", return_value=link) as get:
            cfg = {"interval_s": 5, "sensors": {"air": {"driver": "sim"}},
                   "sanlight": {"net_key": "00" * 16, "app_key": "11" * 16},
                   "outputs": {"links": {"type": "sanlight", "address": "0x0003", "max": 51, "role": "light"}},
                   "light": {"on": "13:00", "off": "07:00", "sunrise": [[60, 100]]},
                   "climate": {}, "irrigation": {"enabled": False, "interval_min": 60, "duration_s": 5}}
            c = controller.Controller(cfg, d)
            self.assertTrue(get.call_args.kwargs["seq_file"].endswith("sanlight_seq.json"))
            c.step(datetime(2026, 1, 1, 13, 30))
            o = c.outs["links"]
            self.assertEqual(o.level, 50)
            self.assertTrue(o.state)
            st = c.status()["outputs"]["links"]["dim"]
            self.assertEqual((st["level"], st["dimmer"]["brightness"]), (50, 51.0))
            c.step(datetime(2026, 1, 1, 8, 0))
            self.assertEqual(o.level, 0)
            c.shutdown()

    def test_lamp_slider(self):
        link = FakeLink()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sl.SanlightLink, "get", return_value=link):
            cfg = {"interval_s": 5, "sensors": {"air": {"driver": "sim"}},
                   "sanlight": {"net_key": "00" * 16, "app_key": "11" * 16},
                   "outputs": {"links": {"type": "sanlight", "address": "0x0003", "max": 51, "role": "light"}},
                   "light": {"on": "00:00", "off": "00:00"},
                   "climate": {}, "irrigation": {"enabled": False, "interval_min": 60, "duration_s": 5}}
            c = controller.Controller(cfg, d)
            c.set_dim("links", 65)
            time.sleep(0.3)                            # Worker-Thread sendet
            self.assertEqual(link.levels[-1], (3, 65))  # sofort gesendet, Licht ist an
            self.assertEqual(c.status()["outputs"]["links"]["dim"]["max"], 65)
            for bad in (101, -1, True, "x", None):
                with self.assertRaises(ValueError):
                    c.set_dim("links", bad)
            with self.assertRaises(ValueError):
                c.set_dim("nope", 50)
            c.shutdown()
            c2 = controller.Controller(json.loads(json.dumps(cfg)), d)   # Neustart: Wert bleibt
            self.assertEqual(c2.outs["links"].backend.max, 65)
            c2.shutdown()


if __name__ == "__main__":
    unittest.main()
