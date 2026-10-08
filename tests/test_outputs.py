import time
import unittest

from growcontroller import backends, outputs


class Rec:
    def __init__(self): self.calls = []
    def set(self, on): self.calls.append(on)
    def close(self): pass


class Flaky(backends.WorkerBackend):
    def __init__(self):
        self.n, self.log = 0, []
        super().__init__(resend_s=60, retry_s=0.05)

    def _send(self, on):
        self.n += 1
        if self.n < 3:
            raise RuntimeError("down")
        self.log.append(on)


class T(unittest.TestCase):
    def test_mode_override_and_min_switch(self):
        be = Rec(); o = outputs.Output("fan", be, min_switch_s=100)
        o.apply(True); o.apply(False)           # zweites Umschalten zu früh -> ignoriert
        self.assertEqual(be.calls, [False, True]); 
        o.mode = "off"; o.apply(True)           # 'off' darf Mindestzeit umgehen
        self.assertEqual(be.calls, [False, True, False])

    def test_worker_retries(self):
        b = Flaky(); b.set(True)
        for _ in range(100):
            if b.log: break
            time.sleep(0.05)
        b.close()
        self.assertEqual(b.log[0], True)

    def test_servo_payloads(self):
        class Hub:
            def __init__(s): s.sent = []
            def publish(s, t, p, r): s.sent.append((t, p))
        h = Hub(); b = backends.MqttServoBackend(h, "t", angle_on=80, angle_off=5, payload='{"angle":{angle}}')
        b.set(True); time.sleep(0.2); b.close()
        self.assertIn(("t", '{"angle":80}'), h.sent)


if __name__ == "__main__":
    unittest.main()


class TestDevices(unittest.TestCase):
    def test_meross_local_signature_and_payload(self):
        import hashlib, json, threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(body)
                out = json.dumps({"header": {"method": "SETACK"}, "payload": {}}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        b = backends.MerossLocalBackend(f"127.0.0.1:{srv.server_port}", "KEY")
        b.set(True)
        for _ in range(60):
            if seen: break
            time.sleep(0.05)
        b.close(); srv.shutdown()
        h = seen[0]["header"]
        self.assertEqual(h["sign"], hashlib.md5(f"{h['messageId']}KEY{h['timestamp']}".encode()).hexdigest())
        self.assertEqual(seen[0]["payload"], {"togglex": {"channel": 0, "onoff": 1}})

    def test_tuya_value_dp_and_version_probe(self):
        calls = []

        class Dev:
            def __init__(s, v): s.v = v
            def status(s): return {"Error": "x"} if s.v == 3.3 else {"dps": {"2": 0}}
            def set_value(s, dp, val):
                calls.append((s.v, dp, val))
                return {"Error": "x"} if s.v == 3.3 else {"dps": {}}

        t = backends.TuyaBackend.__new__(backends.TuyaBackend)
        t.cfg, t.versions, t.dp, t.value_on, t.value_off, t.dev = ("id", "Auto", "k"), [3.3, 3.4], 2, 30, 65, None
        t._device = lambda v: Dev(v)
        t._send(True); t._send(False)
        self.assertEqual(calls, [(3.3, 2, 30), (3.4, 2, 30), (3.4, 2, 65)])


class TestTuyaNoRewrite(unittest.TestCase):
    def test_skip_write_when_value_matches(self):
        writes, state = [], {"2": 65}

        class Dev:
            def status(s): return {"dps": dict(state)}
            def set_value(s, dp, val): writes.append(val); state[str(dp)] = val; return {"dps": {}}

        t = backends.TuyaBackend.__new__(backends.TuyaBackend)
        t.cfg, t.versions, t.dp, t.value_on, t.value_off, t.dev = ("id", "1.2.3.4", "k"), [3.4], 2, 30, 65, None
        t._device = lambda v: Dev()
        t._send(False)                      # steht schon auf 65 -> kein Schreiben (kein Piepen)
        self.assertEqual(writes, [])
        t._send(True); t._send(True)        # einmal schreiben, danach steht es richtig
        self.assertEqual(writes, [30])
        state["2"] = 65                     # jemand stellt am Gerät um -> wird korrigiert
        t._send(True)
        self.assertEqual(writes, [30, 30])
