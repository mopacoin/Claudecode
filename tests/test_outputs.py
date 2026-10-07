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
