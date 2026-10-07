import csv
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime

from . import outputs, rules, sensors

log = logging.getLogger("grow")


class Controller:
    def __init__(self, cfg, state_dir="data"):
        self.cfg = cfg
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.sensors = {n: sensors.create(c) for n, c in cfg["sensors"].items()}
        self.gpio, self.outs, self.hubs = outputs.create_all(cfg)
        self.has_dehum = any(o.role == "dehumidifier" for o in self.outs.values())
        self.readings = {}
        self.last_ok = None
        self.history = deque(maxlen=2880)  # ~4 h bei 5 s
        self.pump_until = 0.0
        self.last_irrigation = self._load_state().get("last_irrigation")
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()

    # --- Persistenz ---
    def _state_path(self):
        return os.path.join(self.state_dir, "state.json")

    def _load_state(self):
        try:
            with open(self._state_path()) as f:
                d = json.load(f)
            if d.get("last_irrigation"):
                d["last_irrigation"] = datetime.fromisoformat(d["last_irrigation"])
            return d
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        li = self.last_irrigation.isoformat() if self.last_irrigation else None
        tmp = self._state_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last_irrigation": li}, f)
        os.replace(tmp, self._state_path())

    # --- Messen ---
    def read_sensors(self):
        merged = {}
        for name, s in self.sensors.items():
            try:
                for k, v in s.read().items():
                    if v is not None:
                        merged[k] = round(float(v), 1)
            except Exception as e:  # Sensorfehler dürfen die Regelung nicht beenden
                log.warning("Sensor %s: %s", name, e)
        if merged:
            self.readings.update(merged)
            self.last_ok = time.monotonic()

    def stale(self):
        return self.last_ok is None or time.monotonic() - self.last_ok > self.cfg.get("sensor_timeout_s", 120)

    # --- Regeln ---
    def step(self, now=None):
        now = now or datetime.now()
        with self.lock:
            self.read_sensors()
            day = rules.light_on(self.cfg["light"], now)
            cur = {}
            for o in self.outs.values():
                cur[o.role] = cur.get(o.role, False) or o.state
            want = {"light": day}
            if self.stale():
                # Failsafe: ohne Messwerte Klima-Aktoren aus, Lüfter bleibt zyklisch an
                want.update(heater=False, humidifier=False, dehumidifier=False, fan=now.minute < 10)
            else:
                want.update(rules.climate(self.cfg["climate"], day, self.readings.get("temp"),
                                          self.readings.get("hum"), cur, now, self.has_dehum))
            want["pump"] = self._pump(now)
            want["vent"] = want["intake"] = want["fan"]  # Abluft-Klappe und Zuluft folgen dem Lüfterbedarf
            for o in self.outs.values():
                o.apply(want.get(o.role, False))
            self._record(now, day)

    def _pump(self, now):
        mono = time.monotonic()
        if mono < self.pump_until:
            return True
        if rules.irrigation_due(self.cfg["irrigation"], self.last_irrigation, now):
            self.pump_until = mono + self.cfg["irrigation"]["duration_s"]
            self.last_irrigation = now
            self._save_state()
            log.info("Bewässerung gestartet")
            return True
        return False

    def _record(self, now, day):
        row = {"t": now.isoformat(timespec="seconds"), **self.readings,
               **{n: int(o.state) for n, o in self.outs.items()}}
        self.history.append(row)
        if now.second < self.cfg["interval_s"]:  # etwa 1x/min ins CSV
            path = os.path.join(self.state_dir, f"log-{now:%Y-%m-%d}.csv")
            new = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(row))
                if new:
                    w.writeheader()
                w.writerow(row)

    # --- API ---
    def status(self):
        with self.lock:
            return {"time": datetime.now().isoformat(timespec="seconds"), "readings": self.readings,
                    "stale": self.stale(), "last_irrigation": self.last_irrigation and self.last_irrigation.isoformat(timespec="seconds"),
                    "outputs": {n: {"state": o.state, "mode": o.mode, "role": o.role, "ok": getattr(o.backend, "healthy", True)} for n, o in self.outs.items()}}

    def set_mode(self, name, mode):
        if name not in self.outs or mode not in ("auto", "on", "off"):
            raise ValueError("ungültig")
        with self.lock:
            self.outs[name].mode = mode

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self.step()
            except Exception:
                log.exception("Regelschleife")
            self.stop_evt.wait(self.cfg["interval_s"])
        self.shutdown()

    def shutdown(self):
        for o in self.outs.values():
            o.shutdown()
        for o in self.outs.values():
            o.backend.close()
        for h in self.hubs.values():
            h.close()
        if self.gpio:
            self.gpio.cleanup()
