import csv
import glob
import json
import logging
import os
import threading
import time
import uuid
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
        # prefix: z. B. "room_" für einen Raum-Sensor, der nur angezeigt wird und nicht regelt
        self.sensors = {n: (c.get("prefix", ""), sensors.create(c)) for n, c in cfg["sensors"].items()}
        self._warned = {}
        self.gpio, self.outs, self.hubs = outputs.create_all(cfg)
        self.has_dehum = any(o.role == "dehumidifier" for o in self.outs.values())
        for n, m in self.saved.get("modes", {}).items():
            if n in self.outs and m in ("auto", "on", "off"):
                self.outs[n].mode = m
        self.cal = settings.validate_cal(self.saved.get("servo_cal"))[0] or settings.DEFAULT_CAL
        self.readings = {}
        self.last_ok = None
        self.live = deque(maxlen=720)            # letzte Stunde im Regeltakt
        self.minutes = deque(maxlen=10080)       # 7 Tage, 1 Wert/min
        self.events = deque(maxlen=500)
        self.alarms, self._pending = {}, {}
        self._prev_state = {n: False for n in self.outs}
        self._last_minute = None
        self._adv_block = None
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
        merged, main = {}, False
        for name, (prefix, s) in self.sensors.items():
            try:
                for k, v in s.read().items():
                    if v is not None:
                        merged[prefix + k] = round(float(v), 1)
                        main = main or (not prefix and k in ("temp", "hum"))
            except Exception as e:  # Sensorfehler dürfen die Regelung nicht beenden
                now = time.monotonic()
                if now - self._warned.get(name, -1e9) > 60:  # höchstens 1x pro Minute ins Log
                    self._warned[name] = now
                    log.warning("Sensor %s: %s", name, e)
        if merged:
            self.readings.update(merged)
        if main:  # nur der Regel-Sensor zählt für "Sensordaten aktuell"
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
            ex_w = None
            if not self.stale():
                p = "day" if day else "night"
                cl = self.effective_climate(day)
                ex_w = rules.exhaust_watts(self.cfg["exhaust"], day, self.readings.get("temp"), self.readings.get("hum"),
                                           cl[f"temp_{p}"], cl[f"hum_{p}"])
            for n, o in self.outs.items():
                if o.proportional:
                    self._apply_servo(o, want, day, ex_w)
                else:
                    o.apply(want.get(o.role, False))
                if o.state != self._prev_state[n]:
                    self._prev_state[n] = o.state
                    self.event("output", f"{n}: {'EIN' if o.state else 'AUS'}")
            self._check_alarms()
            self._record(now)
            self._advance(now)

    def _apply_servo(self, o, want, day, ex_w):
        ex = self.cfg["exhaust"]
        top = ex["max_w"] if day else ex["max_w_night"]
        if o.mode == "manual" and o.manual_angle is not None:  # Kalibrier-Test
            o.apply_level(o.manual_angle, round(rules.watts_for_angle(self.cal, o.manual_angle), 1), 0)
        else:
            if o.mode == "on":
                w = top
            elif o.mode == "off":
                w = ex["min_w"]
            elif not ex["enabled"]:  # nur Ein/Aus: folgt dem Lüfterbedarf
                w = top if want.get(o.role) else ex["min_w"]
            else:
                w = top if ex_w is None else ex_w  # ohne Messwerte: lüften
            o.apply_level(rules.angle_for_watts(self.cal, w), round(w, 1), ex["deadband_deg"])
        o.state = o.watts is not None and o.watts > ex["min_w"] + 0.5

    def set_calibration(self, points):
        pts, err = settings.validate_cal(points)
        if err:
            return None, err
        with self.lock:
            self.cal = pts
            self.saved["servo_cal"] = pts
            settings.save(self._p("settings.json"), self.saved)
            self.event("settings", "Servo-Kennlinie: " + ", ".join(f"{a}°={w:g} W" for a, w in pts))
        return pts, None

    def servo_manual(self, name, angle):
        o = self.outs.get(name)
        if not o or not o.proportional or not isinstance(angle, (int, float)) or isinstance(angle, bool) or not 0 <= angle <= 180:
            raise ValueError("ungültig")
        with self.lock:
            o.mode, o.manual_angle = "manual", int(angle)
            o.apply_level(o.manual_angle, round(rules.watts_for_angle(self.cal, o.manual_angle), 1), 0)

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
        fields = ["t", "temp", "hum", "vpd", "room_temp", "room_hum"] + list(self.outs)
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
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = [r for r in self.minutes if r["t"] >= cutoff]
        if hours == 1:  # gespeicherter Verlauf + frische Werte im Regeltakt (auch direkt nach einem Neustart)
            last = rows[-1]["t"] if rows else ""
            rows += [r for r in self.live if r["t"] > last]
        return rows

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
        cols = ["t", "temp", "hum", "vpd", "room_temp", "room_hum"] + list(self.outs)
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
                "grow": self._grow_status(g, gday),
                "irrigation": {"last": self.last_irrigation and self.last_irrigation.isoformat(timespec="seconds"),
                               "next": nxt, "running": time.monotonic() < self.pump_until},
                "outputs": {n: {"state": o.state, "mode": o.mode, "role": o.role,
                                "ok": getattr(o.backend, "healthy", True), "info": o.info,
                                "proportional": o.proportional, "angle": o.angle, "watts": o.watts} for n, o in self.outs.items()},
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
            if merged["grow"]["stage"] and merged["grow"]["stage"] not in self.presets():
                errors["grow.stage"] = "unbekannter Zyklus"
            if errors:
                return None, errors
            for sec, vals in clean.items():
                self.cfg[sec].update(vals)
                self.saved.setdefault(sec, {}).update(vals)
            settings.save(self._p("settings.json"), self.saved)
            self.event("settings", "Einstellungen geändert: " + ", ".join(f"{s}.{k}={v}" for s, d in clean.items() for k, v in d.items()))
            return self.get_settings(), {}

    # --- Zyklen (Presets) ---
    def presets(self):
        built = {k: {"days": 0, "next": "", "notes": "", **v, "builtin": True} for k, v in settings.PRESETS.items()}
        custom = {k: {"days": 0, "next": "", "notes": "", **v, "builtin": False} for k, v in self.saved.get("presets", {}).items()}
        return {**built, **custom}

    def _grow_status(self, g, gday):
        pr = self.presets()
        p = pr.get(g["stage"])
        nxt = pr.get(p["next"]) if p and p.get("next") else None
        return {"stage": g["stage"], "label": p["label"] if p else "", "color": p.get("color") if p else None, "day": gday,
                "days": p["days"] if p else 0, "next": nxt["label"] if nxt else ""}

    def save_preset(self, body):
        """Legt einen eigenen Zyklus an (ohne id) oder ändert ihn (mit id). -> (id, fehler)"""
        with self.lock:
            custom = self.saved.setdefault("presets", {})
            pid = body.get("id") if isinstance(body, dict) else None
            if pid and pid not in custom:
                return None, {"id": "Eingebaute Zyklen sind schreibgeschützt – bitte duplizieren" if pid in settings.PRESETS else "unbekannter Zyklus"}
            if not pid and len(custom) >= settings.MAX_PRESETS:
                return None, {"": f"Maximal {settings.MAX_PRESETS} eigene Zyklen"}
            clean, errors = settings.validate_preset(body, set(self.presets()), pid)
            if errors:
                return None, errors
            pid = pid or "c" + uuid.uuid4().hex[:8]
            custom[pid] = clean
            settings.save(self._p("settings.json"), self.saved)
            self.event("settings", f"Zyklus gespeichert: {clean['label']}")
            return pid, {}

    def delete_preset(self, pid):
        with self.lock:
            custom = self.saved.get("presets", {})
            if pid not in custom:
                return False
            label = custom.pop(pid)["label"]
            for p in custom.values():  # Verweise auf den gelöschten Zyklus entfernen
                if p.get("next") == pid:
                    p["next"] = ""
            if self.cfg["grow"]["stage"] == pid:
                self.cfg["grow"]["stage"] = ""
                self.saved.setdefault("grow", {})["stage"] = ""
            settings.save(self._p("settings.json"), self.saved)
            self.event("settings", f"Zyklus gelöscht: {label}")
            return True

    def apply_preset(self, pid, start=None):
        """Übernimmt Klima/Licht/Bewässerung eines Zyklus. -> (settings, fehler)"""
        with self.lock:
            p = self.presets().get(pid)
            if not p:
                return None, {"": "unbekannter Zyklus"}
            patch = {s: dict(p[s]) for s in ("climate", "light", "irrigation") if s in p}
            if "light" in patch and "enabled" not in patch["light"]:
                patch["light"]["enabled"] = True
            patch["grow"] = {"stage": pid, "start_date": start or datetime.now().strftime("%Y-%m-%d")}
            new, errors = self.update_settings(patch)
            if not errors:
                self._adv_block = None
                self.event("settings", f"Zyklus angewendet: {p['label']}")
            return new, errors

    def _advance(self, now):
        """Wechselt automatisch zum Folge-Zyklus, wenn die Dauer des aktuellen abgelaufen ist."""
        key = (now.hour, now.minute)
        if getattr(self, "_adv_last", None) == key:
            return
        self._adv_last = key
        for _ in range(10):  # Ketten abarbeiten (z. B. nach längerem Stillstand)
            g = self.cfg["grow"]
            p = self.presets().get(g["stage"])
            if not p or not p["days"] or not p["next"] or not g["start_date"] or self._adv_block == g["stage"]:
                return
            start = datetime.strptime(g["start_date"], "%Y-%m-%d").date()
            if (now.date() - start).days < p["days"]:
                return
            _, errors = self.apply_preset(p["next"], (start + timedelta(days=p["days"])).isoformat())
            if errors:
                self._adv_block = g["stage"]
                log.warning("Automatischer Zykluswechsel fehlgeschlagen: %s", errors)
                return
            self.event("system", f"Automatischer Wechsel: {p['label']} → {self.presets()[p['next']]['label']}")

    def set_mode(self, name, mode):
        if name not in self.outs or mode not in ("auto", "on", "off"):
            raise ValueError("ungültig")
        with self.lock:
            self.outs[name].mode = mode
            self.outs[name].manual_angle = None
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
