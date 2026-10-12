"""Spider Farmer GGS: Protokoll gegen echte Pakete aus einem Bluetooth-Mitschnitt der App."""
import json
import queue
import threading
import time
import unittest

from growcontroller import backends, outputs, sensors, spiderfarmer as sf

# App -> Controller: O5 aus (setConfigField), verschlüsselt
SETCFG = bytes.fromhex(
    "aaaa000300ae00028cf7000000a00000000000a022e6abf044591e5e2c4c479e4cbf3bc1a4eb4ed5e738b448b1568bd03d0400a6"
    "235c478a0302c09e2e1c10e9947b535f95f999af5c227742518d694396d7624db7d0372bf50e65fbc66696e1475e706c05d12f4b"
    "ef6300ab40e9738cee203abd64c98886dda863e5f0f8b3b42a749e927749a3e36738587a68a5e3f4db17a300cf383e88c5aa221b"
    "aef53463d8f5783f5e2b5abeda3ada44b84756c1b685603224bd")
# Controller -> App: getDevSta in drei Rahmen
DEV = [bytes.fromhex(h) for h in (
    "aaaa0003019e0001fde80000036d0000000001907b226d6574686f64223a22676574446576537461222c226d73674964223a2231"
    "373931383130353936343730222c22706964223a22383046314232423143374630222c2270636f6465223a313030322c22756964"
    "223a22313535383535222c22555443223a313739313831303536342c22636f6465223a3230302c226d7367223a226f6b222c2264"
    "617461223a7b226f75746c6574223a7b2270736d6f6465223a312c224f31223a7b226f6e223a317d2c224f32223a7b226f6e223a"
    "317d2c224f33223a7b226f6e223a317d2c224f34223a7b226f6e223a317d2c224f35223a7b226f6e223a317d7d2c22616c61726d"
    "4c617374223a7b226964223a31352c2265706f6368223a313739313736363735322c2264657654797065223a31362c22616c6172"
    "6d54797065223a337d2c2273656e736f72223a7b2274656d70223a31392e372c2268756d69223a36302e342c22767064223a302e"
    "39312c2274656d70536f696c223a32312e312c2268756d69536f696c223a302e352c224543536f696c223a312e38382c22697344"
    "6179456e294c",
    "aaaa0003019e0001fde80000036d00000190019076546172676574223a312c22697344617953656e736f72223a307d2c2273656e"
    "736f7273223a5b7b226964223a22617667222c2274656d70536f696c223a32312e312c2268756d69536f696c223a302e352c2245"
    "43536f696c223a312e38387d2c7b226964223a2233383335333233383036333041373735222c2274797065223a322c2274656d70"
    "536f696c223a32312c2268756d69536f696c223a302e362c224543536f696c223a322e312c2274797065436f6465223a34333532"
    "317d2c7b226964223a2233383335333233383043333034303344222c2274797065223a322c2274656d70536f696c223a32312e35"
    "2c2268756d69536f696c223a302e342c224543536f696c223a312e382c2274797065436f6465223a34333532317d2c7b22696422"
    "3a2233383335333233383130333039353337222c2274797065223a322c2274656d70536f696c223a32302e382c2268756d69536f"
    "696c223a312e312c224543536f696c223a312e372c2274797065436f6465223a34333532317d2c7b226964223a22333833353332"
    "33383044ff2c",
    "aaaa0003005b0001fde80000036d00000320004d333041373243222c2274797065223a322c2274656d70536f696c223a32312c22"
    "68756d69536f696c223a302c224543536f696c223a312e392c2274797065436f6465223a34333532317d5d7d7df752")]


def dev_sta(o5):
    return {"method": "getDevSta", "code": 200, "data": {"outlet": {"O5": {"on": o5}}, "sensor": {"temp": 20}}}


class Fake:
    """Ersetzt den Bluetooth-Socket: nimmt Rahmen entgegen, liefert Antwort-Rahmen."""
    inst = None

    def __init__(self, mac):
        self.written, self.rx = [], queue.Queue()
        Fake.inst = self
        self.push(dev_sta(1))

    def push(self, msg):
        for f in sf.build_frames(json.dumps(msg).encode(), typ=1):
            self.rx.put(f)

    def recv(self):
        try:
            return self.rx.get(timeout=0.05)
        except queue.Empty:
            return None

    def write(self, frame):
        self.written.append(frame)
        req = json.loads(sf.decrypt(frame[20:-2]))
        if req["method"] == "setConfigField":
            self.push(dev_sta(req["params"]["O5"]["mOnOff"]))

    def close(self):
        pass


class TestProtocol(unittest.TestCase):
    def test_request_matches_app_bytes(self):
        app = json.loads(sf.decrypt(SETCFG[20:-2]))
        self.assertEqual(app["params"], {"keyPath": ["outlet", "O5"], "O5": {"modeType": 0, "mOnOff": 0}})
        req = sf.request("setConfigField", "80F1B2B1C7F0", "155855", app["params"], msg_id=app["msgId"])
        self.assertEqual(sf.build_frames(sf.encrypt(req)), [SETCFG])

    def test_reassemble_dev_sta(self):
        rx = sf.Reassembler()
        msgs = rx.feed(DEV[0]) + rx.feed(DEV[1][:100]) + rx.feed(DEV[1][100:] + DEV[2])
        self.assertEqual(len(msgs), 1)
        r = sf.readings(msgs[0])
        self.assertEqual((r["temp"], r["hum"], r["vpd"]), (19.7, 60.4, 0.91))
        self.assertEqual((r["soil_temp"], r["soil_hum"], r["soil_ec"]), (21.1, 0.5, 1.88))
        self.assertEqual(r["soil1_ec"], 2.1)       # Sonde ...0630A775 (sortiert nach ID)
        self.assertEqual(sum(k.endswith("_ec") for k in r), 5)

    def test_unframed_json_between_frames(self):
        # GGS ohne Sensoren: kurze Antworten kommen als reines JSON, auch über zwei Pakete verteilt
        raw = (b'{"method":"getDevSta","pid":"80F1B2B2E850","pcode":1002,"uid":"","UTC":1791813072,"code":200,"msg":"ok",'
               b'"data":{"outlet":{"psmode":0,"O1":{"on":1},"O2":{"on":1},"O3":{"on":1},"O4":{"on":0},"O5":{"on":0}}}}')
        rx = sf.Reassembler()
        msgs = rx.feed(raw[:120]) + rx.feed(raw[120:] + DEV[0]) + rx.feed(DEV[1] + DEV[2] + raw)
        self.assertEqual([m["method"] for m in msgs], ["getDevSta"] * 3)
        self.assertEqual(msgs[0]["data"]["outlet"]["O4"], {"on": 0})
        self.assertEqual(sf.readings(msgs[0]), {})       # keine Sensoren -> keine Messwerte
        self.assertEqual(rx.buf, b"")

    def test_bad_crc_is_dropped(self):
        broken = DEV[2][:-1] + bytes([DEV[2][-1] ^ 1])
        self.assertEqual(sf.Reassembler().feed(DEV[0] + DEV[1] + broken), [])

    def test_pid_from_ble_mac(self):
        self.assertEqual(sf.GGSLink("80:F1:B2:B1:C7:F2", start=False).pid, "80F1B2B1C7F0")


class TestLink(unittest.TestCase):
    def setUp(self):
        sf.GGSLink._links.clear()
        sf.GGSLink.transport = Fake

    def tearDown(self):
        for l in sf.GGSLink._links.values():
            l.close()
        sf.GGSLink._links.clear()
        sf.GGSLink.transport = sf._RawATT

    def wait_data(self, link):
        for _ in range(100):
            if link.dev_sta:
                return
            time.sleep(0.05)
        self.fail("keine Daten")

    def test_sensor_and_outlet_share_one_link(self):
        s = sensors.create({"driver": "spiderfarmer", "mac": "80:f1:b2:b1:c7:f2", "uid": "155855"})
        b = backends.SpiderFarmerBackend("80:F1:B2:B1:C7:F2", 5, uid="155855", retry_s=0.05)
        self.assertIs(s.link, b.link)
        self.wait_data(s.link)
        self.assertEqual(s.read()["temp"], 20)
        b.set(False)
        for _ in range(100):
            if b.healthy:
                break
            time.sleep(0.05)
        b.close()
        self.assertTrue(b.healthy)
        self.assertFalse(s.link.outlet(5))
        sent = [json.loads(sf.decrypt(f[20:-2])) for f in Fake.inst.written]
        cfg = [r for r in sent if r["method"] == "setConfigField"]
        self.assertEqual(len(cfg), 1)
        self.assertEqual(cfg[0]["uid"], "155855")

    def test_no_write_when_state_already_matches(self):
        link = sf.GGSLink.get("80:F1:B2:B1:C7:F2")
        self.wait_data(link)
        link.set_outlet(5, True)                   # meldet schon "an"
        time.sleep(0.2)
        self.assertFalse(any(json.loads(sf.decrypt(f[20:-2]))["method"] == "setConfigField" for f in Fake.inst.written))

    def test_output_type_registered(self):
        _, outs, _ = outputs.create_all({"outputs": {"o5": {"type": "spiderfarmer", "mac": "80:F1:B2:B1:C7:F2", "outlet": 5}}})
        self.assertIsInstance(outs["o5"].backend, backends.SpiderFarmerBackend)
        outs["o5"].backend.close()

    def test_sensor_stale_raises(self):
        s = sensors.create({"driver": "spiderfarmer", "mac": "80:F1:B2:B1:C7:F2", "max_age_s": 0})
        self.wait_data(s.link)
        time.sleep(0.01)
        with self.assertRaises(RuntimeError):
            s.read()


class Reported:
    """Backend mit gemeldetem Zustand (wie SpiderFarmerBackend), ohne Thread."""
    def __init__(self, actual=True):
        self.calls, self.now, self.desired = [], actual, None
    def set(self, on):
        self.calls.append(on); self.desired = on
        if on is not None:
            self.now = on
    def actual(self): return self.now
    def close(self): pass


class TestManualRole(unittest.TestCase):
    def test_free_mode_never_switches(self):
        be = Reported(actual=True)
        o = outputs.Output("ggs_o1", be, role="manual")
        o.apply(False); o.apply(True); o.shutdown()
        self.assertEqual(be.calls, [])          # kein Ausschalten beim Start/Beenden, keine Regel
        self.assertTrue(o.state)                # zeigt den gemeldeten Zustand

    def test_on_off_by_hand_then_free(self):
        be = Reported(actual=True)
        o = outputs.Output("ggs_o5", be, role="manual")
        o.mode = "off"; o.apply(True); o.apply(True)
        self.assertEqual(be.calls, [False]);  self.assertFalse(o.state)
        o.mode = "on"; o.apply(False)
        self.assertEqual(be.calls, [False, True]); self.assertTrue(o.state)
        o.mode = "auto"; o.apply(False)
        self.assertEqual(be.calls, [False, True, None])   # Auffrischen beenden, Zustand bleibt
        self.assertTrue(o.state)


class TestManualEvents(unittest.TestCase):
    def test_first_report_is_not_logged_as_switching(self):
        import tempfile
        from growcontroller import controller
        with tempfile.TemporaryDirectory() as tmp:
            ctl = controller.Controller({"interval_s": 5, "sensors": {"air": {"driver": "sim"}}, "outputs": {},
                                         "light": {"on": "06:00", "off": "00:00"}, "climate": {}, "irrigation": {}}, tmp)
            be = Reported(actual=None)
            ctl.outs["ggs_o1"] = outputs.Output("ggs_o1", be, role="manual")
            ctl._prev_state["ggs_o1"] = None
            msgs = lambda: [e["msg"] for e in ctl.events if e["msg"].startswith("ggs_o1")]
            ctl.step()                       # noch keine Meldung
            be.now = True; ctl.step()        # erste Meldung: an -> kein "EIN" im Protokoll
            self.assertEqual(msgs(), [])
            be.now = False; ctl.step()       # echte Änderung (z. B. in der App geschaltet)
            self.assertEqual(msgs(), ["ggs_o1: AUS"])


if __name__ == "__main__":
    unittest.main()
