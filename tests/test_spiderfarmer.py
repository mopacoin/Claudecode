import json
import unittest

from growcontroller import spiderfarmer as sf


class TestSpiderFarmer(unittest.TestCase):
    def test_encrypt_roundtrip(self):
        body = {"email": "a@b.de", "loginMethod": 1, "password": "pw"}
        enc = sf.encrypt_body(body)
        self.assertEqual(sf.decode_response(enc), body)
        self.assertEqual(sf.decode_response('"' + enc + '"'), body)  # Antwort kann in Anführungszeichen kommen
        self.assertEqual(sf.decode_response('{"code":"000"}'), {"code": "000"})

    def test_prefix(self):
        self.assertEqual(sf.prefix_for("GGS-PS5"), "PS")
        self.assertEqual(sf.prefix_for("AC10"), "PS")
        self.assertEqual(sf.prefix_for("UPS"), "CB")
        self.assertEqual(sf.prefix_for("LC"), "LC")
        self.assertEqual(sf.prefix_for("CB"), "CB")

    def test_parse_ps5(self):
        msg = {"method": "getDevSta", "code": 200, "data": {
            "outlet": {"psmode": 1, "O1": {"on": 1}, "O2": {"on": 0}, "O3": {"mOnOff": 1}, "O5": {"on": 0}},
            "sensor": {"temp": 24.3, "humi": 55.1, "vpd": 1.1}}}
        self.assertEqual(sf.parse_status(msg), {"temp": 24.3, "hum": 55.1, "vpd": 1.1, "o1": 1, "o2": 0, "o3": 1, "o5": 0})
        self.assertEqual(sf.parse_status({"code": 200, "msg": "ok"}), {})
        self.assertEqual(sf.parse_status("kein json-objekt"), {})

    def test_client_id_length(self):
        self.assertLessEqual(len(sf._client_id("123456789012345678")), 23)
        self.assertTrue(sf._client_id("36656").startswith("36656_"))

    def test_ca_file_present(self):
        self.assertIn("BEGIN CERTIFICATE", open(sf.CA_FILE).read())


if __name__ == "__main__":
    unittest.main()
