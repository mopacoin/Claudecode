"""Spider Farmer GGS-Controller (Steckdosenleiste PS5 mit Klima-/Bodensensoren) lokal per Bluetooth.

Protokoll (aus der App 2.5.2 rekonstruiert, Firmware 3.2):
  GATT-Service 0x00FF: Notify 0xFF01 (Handle 0x2A, CCCD 0x2B), Write 0xFF02 (Handle 0x2D)
  Rahmen (big endian): AA AA | 00 03 | len:u16 (ab Byte 6, ohne CRC) | typ:u16 (2 = Anfrage, 1 = Antwort)
    | datacrc:u16 (CRC-16/MODBUS der gesamten Nutzdaten) | gesamt:u32 | offset:u32 | chunk:u16 | daten | crc:u16
  Anfragen: JSON, AES-128-CBC/PKCS7. Antworten: JSON im Klartext, in Stücken zu 400 Byte.
  Der Controller schickt getDevSta (~6 s) und getSysSta (~10 s) von selbst, sobald Notify an ist.
  Steckdose schalten: setConfigField auf outlet.O<n> mit modeType 0 (manuell) und mOnOff 0/1.

Die BlueZ-Dienstsuche bleibt bei diesem ESP32 hängen, deshalb spricht die Verbindung ATT direkt über
einen L2CAP-LE-Socket (CID 4) – braucht root (der Dienst läuft als root).
Solange der Pi verbunden ist, kann sich die App nicht per Bluetooth verbinden (nur eine Verbindung).
"""
import json
import logging
import os
import queue
import socket
import struct
import threading
import time

log = logging.getLogger("grow.spiderfarmer")

KEY = b"mKli62mtym9j6Odi"
IV = b"h411AfTnVVusvsjE"
CHUNK = 400
H_NOTIFY, H_CCCD, H_WRITE = 0x2A, 0x2B, 0x2D


def crc16(data):
    """CRC-16/MODBUS."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _cipher():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher(algorithms.AES(KEY), modes.CBC(IV))


def encrypt(obj):
    from cryptography.hazmat.primitives import padding
    p = padding.PKCS7(128).padder()
    raw = p.update(json.dumps(obj, separators=(",", ":")).encode()) + p.finalize()
    e = _cipher().encryptor()
    return e.update(raw) + e.finalize()


def decrypt(data):
    from cryptography.hazmat.primitives import padding
    d = _cipher().decryptor()
    u = padding.PKCS7(128).unpadder()
    return u.update(d.update(data) + d.finalize()) + u.finalize()


def build_frames(payload, typ=2):
    dcrc, frames = crc16(payload), []
    for off in range(0, len(payload), CHUNK):
        part = payload[off:off + CHUNK]
        body = struct.pack(">HHIIH", typ, dcrc, len(payload), off, len(part)) + part
        frame = b"\xAA\xAA\x00\x03" + struct.pack(">H", len(body)) + body
        frames.append(frame + struct.pack(">H", crc16(frame)))
    return frames


def request(method, pid, uid, params=None, msg_id=None):
    """Anfrage wie die App (gleiche Feldreihenfolge)."""
    msg_id = msg_id or str(int(time.time() * 1000))
    if params is None:
        return {"method": method, "msgId": msg_id, "pid": pid, "uid": uid}
    return {"method": method, "pid": pid, "params": params, "msgId": msg_id, "uid": uid}


class Reassembler:
    """Setzt Notify-Daten zu JSON-Nachrichten zusammen (Rahmen können über mehrere Pakete verteilt sein)."""

    def __init__(self):
        self.buf, self.parts = b"", {}

    def feed(self, data):
        out = []
        self.buf += bytes(data)
        while self.buf:
            if self.buf[:1] == b"{":  # kurze Antworten kommen ohne Rahmen als reines JSON
                try:
                    end = json.JSONDecoder().raw_decode(self.buf.decode("latin-1"))[1]  # latin-1: Index = Byte
                except ValueError:
                    if len(self.buf) < 4096 and b"\xAA\xAA" not in self.buf:
                        break  # noch unvollständig
                    self._resync()
                    continue
                try:
                    out.append(json.loads(self.buf[:end]))
                except ValueError:
                    pass
                self.buf = self.buf[end:]
                continue
            if self.buf[:2] != b"\xAA\xAA":
                self._resync()
                continue
            if len(self.buf) < 6:
                break
            n = struct.unpack(">H", self.buf[4:6])[0] + 8
            if len(self.buf) < n:
                break
            frame, self.buf = self.buf[:n], self.buf[n:]
            if crc16(frame[:-2]) != struct.unpack(">H", frame[-2:])[0]:
                continue
            _typ, dcrc, total, off, ln = struct.unpack(">HHIIH", frame[6:20])
            chunks = self.parts.setdefault(dcrc, {})
            chunks[off] = frame[20:20 + ln]
            if sum(len(c) for c in chunks.values()) >= total:
                del self.parts[dcrc]
                data = b"".join(chunks[k] for k in sorted(chunks))
                try:
                    out.append(json.loads(data if data.lstrip().startswith(b"{") else decrypt(data)))
                except ValueError:
                    pass
        return out

    def _resync(self):
        """Bis zum nächsten Rahmen- oder JSON-Anfang verwerfen."""
        cands = [i for i in (self.buf.find(b"\xAA\xAA", 1), self.buf.find(b'{"', 1)) if i > 0]
        self.buf = self.buf[min(cands):] if cands else b""


def readings(dev_sta):
    """getDevSta -> flaches dict für den Controller (Zahlen; Boden-Mittelwert und je Sonde soil1_…)."""
    d = dev_sta.get("data", {})
    s, out = d.get("sensor", {}), {}
    for src, dst in (("temp", "temp"), ("humi", "hum"), ("vpd", "vpd"), ("co2", "co2"), ("ppfd", "ppfd"),
                     ("tempSoil", "soil_temp"), ("humiSoil", "soil_hum"), ("ECSoil", "soil_ec")):
        if isinstance(s.get(src), (int, float)):
            out[dst] = s[src]
    probes = [x for x in d.get("sensors", []) if x.get("id") != "avg"]
    for i, x in enumerate(sorted(probes, key=lambda x: x.get("id", "")), 1):
        for src, dst in (("tempSoil", "temp"), ("humiSoil", "hum"), ("ECSoil", "ec")):
            if isinstance(x.get(src), (int, float)):
                out[f"soil{i}_{dst}"] = x[src]
    return out


class _RawATT:
    """ATT über L2CAP-LE-Socket (Linux)."""

    def __init__(self, mac):
        import ctypes

        class Addr(ctypes.Structure):
            _fields_ = [("family", ctypes.c_ushort), ("psm", ctypes.c_ushort), ("bdaddr", ctypes.c_ubyte * 6),
                        ("cid", ctypes.c_ushort), ("bdaddr_type", ctypes.c_ubyte)]
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.socket(31, socket.SOCK_SEQPACKET, 0)  # AF_BLUETOOTH, BTPROTO_L2CAP
        loc = Addr(31, 0, (ctypes.c_ubyte * 6)(), 4, 1)
        rem = Addr(31, 0, (ctypes.c_ubyte * 6)(*bytes.fromhex(mac.replace(":", ""))[::-1]), 4, 1)
        if fd < 0 or libc.bind(fd, ctypes.byref(loc), ctypes.sizeof(loc)) < 0 \
                or libc.connect(fd, ctypes.byref(rem), ctypes.sizeof(rem)) < 0:
            err = ctypes.get_errno()
            if fd >= 0:
                os.close(fd)
            raise OSError(err, os.strerror(err))
        self.sock = socket.socket(fileno=fd)
        self.sock.settimeout(1)
        self.sock.send(b"\x02" + struct.pack("<H", 500))                     # MTU
        self.sock.send(b"\x12" + struct.pack("<H", H_CCCD) + b"\x01\x00")   # Notify an

    def recv(self):
        """Notify-Daten oder None (Timeout / andere ATT-Pakete)."""
        try:
            p = self.sock.recv(600)
        except socket.timeout:
            return None
        if not p:
            raise ConnectionError("Verbindung getrennt")
        if p[0] == 0x1B and struct.unpack("<H", p[1:3])[0] == H_NOTIFY:
            return p[3:]
        return None

    def write(self, frame):
        self.sock.send(b"\x12" + struct.pack("<H", H_WRITE) + frame)

    def close(self):
        self.sock.close()


class GGSLink:
    """Eine dauerhafte Verbindung je Controller (geteilt von Sensor und Steckdosen), eigener Thread,
    verbindet bei Abbruch neu."""
    _links = {}
    _lock = threading.Lock()
    transport = _RawATT       # in Tests ersetzbar
    STALE_S = 30              # so lange ohne Daten -> neu verbinden
    RETRY_S = 10

    @classmethod
    def get(cls, mac, uid="0", pid=None):
        mac = mac.upper()
        with cls._lock:
            if mac not in cls._links:
                cls._links[mac] = cls(mac, uid, pid)
            return cls._links[mac]

    def __init__(self, mac, uid="0", pid=None, start=True):
        self.mac, self.uid = mac, str(uid)
        # pid = WLAN-MAC des ESP32; die BLE-MAC ist WLAN-MAC + 2
        self.pid = pid or "%012X" % (int(mac.replace(":", ""), 16) - 2)
        self.dev_sta, self.t, self.error = None, 0.0, "verbindet …"
        self.cmds = queue.Queue()
        self.cond = threading.Condition()
        self.stopping = False
        if start:
            threading.Thread(target=self._run, daemon=True, name=f"ggs-{mac}").start()

    def _run(self):
        while not self.stopping:
            try:
                self._session()
            except Exception as e:  # Bluetooth weg, Controller außer Reichweite, App verbunden …
                self.error = f"Spider Farmer {self.mac}: {e}"
                log.warning("%s – neuer Versuch in %d s", self.error, self.RETRY_S)
            time.sleep(self.RETRY_S)

    def _session(self):
        att = self.transport(self.mac)
        try:
            rx, last_rx, last_req = Reassembler(), time.monotonic(), 0.0
            time.sleep(1.5)  # MTU/CCCD-Antworten abwarten
            while not self.stopping:
                now = time.monotonic()
                if now - last_req > 20:  # falls er nicht von selbst schickt
                    self._send(att, request("getDevSta", self.pid, self.uid))
                    last_req = now
                while not self.cmds.empty():
                    self._send(att, self.cmds.get_nowait())
                data = att.recv()
                if data:
                    last_rx = time.monotonic()
                    for m in rx.feed(data):
                        self._handle(m)
                elif time.monotonic() - last_rx > self.STALE_S:
                    raise TimeoutError(f"seit {self.STALE_S} s keine Daten")
        finally:
            att.close()

    @staticmethod
    def _send(att, req):
        for frame in build_frames(encrypt(req)):
            att.write(frame)
            time.sleep(0.1)

    def _handle(self, m):
        if m.get("method") == "getDevSta" and m.get("code") == 200:
            with self.cond:
                self.dev_sta, self.t, self.error = m, time.monotonic(), None
                self.cond.notify_all()

    def outlet(self, n):
        """Aktueller Zustand der Steckdose O<n> (True/False) oder None."""
        o = (self.dev_sta or {}).get("data", {}).get("outlet", {}).get(f"O{n}", {})
        return None if "on" not in o else bool(o["on"])

    def set_outlet(self, n, on, timeout=20):
        """Schaltet O<n> und wartet, bis der Controller den Zustand meldet.
        psmode 1: Steckdosen folgen ihrer Konfiguration -> setConfigField (manueller Modus, mOnOff).
        psmode 0: direkt schalten -> setOutlet (setConfigField wird dort zwar bestätigt, schaltet aber nicht)."""
        if self.outlet(n) == on:
            return  # steht schon richtig -> nicht in den Flash des Controllers schreiben
        key = f"O{n}"
        if (self.dev_sta or {}).get("data", {}).get("outlet", {}).get("psmode") == 0:
            self.cmds.put(request("setOutlet", self.pid, self.uid, {"num": n, "on": int(on), "mode": None}))
        else:
            self.cmds.put(request("setConfigField", self.pid, self.uid,
                                  {"keyPath": ["outlet", key], key: {"modeType": 0, "mOnOff": int(on)}}))
        end = time.monotonic() + timeout
        with self.cond:
            while self.outlet(n) != on:
                left = end - time.monotonic()
                if left <= 0:
                    raise RuntimeError(self.error or f"Spider Farmer: O{n} meldet keinen neuen Zustand")
                self.cond.wait(left)

    def close(self):
        self.stopping = True


class SpiderFarmerSensor:
    """Sensor-Treiber: Klima (temp, hum, vpd) und Boden (soil_temp, soil_hum, soil_ec, je Sonde soil<i>_…)."""

    def __init__(self, mac, uid="0", pid=None, max_age_s=60, **_):
        self.link, self.max_age = GGSLink.get(mac, uid, pid), max_age_s

    def read(self):
        link = self.link
        if not link.dev_sta:
            raise RuntimeError(link.error or "Spider Farmer: noch keine Daten")
        age = time.monotonic() - link.t
        if age > self.max_age:
            raise RuntimeError(link.error or f"Spider Farmer: letzte Daten vor {age:.0f} s")
        return readings(link.dev_sta)
