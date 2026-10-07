"""Ausgänge mit Modus auto/on/off, Mindestschaltzeit und austauschbarem Backend."""
import time

import logging

from . import backends

log = logging.getLogger("grow.outputs")


class DeadBackend:
    """Platzhalter, wenn ein Backend nicht aufgebaut werden konnte (Paket fehlt, Config falsch)."""
    healthy = False

    def set(self, on): pass
    def close(self): pass


class _FakeGPIO:
    BCM = OUT = HIGH = 1
    LOW = 0

    def setmode(self, *_): pass
    def setwarnings(self, *_): pass
    def setup(self, *_, **__): pass
    def output(self, *_): pass
    def cleanup(self): pass


def _gpio():
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        return GPIO
    except (ImportError, RuntimeError):
        return _FakeGPIO()


class Output:
    def __init__(self, name, backend, role=None, min_switch_s=0, max_on_s=None, safe_state=False, follow=None):
        self.name, self.backend, self.role = name, backend, role or name
        self.min_switch_s, self.max_on_s, self.safe_state = min_switch_s, max_on_s, safe_state
        self.mode = "auto"      # auto | on | off
        self.state = False
        self.changed = time.monotonic() - min_switch_s
        backend.set(False)

    def apply(self, want):
        """want = Sollzustand der Automatik für diese Rolle; Modus kann überschreiben."""
        if self.mode in ("on", "off"):
            want = self.mode == "on"
        now = time.monotonic()
        if self.state and self.max_on_s and now - self.changed > self.max_on_s:
            want = False  # Laufzeitbegrenzung (z. B. Pumpe)
        if want != self.state and (now - self.changed >= self.min_switch_s or self.mode == "off"):
            self.state, self.changed = want, now
            self.backend.set(want)

    def shutdown(self):
        self.state = self.safe_state
        self.backend.set(self.safe_state)


def create_all(cfg):
    """Gibt (gpio, outputs, hubs) zurück; hubs werden nach dem Schließen der Backends geschlossen."""
    gpio = _gpio() if any(c.get("type", "gpio") == "gpio" for c in cfg["outputs"].values()) else None
    hubs, outs = {}, {}

    def hub(key, factory):
        if key not in hubs:
            hubs[key] = factory()
        return hubs[key]

    for name, c in cfg["outputs"].items():
        c = dict(c)
        typ = c.pop("type", "gpio")
        common = {k: c.pop(k) for k in ("role", "min_switch_s", "max_on_s", "safe_state") if k in c}
        try:
            be = _make_backend(typ, c, cfg, gpio, hub)
        except Exception as e:
            log.error("Ausgang %s (%s) nicht verfügbar: %r", name, typ, e)
            be = DeadBackend()
        outs[name] = Output(name, be, **common)
    return gpio, outs, hubs


def _make_backend(typ, c, cfg, gpio, hub):
    if typ == "gpio":
        return backends.GpioBackend(gpio, **c)
    if typ == "meross":
        m = cfg["meross"]
        h = hub("meross", lambda: backends.MerossHub(m["email"], m["password"], m.get("api_base_url", "https://iotx-eu.meross.com")))
        return backends.MerossBackend(h, **c)
    if typ == "tuya":
        return backends.TuyaBackend(**c)
    if typ == "mqtt_servo":
        m = cfg["mqtt"]
        h = hub("mqtt", lambda: backends.MqttHub(m["host"], m.get("port", 1883), m.get("username"), m.get("password")))
        return backends.MqttServoBackend(h, **c)
    raise ValueError(f"unbekannter Typ {typ}")
