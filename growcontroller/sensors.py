"""Sensor-Treiber. Jeder Treiber liefert dict mit temp (°C) und/oder hum (%)."""
import glob
import logging
import math
import struct
import threading
import time

log = logging.getLogger("grow.sensors")


class SimSensor:
    """Simulierter Sensor für Tests ohne Hardware."""

    def __init__(self, **_):
        self.t0 = time.time()

    def read(self):
        t = time.time() - self.t0
        return {"temp": 24 + 3 * math.sin(t / 300), "hum": 60 + 10 * math.sin(t / 420)}


class DHT22:
    def __init__(self, pin, **_):
        import adafruit_dht
        import board
        self.dev = adafruit_dht.DHT22(getattr(board, f"D{pin}"))

    def read(self):
        # DHT22 liefert häufig RuntimeError (Prüfsumme) -> Aufrufer wiederholt beim nächsten Zyklus
        return {"temp": self.dev.temperature, "hum": self.dev.humidity}


class BME280:
    def __init__(self, address=0x76, bus=1, **_):
        import bme280
        import smbus2
        self.bme, self.addr = bme280, address
        self.bus = smbus2.SMBus(bus)
        self.cal = bme280.load_calibration_params(self.bus, address)

    def read(self):
        s = self.bme.sample(self.bus, self.addr, self.cal)
        return {"temp": s.temperature, "hum": s.humidity}


class DS18B20:
    """1-Wire-Temperatursensor (dtoverlay=w1-gpio), nur Standardbibliothek."""

    def __init__(self, device_id=None, **_):
        pattern = f"/sys/bus/w1/devices/{device_id or '28-*'}/w1_slave"
        files = glob.glob(pattern)
        if not files:
            raise RuntimeError("kein DS18B20 gefunden")
        self.path = files[0]

    def read(self):
        with open(self.path) as f:
            lines = f.read().splitlines()
        if not lines[0].endswith("YES"):
            raise RuntimeError("DS18B20 CRC-Fehler")
        return {"temp": int(lines[1].split("t=")[1]) / 1000.0}


def _packed(b):
    """3 Byte big-endian: Temperatur*10000 + Feuchte*10, Bit 23 = negative Temperatur."""
    v = int.from_bytes(b, "big")
    neg, v = v & 0x800000, v & 0x7FFFFF
    t = (v // 1000) / 10
    return (-t if neg else t), (v % 1000) / 10


def decode_govee(mid, d):
    """Govee-BLE-Werbepaket (Herstellerdaten ohne die 2 Byte Hersteller-ID) -> {temp, hum, batt} oder None."""
    cands = []
    if mid == 0xEC88 and len(d) == 6:            # H5072, H5075, H5101, H5102, H5177 (ältere FW)
        t, h = _packed(d[1:4]); cands.append((t, h, d[4] & 0x7F))
    if mid == 0xEC88 and len(d) in (7, 9):       # H5074, H5051, H5052
        t, h, b = struct.unpack("<hHB", d[1:6]); cands.append((t / 100, h / 100, b))
    if mid == 0x0001 and len(d) >= 6:            # H5100, H5101, H5102, H5104, H5105, H5174, H5177 (neuere FW)
        t, h = _packed(d[2:5]); cands.append((t, h, d[5] & 0x7F))
    if mid == 0x8801 and len(d) >= 9:            # H5179
        t, h, b = struct.unpack("<hHB", d[4:9]); cands.append((t / 100, h / 100, b))
    for t, h, b in cands:
        if -40 <= t <= 80 and 0 <= h <= 100:
            return {"temp": round(t, 1), "hum": round(h, 1), "batt": min(b, 100)}
    return None


class _GoveeScanner:
    """Ein gemeinsamer BLE-Scanner (bleak/BlueZ) für alle Govee-Sensoren, in eigenem Thread."""
    _inst = None

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self):
        self.latest, self.raw, self.error = {}, {}, None
        threading.Thread(target=self._run, daemon=True, name="govee-ble").start()

    def _run(self):
        import asyncio
        while True:
            try:
                asyncio.run(self._scan())
            except Exception as e:  # z. B. Bluetooth aus, BlueZ neu gestartet
                self.error = f"Bluetooth: {e!r}"
                log.warning("Govee-Scanner: %r – neuer Versuch in 15 s", e)
                time.sleep(15)

    async def _scan(self):
        import asyncio
        from bleak import BleakScanner
        # DuplicateData=True: auch unveränderte Pakete melden, sonst kämen bei konstanten Werten keine Updates
        async with BleakScanner(detection_callback=self._cb, bluez={"filters": {"DuplicateData": True}}):
            self.error = None
            while True:
                await asyncio.sleep(3600)

    def _cb(self, dev, adv):
        mac = dev.address.upper()
        for mid, data in adv.manufacturer_data.items():
            r = decode_govee(mid, bytes(data))
            if r:
                self.latest[mac] = {**r, "t": time.monotonic(), "rssi": adv.rssi}
                return
            self.raw[mac] = f"0x{mid:04X}:{bytes(data).hex()}"


class Govee:
    """Govee-Thermo-Hygrometer per Bluetooth. Liefert den zuletzt empfangenen Wert (max. `max_age_s` alt)."""

    def __init__(self, mac, max_age_s=600, **_):
        self.mac, self.max_age = mac.upper(), max_age_s
        self.scan = _GoveeScanner.get()

    def read(self):
        r = self.scan.latest.get(self.mac)
        if not r:
            raw = self.scan.raw.get(self.mac)
            raise RuntimeError(self.scan.error or (f"{self.mac}: unbekanntes Paketformat {raw}" if raw else f"{self.mac}: noch keine Daten empfangen"))
        age = time.monotonic() - r["t"]
        if age > self.max_age:
            raise RuntimeError(f"{self.mac}: letzte Daten vor {age:.0f} s")
        return {"temp": r["temp"], "hum": r["hum"], "batt": r["batt"]}


class MerossPower:
    """Leistungsmessung einer Meross-Steckdose (z. B. MSS305) im LAN, nur lesend – schaltet nichts.
    Liefert w (Watt), v (Volt), a (Ampere). Abfrage in eigenem Thread alle `poll_s` Sekunden."""

    def __init__(self, ip, key, poll_s=2, max_age_s=20, **_):
        self.ip, self.key, self.poll_s, self.max_age = ip, key, max(2, poll_s), max_age_s
        self.val, self.t, self.error, self.seq = None, 0.0, None, 0
        threading.Thread(target=self._run, daemon=True, name=f"meross-power-{ip}").start()

    @staticmethod
    def parse(payload):
        e = payload.get("electricity", payload)
        return {"w": e["power"] / 1000.0, "v": e["voltage"] / 10.0, "a": e["current"] / 1000.0}  # mW, 0,1 V, mA

    def _run(self):
        from .backends import meross_request
        while True:
            try:
                val = self.parse(meross_request(self.ip, self.key, "Appliance.Control.Electricity", "GET", {}))
                self.seq += 1
                val["seq"] = self.seq  # Zähler: jede echte Abfrage nur einmal auswerten
                self.val = val
                self.t, self.error = time.monotonic(), None
            except Exception as e:
                self.error = f"Meross {self.ip}: {e}"
            time.sleep(self.poll_s)

    def read(self):
        if not self.val or time.monotonic() - self.t > self.max_age:
            raise RuntimeError(self.error or f"Meross {self.ip}: noch keine Messwerte")
        return dict(self.val)


DRIVERS = {"sim": SimSensor, "dht22": DHT22, "bme280": BME280, "ds18b20": DS18B20, "govee": Govee, "meross_power": MerossPower}


def create(cfg):
    return DRIVERS[cfg["driver"]](**{k: v for k, v in cfg.items() if k not in ("driver", "prefix")})
