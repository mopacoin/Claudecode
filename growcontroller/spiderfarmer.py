"""Spider Farmer Cloud – Daten aus der App auslesen (nur lesend).

Protokoll nachgebaut nach github.com/zeroXmrcl/spider-farmer-cloud-api (MIT, CA-Zertifikat in
certs/spiderfarmer-ca.pem übernommen) und den Protokoll-Notizen von github.com/iceboerg00/spiderfarmer-bridge.
Inoffiziell: Spider Farmer kann das jederzeit ändern oder blockieren.

Test auf dem Pi:  SF_EMAIL=… SF_PASSWORD=… python -m growcontroller.spiderfarmer
"""
import base64
import json
import logging
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("grow.spiderfarmer")

API = "https://api.spider-farmer.com"
MQTT_HOST, MQTT_PORT = "sf.mqtt.spider-farmer.com", 8883
KEY, IV = b"Meizhi1234567890", b"1234567890123456"  # fester Schlüssel der App
CA_FILE = os.path.join(os.path.dirname(__file__), "certs", "spiderfarmer-ca.pem")


class CloudError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code}: {msg}")
        self.code = str(code)


# --- Verschlüsselung der REST-Aufrufe (AES-128-CBC, PKCS7, Base64) ---
def _cipher():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher(algorithms.AES(KEY), modes.CBC(IV))


def encrypt_body(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode()
    pad = 16 - len(raw) % 16
    enc = _cipher().encryptor()
    return base64.b64encode(enc.update(raw + bytes([pad]) * pad) + enc.finalize()).decode()


def decode_response(text):
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    dec = _cipher().decryptor()
    raw = dec.update(base64.b64decode(text.strip('"'))) + dec.finalize()
    return json.loads(raw[:-raw[-1]].decode())


def prefix_for(product_type):
    """MQTT-Topic-Präfix: LC = Lichtcontroller, PS = Steckdosenleiste, sonst CB (Control Box)."""
    tokens = re.findall(r"[A-Z0-9]+", str(product_type).upper())
    if "LC" in tokens or any("LIGHT" in t for t in tokens):
        return "LC"
    if any(t in ("PS10", "AC10", "PS5", "AC5", "PS") for t in tokens):
        return "PS"
    return "CB"


def parse_status(msg):
    """getDevSta-Antwort -> flache Messwerte. Steckdosen als o1…o10 (1/0)."""
    d = msg.get("data", msg) if isinstance(msg, dict) else None
    if not isinstance(d, dict):
        return {}
    out = {}
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    sensor = d.get("sensor") if isinstance(d.get("sensor"), dict) else {}
    for sf, k in (("temp", "temp"), ("humi", "hum"), ("vpd", "vpd"), ("co2", "co2"), ("ppfd", "ppfd")):
        if num(sensor.get(sf)):
            out[k] = sensor[sf]
    outlets = d.get("outlet") if isinstance(d.get("outlet"), dict) else {}
    for key, val in outlets.items():
        if key[:1] == "O" and key[1:].isdigit() and isinstance(val, dict):
            on = val.get("on", val.get("mOnOff"))
            if on is not None:
                out[f"o{key[1:]}"] = 1 if on else 0
    for mod in ("light", "light2", "fan", "blower"):
        b = d.get(mod)
        if isinstance(b, dict):
            on, lvl = b.get("on", b.get("mOnOff")), b.get("level", b.get("mLevel"))
            if on is not None:
                out[f"{mod}_on"] = 1 if on else 0
            if num(lvl):
                out[f"{mod}_level"] = lvl
    return out


class Cloud:
    def __init__(self, email, password, device_id=None, timezone="Europe/Berlin", timeout=20):
        self.email, self.password, self.timezone, self.timeout = email, password, timezone, timeout
        self.device_id = device_id or str(uuid.uuid5(uuid.NAMESPACE_DNS, "growcontroller-" + email))  # stabil: wirkt wie ein Handy
        self._last_req = 0

    def call(self, path, body, token=None):
        now = int(time.time())
        req_id = max(time.time_ns() // 1_000_000, self._last_req + 1)  # muss eindeutig sein, sonst "request replay"
        self._last_req = req_id
        head = {"reqId": req_id, "appVersion": "2.5.2", "osType": "iOS", "osVersion": "27.0", "deviceType": "iPhone",
                "deviceId": self.device_id, "netType": "wifi", "timestamp": now, "wifiName": "unknown_data"}
        if token:
            head["token"] = token
        head.update(timezone=self.timezone, language="English")
        req = urllib.request.Request(API + path, data=None, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "Dart/3.5 (dart:io)",
                                              "systemdata": json.dumps(head, separators=(",", ":"))})
        if body is not None:
            req.data = encrypt_body(body).encode()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = decode_response(r.read().decode())
        except urllib.error.HTTPError as e:
            raise CloudError(e.code, "HTTP-Fehler") from e
        except (urllib.error.URLError, OSError) as e:
            raise CloudError("network", str(e)) from e
        if str(payload.get("code")) != "000":
            raise CloudError(payload.get("code", "?"), payload.get("msg", "Anfrage abgelehnt"))
        return payload

    def login(self):
        d = self.call("/api/ios/ulogin/mailLogin/v2", {"email": self.email, "loginMethod": 1, "password": self.password}).get("data") or {}
        if not (d.get("token") and d.get("mqttName") and d.get("mqttPwd") and d.get("userId")):
            raise CloudError("parse", "Login-Antwort unvollständig")
        return {"token": d["token"], "mqtt_name": str(d["mqttName"]), "mqtt_pwd": str(d["mqttPwd"]), "user_id": str(d["userId"])}

    def devices(self, sess):
        out = []
        for room in self.call("/api/ios/udm/getUserRooms/v2", None, sess["token"]).get("data") or []:
            body = {"belonginRoomId": int(room["id"]), "userId": int(sess["user_id"])}
            for dv in self.call("/api/ios/udm/getDeviceList/v2", body, sess["token"]).get("data") or []:
                serial = str(dv.get("deviceSerialnum") or "").replace(":", "").upper()
                if serial:
                    out.append({"serial": serial, "name": dv.get("deviceName") or serial, "product": dv.get("productType", ""),
                                "prefix": prefix_for(dv.get("productType", "")), "online": dv.get("connectStatus") in (1, "1", True),
                                "room": room.get("roomName", "")})
        return out


def dev_command(method, serial, user_id):
    """Befehl im Format, das die Cloud selbst an das Gerät schickt (pid/uid oben, mitgeschnitten von
    cobragt2000/spider_farmer_bridge); params.pid zusätzlich wie im zeroXmrcl-Client."""
    return {"method": method, "pid": serial, "params": {"pid": serial}, "msgId": str(int(time.time() * 1000)), "uid": str(user_id)}


def _client_id(user_id):
    tail = f"{int(time.time() * 1000)}{secrets.token_hex(2)}"
    cid = f"{user_id}_{tail}"
    return cid if len(cid) <= 23 else f"{user_id[:23 - 13]}_{tail[-12:]}"[:23]


def mqtt_client(sess, on_message, on_connect):
    import paho.mqtt.client as mqtt
    kw = dict(client_id=_client_id(sess["user_id"]), protocol=mqtt.MQTTv31, clean_session=True)
    try:
        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, **kw)
    except AttributeError:  # paho < 2
        c = mqtt.Client(**kw)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=CA_FILE)
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    c.tls_set_context(ctx)
    c.username_pw_set(sess["mqtt_name"], sess["mqtt_pwd"])
    c.on_message, c.on_connect = on_message, on_connect
    return c


class SpiderFarmerSensor:
    """Sensor-Treiber "spiderfarmer": liest Steckdosen (und ggf. Sensoren) eines Geräts aus der Cloud.
    device: Seriennummer oder Name (Standard: erste Steckdosenleiste)."""

    def __init__(self, email, password, device=None, poll_s=60, max_age_s=300, outlet_names=None, **_):
        self.cloud = Cloud(email, password)
        self.want, self.poll_s, self.max_age = device, max(15, poll_s), max_age_s
        self.latest, self.t, self.error, self.dev = {}, 0.0, None, None
        self.meta = {"kind": "spiderfarmer", "outlet_names": outlet_names or {}}
        threading.Thread(target=self._run, daemon=True, name="spiderfarmer").start()

    def _pick(self, devs):
        if self.want:
            w = str(self.want).replace(":", "").upper()
            return next((d for d in devs if d["serial"] == w or str(d["name"]).upper() == w), None)
        return next((d for d in devs if d["prefix"] == "PS"), devs[0] if devs else None)

    def _run(self):
        sess = None
        while True:
            try:
                if sess is None:
                    sess = self.cloud.login()
                    devs = self.cloud.devices(sess)
                    self.dev = self._pick(devs)
                    if not self.dev:
                        raise CloudError("device", "Gerät nicht gefunden: " + ", ".join(f"{d['name']} ({d['serial']})" for d in devs))
                    self.meta.update(device=self.dev["name"], product=self.dev["product"], serial=self.dev["serial"])
                    log.info("Spider Farmer: %s (%s)", self.dev["name"], self.dev["product"])
                self._session(sess)
            except CloudError as e:
                self.error = str(e)
                if e.code in ("100", "101"):  # falsches Passwort: nicht weiter probieren, sonst sperrt die Cloud den Login
                    self.error = "Login abgelehnt (Passwort?) – angehalten, um eine Kontosperre zu vermeiden. Nach Korrektur neu starten."
                    log.error("Spider Farmer: %s", self.error)
                    return
                log.warning("Spider Farmer: %s – neuer Versuch in 5 min", e)
                sess = None
                time.sleep(300)
            except Exception as e:  # MQTT/TLS-Probleme: Verbindung neu aufbauen, Session behalten
                self.error = f"Verbindung: {e!r}"
                log.warning("Spider Farmer: %s – neuer Versuch in 60 s", self.error)
                time.sleep(60)

    def _session(self, sess):
        up = f"SF/GGS/{self.dev['prefix']}/API/UP/{self.dev['serial']}"
        down = f"SF/GGS/{self.dev['prefix']}/API/DOWN/{self.dev['serial']}"
        state = {"ok": None}

        def on_connect(c, _u, _f, rc, *_):
            state["ok"] = getattr(rc, "is_failure", None) is False or rc == 0
            if state["ok"]:
                c.subscribe(up, qos=0)

        def on_message(_c, _u, m):
            try:
                vals = parse_status(json.loads(m.payload.decode(errors="replace")))
            except ValueError:
                return
            if vals:
                self.latest.update(vals)
                self.t, self.error = time.monotonic(), None

        c = mqtt_client(sess, on_message, on_connect)
        c.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        c.loop_start()
        try:
            for _ in range(100):
                if state["ok"] is not None:
                    break
                time.sleep(0.1)
            if not state["ok"]:
                raise CloudError("mqtt", "Broker hat die Verbindung abgelehnt")
            while True:
                if not c.is_connected():
                    raise ConnectionError("MQTT getrennt")
                c.publish(down, json.dumps(dev_command("getDevSta", self.dev["serial"], sess["user_id"]), separators=(",", ":")), qos=0)
                time.sleep(self.poll_s)
        finally:
            c.loop_stop()
            c.disconnect()

    def read(self):
        if not self.latest or time.monotonic() - self.t > self.max_age:
            raise RuntimeError(self.error or "noch keine Daten aus der Spider-Farmer-Cloud")
        return dict(self.latest)


def _cli():
    """Anmelden, Geräte auflisten und 20 s lang die Rohdaten des gewählten Geräts zeigen."""
    email, pw = os.environ.get("SF_EMAIL"), os.environ.get("SF_PASSWORD")
    if not email or not pw:
        raise SystemExit("SF_EMAIL und SF_PASSWORD setzen (z. B. aus .env laden)")
    cloud = Cloud(email, pw)
    sess = cloud.login()
    print("Login ok.")
    devs = cloud.devices(sess)
    for d in devs:
        print(f"  {d['name']:20} {d['product']:12} {d['prefix']}  {d['serial']}  online={d['online']}  Raum={d['room']}")
    dev = next((d for d in devs if d["prefix"] == "PS"), devs[0] if devs else None)
    if not dev:
        raise SystemExit("Keine Geräte im Konto.")
    up, down = (f"SF/GGS/{dev['prefix']}/API/{x}/{dev['serial']}" for x in ("UP", "DOWN"))
    every = f"SF/GGS/+/API/+/{dev['serial']}"  # alle Richtungen/Präfixe dieses Geräts
    seen = {"n": 0}

    def on_connect(cl, _u, _f, rc, *_):
        print(f"MQTT verbunden: {rc}")
        for t in (up, every):
            print(f"  abonniere {t} (mid {cl.subscribe(t, qos=0)[1]})")

    def on_subscribe(_c, _u, mid, granted, *_):
        codes = [getattr(g, "value", g) for g in (granted if isinstance(granted, (list, tuple)) else [granted])]
        print(f"  Abo mid {mid}: {codes}" + ("  <- ABGELEHNT (128)" if 128 in codes else ""))

    def on_message(_c, _u, m):
        seen["n"] += 1
        txt = m.payload.decode(errors="replace")
        print(f"\n[{time.strftime('%H:%M:%S')}] {m.topic}\n  Roh: {txt[:1500]}")
        try:
            print("  Erkannt:", parse_status(json.loads(txt)))
        except ValueError:
            pass

    def on_disconnect(*a):
        print(f"MQTT getrennt: {a[-2] if len(a) > 3 else a}")

    c = mqtt_client(sess, on_message, on_connect)
    c.on_subscribe, c.on_disconnect = on_subscribe, on_disconnect
    c.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    c.loop_start()
    time.sleep(3)
    print(f"\nFrage {dev['name']} 60 s lang alle 15 s ab (Format wie die Cloud) …")
    for _ in range(4):
        cmd = dev_command("getDevSta", dev["serial"], sess["user_id"])
        info = c.publish(down, json.dumps(cmd, separators=(",", ":")))
        print(f"  -> getDevSta an {down} (rc {info.rc})")
        time.sleep(15)
    c.loop_stop()
    c.disconnect()
    print(f"\nFertig, {seen['n']} Nachricht(en) empfangen.")


if __name__ == "__main__":
    _cli()
