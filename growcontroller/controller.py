import csv
import glob
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from . import outputs, rules, sensors, settings

log = logging.getLogger("grow")
RANGES = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}
MAX_POINTS = 600
ALARM_TEXT = {
    "temp_high": "Temperatur zu hoch", "temp_low": "Temperatur zu niedrig",
    "hum_high": "Luftfeuchte zu hoch", "hum_low": "Luftfeuchte zu niedrig",
    "sensor_stale": "Keine aktuellen Sensordaten",
}


class Controller:
    def __init__(self, cfg, state_dir="data"):
        self.cfg = cfg
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.started = time.time()
        self.saved = settings.load(self._p("settings.json"))
        for sec, d in settings.DEFAULTS.items():  # Defaults < config.json < gespeicherte Einstellungen
            cfg[sec] = {**d, **cfg.get(sec, {}), **self.saved.get(sec, {})}
        self.sensors = {n: sensors.create(c) for n, c in cfg["sensors"].items()}
        self.gpio, self.outs, self.hubs = outputs.create_all(cfg)
        self.has_dehum = any(o.role == "dehumidifier" for o in self.outs.values())
        for n, m in self.saved.get("modes", {}).items():
            if n in self.outs and m in ("auto", "on", "off"):
                self.outs[n].mode = m
        self.readings = {}
        self.last_ok = None
        self.live = deque(maxlen=720)            # letzte Stunde im Regeltakt
        self.minutes = deque(maxlen=10080)       # 7 Tage, 1 Wert/min
        self.events = deque(maxlen=500)
        self.alarms, self._pending = {}, {}
        self._prev_state = {n: False for n in self.outs}
        self._last_minute = None
        self.pump_until = 0.0
        self.last_irrigation = self._load_state().get("last_irrigation")
        self.lock = threading.RLock()
        self.stop_evt = threading.Event()
        self._load_history()
        self.event("system", "Controller gestartet")

    def _p(self, name):
        return os.path.join(self.state_dir, name)

    # --- Persistenz ---
    def _load_state(self):
        try:
            with open(self._p("state.json")) as f:
                d = json.load(f)
            if d.get("last_irrigation"):
                d["last_irrigation"] = datetime.fromisoformat(d["last_irrigation"])
            return d
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        li = self.last_irrigation.isoformat() if self.last_irrigation else None
        settings.save(self._p("state.json"), {"last_irrigation": li})

    def _load_history(self):
        cutoff = datetime.now() - timedelta(days=7)
        for path in sorted(glob.glob(self._p("log-*.csv")))[-12:]:
            try:
                with open(path, newline="") as f:
                    for r in csv.DictReader(f):
                        if datetime.fromisoformat(r["t"]) >= cutoff:
                            self.minutes.append({k: (r["t"] if k == "t" else float(v)) for k, v in r.items() if v not in ("", None)})
            except (OSError, ValueError, KeyError):
                log.warning("Verlauf %s nicht lesbar", path)

    def event(self, kind, msg):
        e = {"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, "msg": msg}
        self.events.append(e)
        try:
            with open(self._p("events.jsonl"), "a") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except OSError:
            pass

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

    def vpd(self):
        return rules.vpd(self.readings.get("temp"), self.readings.get("hum"), self.cfg["climate"]["leaf_offset"])

    # --- Regeln ---
    def effective_climate(self, day):
        """Im VPD-Modus wird das VPD-Ziel bei aktueller Temperatur in eine Feuchte-Vorgabe übersetzt."""
        cl = dict(self.cfg["climate"])
        t = self.readings.get("temp")
        if cl["control"] == "vpd" and t is not None:
            for p in ("day", "night"):
                cl[f"hum_{p}"] = rules.rh_for_vpd(t, cl[f"vpd_{p}"], cl["leaf_offset"])
            cl["hum_hyst"] = rules.rh_delta_for_vpd(t, cl["vpd_hyst"])
        return cl

    def step(self, now=None):
        now = now or datetime.now()
        with self.lock:
            self.read_sensors()
            day = rules.light_on(self.cfg["light"], now)
            cur = {}
            for o in self.outs.values():
                cur[o.role] = cur.get(o.role, False) or o.state
            want = {"light": day and self.cfg["light"]["enabled"]}
            if self.stale():
                # Failsafe: ohne Messwerte Klima-Aktoren aus, Lüfter bleibt zyklisch an
                want.update(heater=False, humidifier=False, dehumidifier=False, fan=now.minute < 10)
            else:
                want.update(rules.climate(self.effective_climate(day), day, self.readings.get("temp"),
                                          self.readings.get("hum"), cur, now, self.has_dehum))
            want["pump"] = self._pump(now)
            want["vent"] = want["intake"] = want["fan"]  # Abluft-Klappe und Zuluft folgen dem Lüfterbedarf
            for n, o in self.outs.items():
                o.apply(want.get(o.role, False))
                if o.state != self._prev_state[n]:
                    self._prev_state[n] = o.state
                    self.event("output", f"{n}: {'EIN' if o.state else 'AUS'}")
            self._check_alarms()
            self._record(now)

    def _pump(self, now):
        mono = time.monotonic()
        if mono < self.pump_until:
            return True
        if rules.irrigation_due(self.cfg["irrigation"], self.last_irrigation, now):
            self._start_pump(now)
            return True
        return False

    def _start_pump(self, now):
        self.pump_until = time.monotonic() + self.cfg["irrigation"]["duration_s"]
        self.last_irrigation = now
        self._save_state()
        self.event("irrigation", f"Bewässerung gestartet ({self.cfg['irrigation']['duration_s']} s)")

    def water_now(self):
        with self.lock:
            self._start_pump(datetime.now())

    # --- Alarme ---
    def _check_alarms(self):
        a, t, h = self.cfg["alarms"], self.readings.get("temp"), self.readings.get("hum")
        delay = a["delay_min"] * 60
        conds = {}  # id -> (aktiv, Text, Verzögerung s)
        if a["enabled"] and not self.stale():
            conds["temp_high"] = (t is not None and t > a["temp_max"], f"Temperatur {t} °C über {a['temp_max']} °C", delay)
            conds["temp_low"] = (t is not None and t < a["temp_min"], f"Temperatur {t} °C unter {a['temp_min']} °C", delay)
            conds["hum_high"] = (h is not None and h > a["hum_max"], f"Luftfeuchte {h} % über {a['hum_max']} %", delay)
            conds["hum_low"] = (h is not None and h < a["hum_min"], f"Luftfeuchte {h} % unter {a['hum_min']} %", delay)
        conds["sensor_stale"] = (self.stale(), ALARM_TEXT["sensor_stale"], 60)
        for n, o in self.outs.items():
            conds[f"offline:{n}"] = (getattr(o.backend, "healthy", True) is False, f"Gerät {n} nicht erreichbar", 60)
        mono = time.monotonic()
        for aid, (bad, msg, d) in conds.items():
            if bad:
                first = self._pending.setdefault(aid, mono)
                if mono - first >= d and aid not in self.alarms:
                    self.alarms[aid] = {"since": datetime.now().isoformat(timespec="seconds"), "msg": msg}
                    self.event("alarm", msg)
            else:
                self._pending.pop(aid, None)
                if aid in self.alarms:
                    self.event("alarm", f"Behoben: {self.alarms.pop(aid)['msg']}")
        for aid in [k for k in self.alarms if k not in conds]:
            self.alarms.pop(aid)

    # --- Verlauf ---
    def _record(self, now):
        row = {"t": now.isoformat(timespec="seconds"), **self.readings, "vpd": self.vpd(),
               **{n: int(o.state) for n, o in self.outs.items()}}
        row = {k: v for k, v in row.items() if v is not None}
        self.live.append(row)
        if self._last_minute != (now.hour, now.minute):
            self._last_minute = (now.hour, now.minute)
            self.minutes.append(row)
            self._append_csv(now, row)

    def _append_csv(self, now, row):
        fields = ["t", "temp", "hum", "vpd"] + list(self.outs)
        path = self._p(f"log-{now:%Y-%m-%d}.csv")
        try:
            if os.path.exists(path):
                with open(path) as f:
                    if f.readline().strip().split(",") != fields:
                        path = self._p(f"log-{now:%Y-%m-%d}-b.csv")  # Geräteliste hat sich geändert
            new = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(row)
        except OSError as e:
            log.warning("CSV: %s", e)

    def _rows(self, rng):
        hours = RANGES.get(rng, 1)
        if hours == 1:
            return list(self.live)
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        return [r for r in self.minutes if r["t"] >= cutoff]

    def history(self, rng="1h"):
        with self.lock:
            rows = self._rows(rng)
        size = -(-len(rows) // MAX_POINTS) or 1
        out = []
        for i in range(0, len(rows), size):
            chunk = rows[i:i + size]
            r = {"t": chunk[-1]["t"]}
            for k in {k for c in chunk for k in c if k != "t"}:
                vals = [c[k] for c in chunk if k in c]
                r[k] = round(sum(vals) / len(vals), 2) if k in ("temp", "hum", "vpd") else max(vals)
            out.append(r)
        return out

    def export_csv(self, rng):
        import io
        with self.lock:
            rows = self._rows(rng)
        cols = ["t", "temp", "hum", "vpd"] + list(self.outs)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue()

    def stats(self):
        rows = self._rows("24h")
        out = {}
        for k in ("temp", "hum", "vpd"):
            v = [r[k] for r in rows if k in r]
            if v:
                out[k] = {"min": round(min(v), 1 if k != "vpd" else 2), "max": round(max(v), 1 if k != "vpd" else 2),
                          "avg": round(sum(v) / len(v), 1 if k != "vpd" else 2)}
        return out

    # --- API ---
    def status(self):
        with self.lock:
            now = datetime.now()
            day = rules.light_on(self.cfg["light"], now)
            cl = self.effective_climate(day)
            p = "day" if day else "night"
            g = self.cfg["grow"]
            gday = None
            if g["start_date"]:
                gday = (now.date() - datetime.strptime(g["start_date"], "%Y-%m-%d").date()).days + 1
            ir = self.cfg["irrigation"]
            nxt = None
            if ir["enabled"] and self.last_irrigation:
                nxt = (self.last_irrigation + timedelta(minutes=ir["interval_min"])).isoformat(timespec="minutes")
            return {
                "time": now.isoformat(timespec="seconds"), "uptime_s": int(time.time() - self.started),
                "readings": self.readings, "vpd": self.vpd(), "stale": self.stale(), "is_day": day,
                "targets": {"temp": cl[f"temp_{p}"], "hum": cl[f"hum_{p}"], "vpd": self.cfg["climate"][f"vpd_{p}"],
                            "control": cl["control"]},
                "stats": self.stats(), "alarms": [{"id": k, **v} for k, v in self.alarms.items()],
                "grow": {"stage": g["stage"], "day": gday},
                "irrigation": {"last": self.last_irrigation and self.last_irrigation.isoformat(timespec="seconds"),
                               "next": nxt, "running": time.monotonic() < self.pump_until},
                "outputs": {n: {"state": o.state, "mode": o.mode, "role": o.role,
                                "ok": getattr(o.backend, "healthy", True), "info": o.info} for n, o in self.outs.items()},
            }

    def get_settings(self):
        with self.lock:
            return {s: dict(self.cfg[s]) for s in settings.SPEC}

    def update_settings(self, patch):
        """Gibt (settings, fehler) zurück; bei Fehlern wird nichts übernommen."""
        clean, errors = settings.validate(patch)
        if errors:
            return None, errors
        with self.lock:
            merged = settings.merge({s: self.cfg[s] for s in settings.SPEC}, clean)
            errors = settings.cross_check(merged)
            if errors:
                return None, errors
            for sec, vals in clean.items():
                self.cfg[sec].update(vals)
                self.saved.setdefault(sec, {}).update(vals)
            settings.save(self._p("settings.json"), self.saved)
            self.event("settings", "Einstellungen geändert: " + ", ".join(f"{s}.{k}={v}" for s, d in clean.items() for k, v in d.items()))
            return self.get_settings(), {}

    def set_mode(self, name, mode):
        if name not in self.outs or mode not in ("auto", "on", "off"):
            raise ValueError("ungültig")
        with self.lock:
            self.outs[name].mode = mode
            self.saved.setdefault("modes", {})[name] = mode
            settings.save(self._p("settings.json"), self.saved)
            self.event("mode", f"{name}: Modus {mode}")

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self.step()
            except Exception:
                log.exception("Regelschleife")
            self.stop_evt.wait(self.cfg["interval_s"])
        self.shutdown()

    def shutdown(self):
        self.event("system", "Controller beendet")
        for o in self.outs.values():
            o.shutdown()
        for o in self.outs.values():
            o.backend.close()
        for h in self.hubs.values():
            h.close()
        if self.gpio:
            self.gpio.cleanup()
