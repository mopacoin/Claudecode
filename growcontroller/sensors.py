"""Sensor-Treiber. Jeder Treiber liefert dict mit temp (°C) und/oder hum (%)."""
import glob
import math
import time


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


DRIVERS = {"sim": SimSensor, "dht22": DHT22, "bme280": BME280, "ds18b20": DS18B20}


def create(cfg):
    return DRIVERS[cfg["driver"]](**{k: v for k, v in cfg.items() if k != "driver"})
