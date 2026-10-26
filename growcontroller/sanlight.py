#!/usr/bin/env python3
"""SANlight-LED-Dimmer (SLD) lokal per Bluetooth Mesh – ohne App.

Protokoll aus der SANlight-Mesh-App 3.4.0 rekonstruiert, an SLD V5 (Firmware 5) geprüft:
  Standard-Bluetooth-Mesh über GATT-Proxy (Dienst 0x1828), Keys aus dem Mesh-Export der App.
  Vendor-Modell: Company 0x0A8B, Model 0x0001, Opcode = [0xC0|op, 0x8B, 0x0A], Werte little endian.
    6 SET_MAX_BRIGHTNESS u8 %          -> 7  Status: u32 Uptime ms, u16 Helligkeit ‰
   12 GET_UPTIME_AND_BRIGHTNESS        -> 13 dito
   14 GET_COMBINED_DAYLIGHT_DATA       -> 15 Uptime/Helligkeit, u8 Max %, Tageslichtzyklus
    1 SET_DAYLIGHT_CONFIG: u32 id, u8 n, n x (u16 Minute, u8 %), Name\\0   (Zeit = seit Einschalten!)
   10 SET_UPTIME u32 ms (Dimmer-Uhr), 21 GET_FIRMWARE_VERSION -> u16

Implementiert die benoetigten Teile des Bluetooth-Mesh-Profils:
  - Proxy-Protokoll inkl. SAR (MTU 23 tauglich)
  - Network-Layer (Verschluesselung, Obfuskierung, IV-Index aus Secure Network Beacon)
  - Lower Transport inkl. Segmentierung, Reassembly und Segment-Acks
  - Proxy-Filter, damit Status-Antworten bei uns ankommen
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.cmac import CMAC

log = logging.getLogger("grow.sanlight")

PROXY_SERVICE = "00001828-0000-1000-8000-00805f9b34fb"
PROXY_DATA_IN = "00002add-0000-1000-8000-00805f9b34fb"
PROXY_DATA_OUT = "00002ade-0000-1000-8000-00805f9b34fb"

SANLIGHT_CID = 0x0A8B
SANLIGHT_PRODUCT_ID = 3027

# SANlight Vendor-Opcodes (SanlightMessageOpCodes)
OP_SET_DAYLIGHT_CONFIG = 1
OP_SET_DAYLIGHT_CONFIG_STATUS = 2
OP_GET_DAYLIGHT_CONFIG = 3
OP_GET_DAYLIGHT_CONFIG_STATUS = 4
OP_SET_MAX_BRIGHTNESS = 6
OP_SET_MAX_BRIGHTNESS_STATUS = 7
OP_GET_MAX_BRIGHTNESS = 8
OP_GET_MAX_BRIGHTNESS_STATUS = 9
OP_SET_UPTIME = 10
OP_SET_UPTIME_STATUS = 11
OP_GET_UPTIME_AND_BRIGHTNESS = 12
OP_GET_UPTIME_AND_BRIGHTNESS_STATUS = 13
OP_GET_COMBINED_DAYLIGHT_DATA = 14
OP_GET_COMBINED_DAYLIGHT_DATA_STATUS = 15
OP_SET_NODE_NAME = 17
OP_SET_NODE_NAME_STATUS = 18
OP_GET_NODE_NAME = 19
OP_GET_NODE_NAME_STATUS = 20
OP_GET_FIRMWARE_VERSION = 21
OP_GET_FIRMWARE_VERSION_STATUS = 22

# SIG Generic Models
OP_GENERIC_ONOFF_GET = 0x8201
OP_GENERIC_ONOFF_SET = 0x8202
OP_GENERIC_ONOFF_STATUS = 0x8204
OP_GENERIC_LEVEL_GET = 0x8205
OP_GENERIC_LEVEL_SET = 0x8206
OP_GENERIC_LEVEL_STATUS = 0x8208


def vendor_opcode(op: int) -> int:
    """3-Byte-Vendor-Opcode als Integer (Byte-Reihenfolge wie auf der Luft)."""
    return ((0xC0 | op) << 16) | ((SANLIGHT_CID & 0xFF) << 8) | (SANLIGHT_CID >> 8)


# ═══════════════════════════════════════════════════════════════
#  Krypto (Mesh Profile 3.8)
# ═══════════════════════════════════════════════════════════════

def aes_ecb(key: bytes, data: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(data) + enc.finalize()


def aes_cmac(key: bytes, data: bytes) -> bytes:
    c = CMAC(algorithms.AES(key))
    c.update(data)
    return c.finalize()


def s1(m: bytes) -> bytes:
    return aes_cmac(b"\x00" * 16, m)


def k2(n: bytes, p: bytes = b"\x00"):
    t = aes_cmac(s1(b"smk2"), n)
    t1 = aes_cmac(t, p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    return t1[15] & 0x7F, t2, t3


def k3(n: bytes) -> bytes:
    t = aes_cmac(s1(b"smk3"), n)
    return aes_cmac(t, b"id64\x01")[-8:]


def k4(n: bytes) -> int:
    t = aes_cmac(s1(b"smk4"), n)
    return aes_cmac(t, b"id6\x01")[-1] & 0x3F


def xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def is_unicast(addr: int) -> bool:
    return 0x0001 <= addr <= 0x7FFF


# ═══════════════════════════════════════════════════════════════
#  Sequenznummer (muss ueber Neustarts hinweg steigen!)
# ═══════════════════════════════════════════════════════════════

class SeqStore:
    def __init__(self, path: str, minimum: int = 1):
        self.path = path
        self.seq = minimum
        try:
            with open(path) as f:
                self.seq = max(minimum, int(json.load(f)["seq"]))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("Seq-Datei unlesbar (%s), starte bei %d", e, self.seq)

    def next(self) -> int:
        s = self.seq
        if s >= 0xFFFFFF:
            raise RuntimeError("Sequenznummern erschoepft - neue SRC-Adresse noetig")
        self.seq += 1
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump({"seq": self.seq}, f)
        os.replace(tmp, self.path)
        return s


# ═══════════════════════════════════════════════════════════════
#  Datenstrukturen der SANlight-Nachrichten
# ═══════════════════════════════════════════════════════════════

@dataclass
class DaylightConfig:
    id: int = 1
    name: str = "Default"
    points: list = field(default_factory=lambda: [(0, 100), (1440, 100)])  # (Minute, Prozent)

    def to_bytes(self) -> bytes:
        b = struct.pack("<IB", self.id, len(self.points))
        for minute, pct in self.points:
            b += struct.pack("<HB", int(minute), int(pct))
        return b + self.name.encode("ascii", "replace") + b"\x00"

    @classmethod
    def from_bytes(cls, b: bytes) -> "DaylightConfig":
        cid, n = struct.unpack_from("<IB", b, 0)
        pts, o = [], 5
        for _ in range(n):
            m, p = struct.unpack_from("<HB", b, o)
            pts.append((m, p))
            o += 3
        name = b[o:].split(b"\x00", 1)[0].decode("ascii", "replace")
        return cls(cid, name, pts)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "points": [list(p) for p in self.points]}


@dataclass
class UptimeAndBrightness:
    uptime_ms: int
    brightness_permille: int

    @classmethod
    def from_bytes(cls, b: bytes):
        return cls(*struct.unpack_from("<IH", b, 0))

    def to_dict(self):
        s = self.uptime_ms // 1000
        return {"uptime_ms": self.uptime_ms,
                "uptime": f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}",
                "brightness_permille": self.brightness_permille,
                "brightness": round(self.brightness_permille / 10, 1)}


def parse_manufacturer_data(b: bytes) -> Optional[dict]:
    """SanlightManufacturerDataV1 (Company 0x0A8B im Advertising)."""
    if not b or len(b) < 15:
        return None
    ver, pid, fw, addr = struct.unpack_from("<BHHH", b, 0)
    if pid != SANLIGHT_PRODUCT_ID:
        return None
    return {"version": ver, "product_id": pid, "firmware": fw,
            "node_address": addr, "network_id": b[7:15].hex()}


# ═══════════════════════════════════════════════════════════════
#  Mesh-Client
# ═══════════════════════════════════════════════════════════════

class MeshError(Exception):
    pass


class SanlightMesh:
    def __init__(self, net_key: bytes, app_key: bytes, src: int, seq_file: str,
                 ttl: int = 5, iv_index: int = 0):
        self.net_key, self.app_key, self.src, self.ttl = net_key, app_key, src, ttl
        self.nid, self.enc_key, self.priv_key = k2(net_key)
        self.network_id = k3(net_key)
        self.aid = k4(app_key)
        self.iv_index = iv_index
        self.iv_update = False
        self.seq = SeqStore(seq_file)
        self.client = None
        self.address: Optional[str] = None
        self.mtu = 23
        self._sar_buf = b""
        self._rx_segments = {}           # (src, seq_zero) -> dict
        self._ack_waiters = {}           # (dst, seq_zero) -> Future[int block_ack]
        self._resp_waiters = []          # [(src, opcode, Future)]
        self._beacon_event = asyncio.Event()
        self._tx_lock = asyncio.Lock()
        self.on_message = None           # Callback(src, dst, opcode, params)
        self.on_disconnect = None

    # ── IV-Index ──────────────────────────────────────────────
    @property
    def tx_iv(self) -> int:
        return self.iv_index - 1 if self.iv_update and self.iv_index > 0 else self.iv_index

    def rx_iv(self, ivi: int) -> int:
        return self.iv_index if (self.iv_index & 1) == ivi else max(0, self.iv_index - 1)

    # ── Network-Layer ─────────────────────────────────────────
    def _net_nonce(self, ctl, ttl, seq, src, iv, proxy=False) -> bytes:
        head = bytes([0x03, 0x00]) if proxy else bytes([0x00, (ctl << 7) | ttl])
        return head + seq.to_bytes(3, "big") + src.to_bytes(2, "big") + b"\x00\x00" + iv.to_bytes(4, "big")

    def encode_network(self, ctl: int, ttl: int, seq: int, dst: int, transport: bytes,
                       proxy: bool = False) -> bytes:
        iv = self.tx_iv
        nonce = self._net_nonce(ctl, ttl, seq, self.src, iv, proxy)
        enc = AESCCM(self.enc_key, tag_length=8 if ctl else 4).encrypt(
            nonce, dst.to_bytes(2, "big") + transport, None)
        pecb = aes_ecb(self.priv_key, b"\x00" * 5 + iv.to_bytes(4, "big") + enc[:7])[:6]
        hdr = bytes([(ctl << 7) | ttl]) + seq.to_bytes(3, "big") + self.src.to_bytes(2, "big")
        return bytes([((iv & 1) << 7) | self.nid]) + xor(hdr, pecb) + enc

    def decode_network(self, pdu: bytes, proxy: bool = False) -> Optional[dict]:
        if len(pdu) < 14 or (pdu[0] & 0x7F) != self.nid:
            return None
        iv = self.rx_iv(pdu[0] >> 7)
        pecb = aes_ecb(self.priv_key, b"\x00" * 5 + iv.to_bytes(4, "big") + pdu[7:14])[:6]
        hdr = xor(pdu[1:7], pecb)
        ctl, ttl = hdr[0] >> 7, hdr[0] & 0x7F
        seq = int.from_bytes(hdr[1:4], "big")
        src = int.from_bytes(hdr[4:6], "big")
        try:
            plain = AESCCM(self.enc_key, tag_length=8 if ctl else 4).decrypt(
                self._net_nonce(ctl, ttl, seq, src, iv, proxy), pdu[7:], None)
        except Exception:
            return None
        return {"ctl": ctl, "ttl": ttl, "seq": seq, "src": src, "iv": iv,
                "dst": int.from_bytes(plain[:2], "big"), "transport": plain[2:]}

    # ── Upper/Lower Transport ─────────────────────────────────
    def _app_nonce(self, aszmic, seq_auth, src, dst, iv) -> bytes:
        return (bytes([0x01, aszmic << 7]) + seq_auth.to_bytes(3, "big") + src.to_bytes(2, "big")
                + dst.to_bytes(2, "big") + iv.to_bytes(4, "big"))

    async def send_access(self, dst: int, access: bytes):
        """Access-PDU mit dem AppKey verschluesselt senden (bei Bedarf segmentiert)."""
        async with self._tx_lock:
            seq_auth = self.seq.next()
            upper = AESCCM(self.app_key, tag_length=4).encrypt(
                self._app_nonce(0, seq_auth, self.src, dst, self.tx_iv), access, None)
            if len(upper) <= 15:
                await self._send_net(seq_auth, dst, bytes([0x40 | self.aid]) + upper)
                return
            await self._send_segmented(seq_auth, dst, upper)

    async def _send_segmented(self, seq_auth: int, dst: int, upper: bytes):
        segs = [upper[i:i + 12] for i in range(0, len(upper), 12)]
        seg_n, seq_zero = len(segs) - 1, seq_auth & 0x1FFF
        if seg_n > 31:
            raise MeshError("Nachricht zu lang")

        def seg_pdu(o):
            h = (seq_zero << 10) | (o << 5) | seg_n  # SZMIC=0
            return bytes([0xC0 | self.aid]) + h.to_bytes(3, "big") + segs[o]

        missing = set(range(len(segs)))
        first = True
        for attempt in range(4):
            fut = None
            if is_unicast(dst):
                fut = asyncio.get_running_loop().create_future()
                self._ack_waiters[(dst, seq_zero)] = fut
            for o in sorted(missing):
                seq = seq_auth if (first and o == 0) else self.seq.next()
                await self._send_net(seq, dst, seg_pdu(o))
            first = False
            if fut is None:
                if attempt >= 1:   # Gruppenadresse: ohne Ack, zweimal senden
                    return
                continue
            try:
                block = await asyncio.wait_for(fut, 2.0)
            except asyncio.TimeoutError:
                continue
            finally:
                self._ack_waiters.pop((dst, seq_zero), None)
            missing = {o for o in range(len(segs)) if not (block >> o) & 1}
            if not missing:
                return
        if is_unicast(dst):
            raise MeshError(f"Segmentierte Nachricht an 0x{dst:04X} nicht bestaetigt")

    async def _send_net(self, seq: int, dst: int, lower: bytes, ctl: int = 0):
        await self._proxy_send(0x00, self.encode_network(ctl, self.ttl, seq, dst, lower))

    async def _send_segment_ack(self, dst: int, seq_zero: int, block: int):
        pdu = bytes([0x00]) + ((seq_zero << 2) & 0x7FFC).to_bytes(2, "big") + block.to_bytes(4, "big")
        await self._send_net(self.seq.next(), dst, pdu, ctl=1)

    # ── Proxy-Protokoll mit SAR ───────────────────────────────
    async def _proxy_send(self, typ: int, data: bytes):
        if not self.client or not self.client.is_connected:
            raise MeshError("Nicht verbunden")
        chunk = self.mtu - 4   # 3 Byte ATT-Header + 1 Byte Proxy-Header
        if len(data) <= chunk:
            parts = [bytes([typ]) + data]
        else:
            pieces = [data[i:i + chunk] for i in range(0, len(data), chunk)]
            parts = []
            for i, p in enumerate(pieces):
                sar = 1 if i == 0 else (3 if i == len(pieces) - 1 else 2)
                parts.append(bytes([(sar << 6) | typ]) + p)
        for p in parts:
            await self.client.write_gatt_char(PROXY_DATA_IN, p, response=False)
            await asyncio.sleep(0.02)

    def _on_notify(self, _sender, data: bytearray):
        data = bytes(data)
        sar, typ = data[0] >> 6, data[0] & 0x3F
        if sar in (0, 1):
            self._sar_buf = data[1:]
        else:
            self._sar_buf += data[1:]
        if sar in (0, 3):
            payload, self._sar_buf = self._sar_buf, b""
            try:
                self._handle_proxy_pdu(typ, payload)
            except Exception:
                log.exception("Fehler beim Verarbeiten eines Proxy-PDU")

    def _handle_proxy_pdu(self, typ: int, p: bytes):
        if typ == 0x01 and p[:1] == b"\x01" and len(p) >= 14:     # Secure Network Beacon
            flags, nid, iv = p[1], p[2:10], int.from_bytes(p[10:14], "big")
            if nid == self.network_id:
                if iv != self.iv_index or bool(flags & 2) != self.iv_update:
                    log.info("IV-Index aus Beacon: %d (IV-Update: %s)", iv, bool(flags & 2))
                self.iv_index, self.iv_update = iv, bool(flags & 2)
                self._beacon_event.set()
        elif typ == 0x00:
            net = self.decode_network(p)
            if net:
                self._handle_lower(net)
        elif typ == 0x02:
            net = self.decode_network(p, proxy=True)
            if net:
                log.debug("Proxy-Config-Antwort: %s", net["transport"].hex())

    def _handle_lower(self, net: dict):
        t, src, dst = net["transport"], net["src"], net["dst"]
        if net["ctl"]:
            if t and t[0] & 0x7F == 0x00 and len(t) >= 7:          # Segment Acknowledgment
                seq_zero = (int.from_bytes(t[1:3], "big") >> 2) & 0x1FFF
                fut = self._ack_waiters.get((src, seq_zero))
                if fut and not fut.done():
                    fut.set_result(int.from_bytes(t[3:7], "big"))
            return
        if not t[0] & 0x80:                                         # unsegmentiert
            self._handle_upper(net, t[0], net["seq"], 0, t[1:])
            return
        h = int.from_bytes(t[1:4], "big")
        szmic, seq_zero, seg_o, seg_n = h >> 23, (h >> 10) & 0x1FFF, (h >> 5) & 0x1F, h & 0x1F
        seq_auth = net["seq"] - ((net["seq"] - seq_zero) & 0x1FFF)
        key = (src, seq_zero)
        st = self._rx_segments.get(key)
        if not st or st["seq_auth"] != seq_auth:
            st = self._rx_segments[key] = {"seq_auth": seq_auth, "n": seg_n, "segs": {},
                                           "done": False, "t": time.monotonic()}
        st["segs"][seg_o] = t[4:]
        block = sum(1 << o for o in st["segs"])
        complete = len(st["segs"]) == seg_n + 1
        if dst == self.src and (complete or seg_o == seg_n):
            asyncio.ensure_future(self._send_segment_ack(src, seq_zero, block))
        if complete and not st["done"]:
            st["done"] = True
            upper = b"".join(st["segs"][i] for i in range(seg_n + 1))
            self._handle_upper(net, t[0], seq_auth, szmic, upper)
        for k in [k for k, v in self._rx_segments.items() if time.monotonic() - v["t"] > 30]:
            del self._rx_segments[k]

    def _handle_upper(self, net: dict, hdr: int, seq_auth: int, szmic: int, upper: bytes):
        if not hdr & 0x40:
            return   # DevKey-Nachricht (Konfiguration) - nicht benoetigt
        if hdr & 0x3F != self.aid:
            log.debug("Nachricht mit anderem AppKey (AID %#x) von 0x%04X", hdr & 0x3F, net["src"])
            return
        try:
            access = AESCCM(self.app_key, tag_length=8 if szmic else 4).decrypt(
                self._app_nonce(szmic, seq_auth, net["src"], net["dst"], net["iv"]), upper, None)
        except Exception:
            log.debug("Entschluesselung fehlgeschlagen von 0x%04X", net["src"])
            return
        if access[0] & 0xC0 == 0xC0:
            opcode, params = int.from_bytes(access[:3], "big"), access[3:]
        elif access[0] & 0x80:
            opcode, params = int.from_bytes(access[:2], "big"), access[2:]
        else:
            opcode, params = access[0], access[1:]
        src = net["src"]
        log.debug("RX 0x%04X -> 0x%04X op=%06X %s", src, net["dst"], opcode, params.hex())
        for w in list(self._resp_waiters):
            if w[0] in (src, None) and w[1] == opcode and not w[2].done():
                w[2].set_result((src, params))
        if self.on_message:
            self.on_message(src, net["dst"], opcode, params)

    # ── Verbindung ────────────────────────────────────────────
    async def scan(self, timeout: float = 10.0) -> list:
        """Proxy-Knoten dieses Netzes finden (per Network-ID im Advertising)."""
        found = {}

        def cb(dev, adv):
            sd = adv.service_data.get(PROXY_SERVICE)
            mfr = parse_manufacturer_data(adv.manufacturer_data.get(SANLIGHT_CID))
            ours = (sd and sd[0] == 0 and sd[1:9] == self.network_id) or \
                   (mfr and mfr["network_id"] == self.network_id.hex())
            if ours:
                found[dev.address] = {"address": dev.address, "name": dev.name,
                                      "rssi": adv.rssi, "info": mfr}

        from bleak import BleakScanner
        async with BleakScanner(detection_callback=cb):
            await asyncio.sleep(timeout)
        return sorted(found.values(), key=lambda d: -d["rssi"])

    async def connect(self, address: Optional[str] = None, scan_timeout: float = 10.0):
        if not address:
            nodes = await self.scan(scan_timeout)
            if not nodes:
                raise MeshError("Kein SANlight-Proxy dieses Netzes gefunden")
            address = nodes[0]["address"]
            log.info("Verbinde mit %s (%s, RSSI %s)", address, nodes[0]["name"], nodes[0]["rssi"])
        self._beacon_event.clear()
        from bleak import BleakClient
        self.client = BleakClient(address, disconnected_callback=self._disconnected)
        await self.client.connect(timeout=20)
        self.address = address
        try:
            await self.client._backend._acquire_mtu()   # BlueZ: echte MTU abfragen
        except Exception:
            pass
        self.mtu = max(23, self.client.mtu_size or 23)
        await self.client.start_notify(PROXY_DATA_OUT, self._on_notify)
        try:
            await asyncio.wait_for(self._beacon_event.wait(), 5)
        except asyncio.TimeoutError:
            log.warning("Kein Secure Network Beacon empfangen - nutze IV-Index %d", self.iv_index)
        await self.add_to_proxy_filter([self.src])
        log.info("Verbunden: %s, MTU %d, IV-Index %d, SRC 0x%04X",
                 address, self.mtu, self.iv_index, self.src)

    def _disconnected(self, _client):
        log.warning("BLE-Verbindung getrennt")
        if self.on_disconnect:
            self.on_disconnect()

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def add_to_proxy_filter(self, addresses):
        """Proxy-Konfig 'Add Addresses to Filter' (Accept-List), sonst kommen keine Antworten."""
        payload = bytes([0x01]) + b"".join(a.to_bytes(2, "big") for a in addresses)
        async with self._tx_lock:
            pdu = self.encode_network(1, 0, self.seq.next(), 0x0000, payload, proxy=True)
            await self._proxy_send(0x02, pdu)

    # ── Request/Response ──────────────────────────────────────
    async def request(self, dst: int, access: bytes, resp_opcode: Optional[int],
                      timeout: float = 4.0, retries: int = 2):
        """Sendet und wartet (bei Unicast) auf die Statusantwort. Gibt params zurueck."""
        if resp_opcode is None or not is_unicast(dst):
            await self.send_access(dst, access)
            return None
        for attempt in range(retries + 1):
            fut = asyncio.get_running_loop().create_future()
            w = (dst, resp_opcode, fut)
            self._resp_waiters.append(w)
            try:
                await self.send_access(dst, access)
                _, params = await asyncio.wait_for(fut, timeout)
                return params
            except asyncio.TimeoutError:
                log.debug("Timeout 0x%04X op %06X (Versuch %d)", dst, resp_opcode, attempt + 1)
            finally:
                self._resp_waiters.remove(w)
        raise MeshError(f"Keine Antwort von 0x{dst:04X}")

    async def vendor(self, dst: int, op: int, params: bytes = b"", resp_op: Optional[int] = None, **kw):
        access = vendor_opcode(op).to_bytes(3, "big") + params
        return await self.request(dst, access, vendor_opcode(resp_op) if resp_op else None, **kw)

    # ── SANlight-Befehle ──────────────────────────────────────
    async def get_uptime_and_brightness(self, dst: int) -> UptimeAndBrightness:
        p = await self.vendor(dst, OP_GET_UPTIME_AND_BRIGHTNESS, resp_op=OP_GET_UPTIME_AND_BRIGHTNESS_STATUS)
        return UptimeAndBrightness.from_bytes(p)

    async def set_max_brightness(self, dst: int, percent: int):
        percent = max(0, min(100, int(percent)))
        p = await self.vendor(dst, OP_SET_MAX_BRIGHTNESS, bytes([percent]),
                              resp_op=OP_SET_MAX_BRIGHTNESS_STATUS)
        return UptimeAndBrightness.from_bytes(p) if p else None

    async def get_max_brightness(self, dst: int) -> int:
        p = await self.vendor(dst, OP_GET_MAX_BRIGHTNESS, resp_op=OP_GET_MAX_BRIGHTNESS_STATUS)
        return p[0]

    async def set_uptime(self, dst: int, uptime_ms: int):
        p = await self.vendor(dst, OP_SET_UPTIME, struct.pack("<I", int(uptime_ms)),
                              resp_op=OP_SET_UPTIME_STATUS)
        return UptimeAndBrightness.from_bytes(p) if p else None

    async def sync_time_of_day(self, dst: int):
        """Dimmer-Uhr (Zeit seit Einschalten) auf die lokale Uhrzeit setzen."""
        lt = time.localtime()
        ms = ((lt.tm_hour * 60 + lt.tm_min) * 60 + lt.tm_sec) * 1000
        return await self.set_uptime(dst, ms)

    async def get_daylight_config(self, dst: int) -> DaylightConfig:
        p = await self.vendor(dst, OP_GET_DAYLIGHT_CONFIG, resp_op=OP_GET_DAYLIGHT_CONFIG_STATUS)
        return DaylightConfig.from_bytes(p)

    async def set_daylight_config(self, dst: int, cfg: DaylightConfig):
        p = await self.vendor(dst, OP_SET_DAYLIGHT_CONFIG, cfg.to_bytes(),
                              resp_op=OP_SET_DAYLIGHT_CONFIG_STATUS, timeout=6.0)
        return UptimeAndBrightness.from_bytes(p) if p else None

    async def get_combined(self, dst: int) -> dict:
        p = await self.vendor(dst, OP_GET_COMBINED_DAYLIGHT_DATA,
                              resp_op=OP_GET_COMBINED_DAYLIGHT_DATA_STATUS, timeout=6.0)
        return {**UptimeAndBrightness.from_bytes(p[:6]).to_dict(),
                "max_brightness": p[6],
                "daylight": DaylightConfig.from_bytes(p[7:]).to_dict()}

    async def get_firmware_version(self, dst: int) -> int:
        p = await self.vendor(dst, OP_GET_FIRMWARE_VERSION, resp_op=OP_GET_FIRMWARE_VERSION_STATUS)
        return struct.unpack_from("<H", p)[0]

    async def get_node_name(self, dst: int) -> str:
        p = await self.vendor(dst, OP_GET_NODE_NAME, resp_op=OP_GET_NODE_NAME_STATUS)
        return p.split(b"\x00", 1)[0].decode("ascii", "replace")

    async def set_node_name(self, dst: int, name: str):
        await self.vendor(dst, OP_SET_NODE_NAME, name.encode("ascii", "replace") + b"\x00",
                          resp_op=OP_SET_NODE_NAME_STATUS)

    # ── SIG Generic Models ────────────────────────────────────
    _tid = 0

    def _next_tid(self) -> int:
        SanlightMesh._tid = (SanlightMesh._tid + 1) & 0xFF
        return SanlightMesh._tid

    async def generic_level_set(self, dst: int, level: int):
        access = OP_GENERIC_LEVEL_SET.to_bytes(2, "big") + struct.pack("<hB", level, self._next_tid())
        p = await self.request(dst, access, OP_GENERIC_LEVEL_STATUS)
        return struct.unpack_from("<h", p)[0] if p else None

    async def generic_level_get(self, dst: int) -> int:
        p = await self.request(dst, OP_GENERIC_LEVEL_GET.to_bytes(2, "big"), OP_GENERIC_LEVEL_STATUS)
        return struct.unpack_from("<h", p)[0]

    async def generic_onoff_set(self, dst: int, on: bool):
        access = OP_GENERIC_ONOFF_SET.to_bytes(2, "big") + bytes([1 if on else 0, self._next_tid()])
        p = await self.request(dst, access, OP_GENERIC_ONOFF_STATUS)
        return bool(p[0]) if p else None

    async def generic_onoff_get(self, dst: int) -> bool:
        p = await self.request(dst, OP_GENERIC_ONOFF_GET.to_bytes(2, "big"), OP_GENERIC_ONOFF_STATUS)
        return bool(p[0])


# ═══════════════════════════════════════════════════════════════
#  Dauerhafte Verbindung für den Growcontroller
# ═══════════════════════════════════════════════════════════════

class Unreachable(Exception):
    """Kein Dimmer erreichbar (z. B. Lampen stromlos, weil das Licht per Relais aus ist)."""


CONNECT_TIMEOUT_S = 35   # Scan (8 s) + Verbinden (20 s) + Beacon (5 s) + Reserve


class SanlightLink:
    """Eine Mesh-Verbindung je Netz (geteilt von allen SANlight-Ausgängen), eigener Thread mit asyncio.
    Verbindet bei Bedarf, hält die Verbindung und liest regelmäßig Uptime/Helligkeit jedes Dimmers."""
    _links = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, net_key, app_key, src_address=0x0011, seq_file="data/sanlight_seq.json", proxy=None, **_):
        key = (net_key.lower(), int(str(src_address), 0) if isinstance(src_address, str) else int(src_address))
        with cls._lock:
            if key not in cls._links:
                cls._links[key] = cls(net_key, app_key, key[1], seq_file, proxy)
            return cls._links[key]

    def __init__(self, net_key, app_key, src, seq_file, proxy=None, start=True):
        self.mesh = SanlightMesh(bytes.fromhex(net_key), bytes.fromhex(app_key), src, seq_file)
        self.proxy = proxy
        self.addresses = set()            # bekannte Dimmer (für die Statusabfrage)
        self.status = {}                  # Adresse -> {"brightness", "uptime", "max", "t"}
        self.connected = False
        self.loop = asyncio.new_event_loop()
        self._conn_lock = None
        self._next_try = 0.0
        if start:
            threading.Thread(target=self._run, daemon=True, name="sanlight").start()

    def register(self, address):
        self.addresses.add(address)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self._conn_lock = asyncio.Lock()
        self.loop.create_task(self._poller())
        self.loop.run_forever()

    async def _ensure(self):
        async with self._conn_lock:
            if self.mesh.connected:
                return
            if time.monotonic() < self._next_try:
                raise Unreachable("SANlight: kein Dimmer erreichbar")
            try:
                # harte Zeitgrenze: ein hängender BlueZ-Scan/Connect hielt sonst die Sperre für immer
                # und blockierte alle Befehle an die Lampen (26.10.: ab Neustart 01:09 nichts mehr gesendet)
                await asyncio.wait_for(self.mesh.connect(self.proxy, scan_timeout=8), CONNECT_TIMEOUT_S)
                self.connected = True
            except Exception as e:
                self.connected = False
                self._next_try = time.monotonic() + 30   # nicht dauernd scannen (Govee teilt den Adapter)
                try:
                    await asyncio.wait_for(self.mesh.disconnect(), 10)
                except Exception:
                    pass
                why = "Verbindungsaufbau hängt (Zeitüberschreitung)" if isinstance(e, asyncio.TimeoutError) else (str(e) or type(e).__name__)
                raise Unreachable(f"SANlight: {why}") from e

    async def _poller(self, every=60):
        while True:
            try:
                await self._ensure()
                for a in sorted(self.addresses):
                    try:
                        s = await self.mesh.get_combined(a)
                        self.status[a] = {"brightness": s["brightness"], "uptime": s["uptime"],
                                          "max": s["max_brightness"], "cycle": s["daylight"]["name"], "t": time.time()}
                    except MeshError as e:
                        log.debug("SANlight 0x%04X: %s", a, e)
            except Unreachable:
                pass
            except Exception:
                log.exception("SANlight-Statusabfrage")
            self.connected = self.mesh.connected
            await asyncio.sleep(every)

    def _call(self, coro_fn, timeout=40):
        async def go():
            await self._ensure()
            return await coro_fn()
        fut = asyncio.run_coroutine_threadsafe(go(), self.loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise Unreachable("SANlight: keine Antwort (Zeitüberschreitung)") from None

    def set_level(self, address, percent):
        """Max-Helligkeit setzen (wie der Schieberegler der App). Wirft Unreachable/MeshError."""
        r = self._call(lambda: self.mesh.set_max_brightness(address, percent))
        st = self.status.setdefault(address, {})
        st.update({"max": int(percent), "t": time.time()})
        if r:
            st.update({"brightness": round(r.brightness_permille / 10, 1), "uptime": r.to_dict()["uptime"]})

    def sync_time(self, address):
        self._call(lambda: self.mesh.sync_time_of_day(address))

    def info(self, address):
        s = self.status.get(address)
        return dict(s) if s and time.time() - s.get("t", 0) < 300 else None

    def close(self):
        async def c():
            await self.mesh.disconnect()
        try:
            asyncio.run_coroutine_threadsafe(c(), self.loop).result(10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
