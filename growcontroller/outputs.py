"""Relais-Ausgänge mit Modus auto/on/off und Mindestschaltzeit."""
import time


class _FakeGPIO:
    BCM = OUT = HIGH = 1
    LOW = 0

    def setmode(self, *_): pass
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
    def __init__(self, gpio, name, pin, active_low=True, min_switch_s=0, max_on_s=None):
        self.gpio, self.name, self.pin = gpio, name, pin
        self.active_low, self.min_switch_s, self.max_on_s = active_low, min_switch_s, max_on_s
        self.mode = "auto"      # auto | on | off
        self.state = False
        self.changed = time.monotonic() - min_switch_s
        gpio.setup(pin, gpio.OUT, initial=self._level(False))

    def _level(self, on):
        return self.gpio.LOW if on == self.active_low else self.gpio.HIGH

    def apply(self, want):
        """want = Sollzustand der Automatik; Modus kann überschreiben."""
        if self.mode == "on":
            want = True
        elif self.mode == "off":
            want = False
        now = time.monotonic()
        if self.state and self.max_on_s and now - self.changed > self.max_on_s:
            want = False  # Laufzeitbegrenzung (z. B. Pumpe)
        if want != self.state and (now - self.changed >= self.min_switch_s or not want and self.mode == "off"):
            self.state, self.changed = want, now
            self.gpio.output(self.pin, self._level(want))

    def force_off(self):
        self.state = False
        self.gpio.output(self.pin, self._level(False))


def create_all(cfg):
    gpio = _gpio()
    outs = {n: Output(gpio, n, **c) for n, c in cfg.items()}
    return gpio, outs
