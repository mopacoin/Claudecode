"""Ausgänge mit Modus auto/on/off, Mindestschaltzeit und austauschbarem Backend."""
import time

import logging

from . import backends

log = logging.getLogger("grow.outputs")
MANUAL = "manual"  # Rolle ohne Automatik: nur Ein/Aus von Hand, wird beim Start/Beenden nicht geschaltet


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
    def __init__(self, name, backend, role=None, min_switch_s=0, max_on_s=None, safe_state=False, info=None, proportional=False):
        self.info = info or {}
        self.proportional = proportional  # Servo: Winkel statt Ein/Aus
        self.angle = self.watts = self.manual_angle = None
        self.name, self.backend, self.role = name, backend, role or name
        self.min_switch_s, self.max_on_s, self.safe_state = min_switch_s, max_on_s, safe_state
        self.mode = "auto"      # auto | on | off
        self.state = False
        self.changed = time.monotonic() - min_switch_s
        self._max_hit = False  # Laufzeitbegrenzung ausgelöst -> erst nach "aus" wieder freigeben
        self._cmd = None
        self.known = not self.manual  # manual: erst bekannt, wenn das Gerät meldet oder von Hand geschaltet wurde
        self.dimmable = hasattr(backend, "level")  # SANlight-Dimmer: Helligkeit in % statt Ein/Aus
        self.level = self._out = None
        if not proportional and not self.manual and getattr(backend, "initial", True):
            backend.set(False)

    @property
    def manual(self):
        return self.role == MANUAL  # Rolle kann zur Laufzeit wechseln (GGS-Steckdose: Modus "Growcontroller")

    def apply(self, want):
        """want = Sollzustand der Automatik für diese Rolle; Modus kann überschreiben."""
        if self.manual:
            return self._apply_manual()
        self.known = True
        if self.mode in ("on", "off"):
            want = self.mode == "on"
        now = time.monotonic()
        if not want:
            self._max_hit = False
        if self.state and self.max_on_s and now - self.changed > self.max_on_s:
            self._max_hit = True  # Laufzeitbegrenzung (z. B. Pumpe): bleibt aus, bis die Anforderung endet
        if self._max_hit:
            want = False
        if want != self.state and (now - self.changed >= self.min_switch_s or self.mode == "off"):
            self.state, self.changed = want, now
            self.backend.set(want)

    def _apply_manual(self):
        """Rolle "manual": keine Regel. Ein/Aus von Hand, "auto" (= frei) lässt das Gerät in Ruhe.
        Angezeigt wird der vom Gerät gemeldete Zustand, falls das Backend ihn kennt."""
        if self.mode in ("on", "off"):
            want = self.mode == "on"
            if want != self._cmd:
                self._cmd, self.changed = want, time.monotonic()
                self.backend.set(want)
        elif self._cmd is not None:
            self._cmd = None
            if hasattr(self.backend, "desired"):
                self.backend.set(None)  # Netzwerk-Backend: nicht mehr auffrischen
        actual = self.backend.actual() if hasattr(self.backend, "actual") else None
        self.state = bool(self._cmd) if actual is None else actual
        self.known = actual is not None or self._cmd is not None

    def apply_level(self, angle, watts, deadband=3):
        """Stufenlos: Winkel nur senden, wenn er sich um mindestens `deadband` Grad ändert."""
        angle = int(round(max(0, min(180, angle))))
        if self.angle is None or abs(angle - self.angle) >= deadband or (self.mode == "manual" and angle != self.angle):
            self.angle = angle
            self.backend.set(angle)
        self.watts = watts

    def apply_dim(self, pct):
        """Dimmbares Licht: `pct` = Helligkeit laut Zeitplan (0…100). Ein = 100 %, Aus = 0 %.
        Gesendet wird nur, wenn sich die tatsächliche Leistung ändert (unter der Mindestleistung = aus)."""
        if self.mode in ("on", "off"):
            pct = 100 if self.mode == "on" else 0
        pct = int(round(max(0, min(100, pct))))
        self.known = True
        if pct != self.level:
            out = self.backend.level(pct)
            if (out > 0) != self.state:
                self.changed = time.monotonic()
            self.level, self.state = pct, out > 0
            if out != self._out:
                self._out = out
                self.backend.set(pct)

    def shutdown(self):
        if self.manual or getattr(self.backend, "keep_on_shutdown", False):
            return  # von Hand geschaltete Geräte / Dimmer beim Beenden nicht anfassen
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
        common["proportional"] = bool(c.pop("proportional", typ == "mqtt_servo"))
        secret = ("key", "local_key", "password")
        common["info"] = {"type": typ, **{k: v for k, v in c.items() if k not in secret}, **{k: common[k] for k in ("min_switch_s", "max_on_s") if k in common}}
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
    if typ == "meross_local":
        return backends.MerossLocalBackend(**c)
    if typ == "tuya":
        return backends.TuyaBackend(**c)
    if typ == "spiderfarmer":
        return backends.SpiderFarmerBackend(**c)
    if typ == "sanlight":
        from .sanlight import SanlightLink
        return backends.SanlightBackend(SanlightLink.get(**cfg["sanlight"]), **c)
    if typ == "mqtt_servo":
        m = cfg["mqtt"]
        h = hub("mqtt", lambda: backends.MqttHub(m["host"], m.get("port", 1883), m.get("username"), m.get("password")))
        return backends.MqttServoBackend(h, **c)
    raise ValueError(f"unbekannter Typ {typ}")
