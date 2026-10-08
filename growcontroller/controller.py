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
LEARN_BIN = 15        # Grad je Lernpunkt-Fach (gleich der Messfahrt-Schrittweite)
MIN_W = 2.0           # darunter gilt die Abluft-Steckdose als aus -> nicht lernen
MANUAL_MAX_S = 600    # Kalibrier-Test endet spätestens nach 10 min von selbst
SETTLE_S = 8          # nach jeder Servo-Bewegung so lange warten, bis der Messwert stimmt (Lüfter läuft ein)
SAMPLES = 4           # dann Mittelwert aus so vielen echten Abfragen der Steckdose
SWEEP_STEP = 15       # Schrittweite der automatischen Kalibrierung
SWEEP_MAX_S = 60      # bleiben die Messwerte länger aus, wird die Messfahrt abgebrochen
MAX_POINTS = 600
EFF_BIN = 5           # Watt je Wirksamkeits-Fach
EFF_WIN = 10          # so viele Minuten muss alles ruhig sein, bevor ein Wirksamkeits-Punkt zählt
EFF_LIGHT_MIN = 30    # nach Licht an/aus so lange warten (Lampenwärme/Verdunstung schwingen erst ein)
DEV_ROLES = ("dehumidifier", "humidifier", "heater")
DEV_POWER = {"dehumidifier": 250, "humidifier": 30, "heater": 300}   # Annahme, wenn "power_w" nicht in config.json steht
DEV_SETTLE_MIN = 3    # Wirkung erst ab Minute 3 nach dem Einschalten messen
DEV_ON_MIN = 15       # spätestens nach so vielen Minuten Laufzeit auswerten
EFF_DEVICES = {"dehumidifier": "Entfeuchter", "humidifier": "Befeuchter", "heater": "Heizung"}
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
        # Kalibrier-Versatz je Sensor (z. B. "temp_offset": -0.4, "hum_offset": 2) – wie bei Profi-Controllern
        self._offsets = {n: (float(c.get("temp_offset", 0)), float(c.get("hum_offset", 0))) for n, c in cfg["sensors"].items()}
        self._sensor_ok = {n: time.monotonic() for n in self.sensors}  # letzte erfolgreiche Messung je Sensor
        self._rt = {}   # Messwert -> Zeitpunkt; veraltete Werte werden entfernt statt weiter angezeigt/geregelt
        self.rev = 0    # Einstellungs-Revision: die Oberfläche lädt nach, wenn sich etwas geändert hat
        self._ev_n = 0
        self._warned = {}
        self.exhaust_notes = []
        self._on_since = {}  # Rolle -> seit wann an (monotonic), für Entfeuchter-Stufe 2
        self.gpio, self.outs, self.hubs = outputs.create_all(cfg)
        self.has_dehum = any(o.role == "dehumidifier" for o in self.outs.values())
        for n, m in self.saved.get("modes", {}).items():
            if n in self.outs and m in ("auto", "on", "off"):
                self.outs[n].mode = m
        self.cal = settings.validate_cal(self.saved.get("servo_cal"))[0] or settings.DEFAULT_CAL
        self.learn = {k: v for k, v in self.saved.get("servo_learn", {}).items() if isinstance(v, list) and len(v) == 3}
        self.sweep = None
        eff = self.saved.get("effect", {}).get("exhaust", {})
        self.effect = {p: {k: v for k, v in eff.get(p, {}).items() if isinstance(v, list) and len(v) == 4}
                       for p in ("day", "night")}
        self._eff = {"min": None, "phase": None, "phase_t": time.monotonic(), "win": deque(maxlen=EFF_WIN), "why": "startet"}
        self.exhaust_ff = {}
        self.dev_eff = {k: v for k, v in self.saved.get("effect", {}).get("devices", {}).items() if isinstance(v, dict)}
        self._ep, self._wait, self.smart = {}, None, {"reasons": {}, "forecast": None}
        self._lrn = {"angle": None, "since": 0.0, "last": 0.0, "saved": time.monotonic()}
        self.fresh = {}  # Messwerte genau dieses Regeltakts (ohne veraltete)
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
                        row = self._parse_row(r)
                        if row and datetime.fromisoformat(row["t"]) >= cutoff:
                            self.minutes.append(row)
            except (OSError, ValueError, KeyError, csv.Error) as e:  # Verlauf darf den Start nie verhindern
                log.warning("Verlauf %s nicht lesbar: %s", path, e)
        self.minutes = deque(sorted(self.minutes, key=lambda r: r["t"]), maxlen=self.minutes.maxlen)

    @staticmethod
    def _parse_row(r):
        """CSV-Zeile -> dict; überzählige/fehlerhafte Felder werden ignoriert, kaputte Zeilen übersprungen."""
        try:
            datetime.fromisoformat(r.get("t") or "")
        except (TypeError, ValueError):
            return None
        row = {"t": r["t"]}
        for k, v in r.items():
            if k in (None, "t") or not isinstance(v, str) or v == "":
                continue
            try:
                row[k] = float(v)
            except ValueError:
                pass
        return row

    def event(self, kind, msg):
        e = {"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, "msg": msg}
        self.events.append(e)
        try:
            with open(self._p("events.jsonl"), "a") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            self._ev_n += 1
            if self._ev_n % 200 == 0 and os.path.getsize(self._p("events.jsonl")) > 1_000_000:
                with open(self._p("events.jsonl")) as f:  # SD-Karte nicht volllaufen lassen
                    keep = f.readlines()[-2000:]
                with open(self._p("events.jsonl"), "w") as f:
                    f.writelines(keep)
        except OSError:
            pass

    # --- Messen ---
    def read_sensors(self):
        merged, main, mono = {}, False, time.monotonic()
        for name, (prefix, s) in self.sensors.items():
            try:
                t_off, h_off = self._offsets.get(name, (0.0, 0.0))
                for k, v in s.read().items():
                    if v is None:
                        continue
                    v = float(v) + (t_off if k == "temp" else h_off if k == "hum" else 0.0)
                    if k == "hum":
                        v = min(100.0, max(0.0, v))
                    merged[prefix + k] = v if k.endswith("seq") else round(v, 1 if k in ("temp", "hum") else 2)
                    main = main or (not prefix and k in ("temp", "hum"))
                self._sensor_ok[name] = mono
            except Exception as e:  # Sensorfehler dürfen die Regelung nicht beenden
                now = time.monotonic()
                if now - self._warned.get(name, -1e9) > 60:  # höchstens 1x pro Minute ins Log
                    self._warned[name] = now
                    log.warning("Sensor %s: %s", name, e)
        self.fresh = merged
        self.readings.update(merged)
        for k in merged:
            self._rt[k] = mono
        timeout = self.cfg.get("sensor_timeout_s", 120)
        for k in [k for k, t in self._rt.items() if mono - t > timeout]:  # nichts Veraltetes anzeigen oder regeln
            self.readings.pop(k, None)
            self._rt.pop(k)
        if main:  # nur der Regel-Sensor zählt für "Sensordaten aktuell"
            self.last_ok = mono

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
            mono = time.monotonic()
            for role, on in cur.items():
                if on:
                    self._on_since.setdefault(role, mono)
                else:
                    self._on_since.pop(role, None)
            active = {r for r, on in cur.items() if on}
            dehum_min = (mono - self._on_since["dehumidifier"]) / 60 if "dehumidifier" in self._on_since else 0
            want = {"light": day and self.cfg["light"]["enabled"]}
            if self.stale():
                # Failsafe: ohne Messwerte Klima-Aktoren aus, Lüfter bleibt zyklisch an
                want.update(heater=False, humidifier=False, dehumidifier=False, fan=now.minute < 10)
            else:
                cl, r = self.effective_climate(day), self.readings
                if cl.get("logic", "smart") == "smart":
                    w, self.smart = rules.smart_climate(cl, day, r.get("temp"), r.get("hum"), cur, now,
                                                        self._smart_ctx(day, cl["hum_day" if day else "hum_night"]))
                    want.update(w)
                    if self.smart.get("prefer_exhaust"):
                        self._wait = self._wait or time.monotonic()
                    else:
                        self._wait = None
                else:
                    self.smart = {"reasons": {}, "forecast": None}
                    want.update(rules.climate(cl, day, r.get("temp"), r.get("hum"), cur, now, self.has_dehum))
            want["pump"] = self._pump(now)
            want["vent"] = want["intake"] = want["fan"]  # ohne stufenlose Abluft: folgen dem Lüfterbedarf
            ex_w, notes = None, []
            if not self.stale():
                p = "day" if day else "night"
                cl = self.effective_climate(day)
                r = self.readings
                self.exhaust_ff = self._effect_ff(day, cl[f"temp_{p}"], cl[f"hum_{p}"])
                ex_w, notes = rules.exhaust_plan(self.cfg["exhaust"], day, r.get("temp"), r.get("hum"), cl[f"temp_{p}"], cl[f"hum_{p}"],
                                                 r.get("room_temp"), r.get("room_hum"), active, dehum_min,
                                                 {k: v["w"] for k, v in self.exhaust_ff.items() if v["w"] is not None})
            self._exhaust_notes(notes)
            ex = self.cfg["exhaust"]
            if ex["enabled"] and ex.get("intake_pct", 0) and any(o.proportional for o in self.outs.values()):
                # Zuluft (Ein/Aus) unterstützt die Abluft, sobald diese kräftig läuft – wie die Unterdruck-Regelung
                # bei Profi-Controllern (Zuluft immer schwächer als Abluft, damit kein Geruch austritt)
                top = ex["max_w"] if day else ex["max_w_night"]
                w = top if ex_w is None else ex_w
                share = 100 * (w - ex["min_w"]) / (top - ex["min_w"]) if top > ex["min_w"] else 0
                pct = ex["intake_pct"]
                want["intake"] = share >= pct or (cur.get("intake", False) and share >= pct - 10) \
                    or now.minute < self.cfg["climate"].get("fan_min_per_hour", 0)
            for n, o in self.outs.items():
                if o.proportional:
                    self._apply_servo(o, want, day, ex_w)
                else:
                    o.apply(want.get(o.role, False))
                if o.state != self._prev_state[n]:
                    self._prev_state[n] = o.state
                    if not o.proportional:  # stufenlose Abluft wechselt laufend – nicht als EIN/AUS protokollieren
                        self.event("output", f"{n}: {'EIN' if o.state else 'AUS'}")
            self._effect_step(now, day, active)
            if getattr(self, "_dev_min", None) != (now.hour, now.minute):
                self._dev_min = (now.hour, now.minute)
                self._dev_learn(now)
            self._check_alarms()
            self._record(now)
            self._advance(now)

    def _apply_servo(self, o, want, day, ex_w):
        ex = self.cfg["exhaust"]
        if o.mode == "manual" and not self.sweep and time.monotonic() - getattr(o, "_manual_t", 0) > MANUAL_MAX_S:
            o.mode, o.manual_angle = getattr(o, "_prev_mode", "auto"), None
            self.event("mode", f"{o.name}: Kalibrier-Test nach {MANUAL_MAX_S // 60} min automatisch beendet")
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
            if o.mode == "auto":  # sanfte Rampe nur in der Automatik; manuelle Befehle wirken sofort
                dt = time.monotonic() - getattr(o, "_ramp_t", time.monotonic())
                w = rules.ramp(o.watts, w, ex.get("ramp_w_min", 0) * max(dt, 0) / 60)
            o._ramp_t = time.monotonic()
            o.apply_level(rules.angle_for_watts(self.cal, w), round(w, 1), ex["deadband_deg"])
        o.state = o.watts is not None and o.watts > ex["min_w"] + 0.5
        self._learn_step(o)

    def _exhaust_notes(self, notes):
        """Hinweis ins Ereignisprotokoll, wenn die Raumluft eine Grenze setzt (nur bei Änderung)."""
        notes_all, notes = notes, [n for n in notes if n["key"] in ("temp", "hum")]
        self.exhaust_interlock = [n for n in notes_all if n["key"] == "interlock"]
        old = {n["key"] for n in self.exhaust_notes}
        new = {n["key"] for n in notes}
        what = {"temp": "Temperatur", "hum": "Luftfeuchte"}
        for k in new - old:
            n = next(x for x in notes if x["key"] == k)
            unit = "°C" if k == "temp" else "%"
            self.event("system", f"{what[k]}-Ziel per Abluft nicht erreichbar: Raumluft erlaubt nur ca. {n['floor']} {unit}")
        for k in old - new:
            self.event("system", f"{what[k]}-Ziel per Abluft wieder erreichbar")
        self.exhaust_notes = notes

    # --- Kennlinie automatisch lernen (Leistungsmessung an der Abluft-Steckdose, Sensor-Prefix "exhaust_") ---
    def _learn_step(self, o):
        mono, w, seq = time.monotonic(), self.fresh.get("exhaust_w"), self.fresh.get("exhaust_seq")
        L = self._lrn
        if o.angle != L["angle"]:
            L["angle"], L["since"], L["vals"], L["seq"] = o.angle, mono, [], None
            if self.sweep:
                self.sweep["step_t"] = mono
            return
        # nur neue Abfragen der Steckdose zählen, und erst nach der Mindest-Einschwingzeit
        if w is not None and w >= MIN_W and mono - L["since"] >= SETTLE_S and (seq is None or seq != L.get("seq")):
            L["seq"] = seq
            L.setdefault("vals", []).append(w)
        if self.sweep:
            self._sweep_step(o, mono)
        elif len(L.get("vals", [])) >= SAMPLES and o.mode == "auto" and self.cfg["exhaust"].get("auto_learn", True):
            if mono - L["last"] >= 30:
                L["last"] = mono
                self._add_sample(o.angle, sum(L["vals"]) / len(L["vals"]))
            L["vals"] = []  # nächster Mittelwert aus neuen Messungen
            if mono - L["saved"] > 600:  # Lernstand höchstens alle 10 min speichern (SD-Karte schonen)
                self._save_learn(rebuild=True)

    def _add_sample(self, angle, w, weight=1):
        key = str(int((angle + LEARN_BIN / 2) // LEARN_BIN * LEARN_BIN))  # gleiche Fächer wie die Messfahrt
        a0, w0, n = self.learn.get(key, [angle, w, 0])
        k = weight / (min(n, 20) + weight)  # gleitender Mittelwert, neue Werte zählen weiter mit
        self.learn[key] = [round(a0 + (angle - a0) * k, 1), round(w0 + (w - w0) * k, 2), n + weight]
        if n < 3 <= n + weight:  # neues Fach verlässlich -> Kennlinie neu bauen
            self._save_learn(rebuild=True)

    def _save_learn(self, rebuild=False):
        if rebuild:
            pts, err = settings.validate_cal(rules.learned_curve(self.learn, self.cal))
            if not err and pts != self.cal:
                self.cal = pts
                self.saved["servo_cal"] = pts
        self.saved["servo_learn"] = self.learn
        settings.save(self._p("settings.json"), self.saved)
        self._lrn["saved"] = time.monotonic()

    # --- Wirksamkeit der Abluft lernen: Abstand Zelt–Raum je Leistung (Tag/Nacht getrennt) ---
    def _effect_ff(self, day, t_target, h_target):
        """Gelernte Vorsteuerung: welche Leistung hält das Ziel bei der aktuellen Raumluft? {temp|hum: {w, need}}"""
        ex, r = self.cfg["exhaust"], self.readings
        rt, rh, t = r.get("room_temp"), r.get("room_hum"), r.get("temp")
        if not ex.get("use_effect", True) or rt is None or rh is None:
            return {}
        bins, top = self.effect["day" if day else "night"], ex["max_w"] if day else ex["max_w_night"]
        need = {"temp": t_target - rt}
        if t is not None:
            need["hum"] = rules.abs_hum(t, h_target) - rules.abs_hum(rt, rh)
        out = {}
        for k, idx in (("temp", 1), ("hum", 2)):
            if k in need:
                curve = rules.effect_curve(bins, idx)
                w = rules.watts_for_effect(curve, need[k], ex["min_w"])
                if w is not None:
                    out[k] = {"w": w, "need": round(need[k], 2)}
                elif curve and curve[-1][0] >= top - 5 and need[k] < curve[-1][1]:  # nur zur Anzeige
                    out[k] = {"w": None, "need": round(need[k], 2), "unreachable": True}
        return out

    def _effect_why(self, o, active, mono):
        """Grund, warum gerade nicht gelernt wird (oder None)."""
        ex, r = self.cfg["exhaust"], self.readings
        if not ex.get("learn_effect", True):
            return "Lernen ausgeschaltet"
        if not ex["enabled"]:
            return "stufenlose Regelung aus"
        if self.sweep or o.mode != "auto":
            return "Abluft nicht auf Automatik"
        if self.stale() or r.get("temp") is None or r.get("hum") is None:
            return "keine aktuellen Zelt-Messwerte"
        if r.get("room_temp") is None or r.get("room_hum") is None:
            return "kein Raum-Sensor (prefix \"room_\")"
        if r.get("exhaust_w") is not None and r["exhaust_w"] < MIN_W:
            return "Abluft-Steckdose aus"
        busy = [EFF_DEVICES[d] for d in EFF_DEVICES if d in active]
        if busy:
            return f"{', '.join(busy)} läuft – verfälscht die Messung"
        if mono - self._eff["phase_t"] < EFF_LIGHT_MIN * 60:
            return f"wartet {EFF_LIGHT_MIN} min nach Start bzw. Licht an/aus"
        return None

    def _effect_step(self, now, day, active):
        o = next((x for x in self.outs.values() if x.proportional), None)
        E, mono = self._eff, time.monotonic()
        if not o or E["min"] == (now.hour, now.minute):
            return
        E["min"] = (now.hour, now.minute)  # 1 Wert pro Minute
        if E["phase"] != day:
            if E["phase"] is not None:
                E["phase_t"] = mono
            E["phase"] = day
            E["win"].clear()
        why = self._effect_why(o, active, mono)
        if why:
            E["win"].clear()
            E["why"] = why
            return
        r = self.readings
        w = r.get("exhaust_w", o.watts)
        if w is None:
            E["why"] = "noch keine Leistung bekannt"
            return
        dah = rules.abs_hum(r["temp"], r["hum"]) - rules.abs_hum(r["room_temp"], r["room_hum"])
        E["win"].append((w, r["temp"] - r["room_temp"], dah))
        win = list(E["win"])
        if len(win) < EFF_WIN:
            E["why"] = f"sammelt ruhige Minuten ({len(win)}/{EFF_WIN})"
            return
        spread = [max(c) - min(c) for c in zip(*win)]
        if spread[0] > 4:
            E["why"] = f"Leistung schwankt ({spread[0]:.0f} W)"
            return
        if spread[1] > 0.4 or spread[2] > 0.6:
            E["why"] = "Klima noch nicht eingeschwungen"
            return
        mw, dt, da = (sum(c) / len(c) for c in zip(*win))
        self._effect_add("day" if day else "night", mw, dt, da)
        E["win"].clear()  # nächster Punkt aus neuen Minuten
        E["why"] = f"Lernpunkt: {mw:.0f} W → Zelt {dt:+.1f} °C / {da:+.1f} g/m³ ggü. Raum"

    def _effect_add(self, phase, w, dt, da):
        bins = self.effect[phase]
        key = str(int(round(w / EFF_BIN) * EFF_BIN))
        w0, t0, a0, n = bins.get(key, [w, dt, da, 0])
        k = 1 / (min(n, 30) + 1)  # gleitender Mittelwert – folgt langsam Pflanzengröße und Jahreszeit
        bins[key] = [round(w0 + (w - w0) * k, 1), round(t0 + (dt - t0) * k, 2), round(a0 + (da - a0) * k, 2), n + 1]
        self.saved["effect"] = {"exhaust": self.effect}
        settings.save(self._p("settings.json"), self.saved)

    def reset_effect(self):
        with self.lock:
            self.effect = {"day": {}, "night": {}}
            self._eff["win"].clear()
            self.saved["effect"] = {"exhaust": self.effect}
            settings.save(self._p("settings.json"), self.saved)
            self.rev += 1
            self.event("settings", "Abluft-Wirksamkeit zurückgesetzt")

    def _effect_status(self):
        out = {p: {"bins": sorted(b.values()), "temp": rules.effect_curve(b, 1), "hum": rules.effect_curve(b, 2)}
               for p, b in self.effect.items()}
        return {**out, "ff": self.exhaust_ff, "why": self._eff["why"], "progress": len(self._eff["win"]), "need": EFF_WIN}

    # --- Wirkung der Schaltgeräte lernen: Trend vorher (aus) gegen Trend danach (an) ---
    def _trend(self, minutes=10):
        """Gemessener Trend der letzten Minuten: {"dT": °C/h, "dAH": g/m³/h} oder {} bei zu wenig Daten."""
        rows = [r for r in self._rows("1h") if "temp" in r and "hum" in r]  # gespeicherter Verlauf + Regeltakt
        if not rows:
            return {}
        t_end = datetime.fromisoformat(rows[-1]["t"])
        pts = [((datetime.fromisoformat(r["t"]) - t_end).total_seconds() / 60, r) for r in rows]
        pts = [(m, r) for m, r in pts if m >= -minutes]
        if not pts or pts[-1][0] - pts[0][0] < minutes / 2:
            return {}
        st = rules.slope([(m, r["temp"]) for m, r in pts])
        sa = rules.slope([(m, rules.abs_hum(r["temp"], r["hum"])) for m, r in pts])
        if st is None or sa is None:
            return {}
        return {"dT": round(max(-20, min(20, st * 60)), 2), "dAH": round(max(-20, min(20, sa * 60)), 2)}

    def _dev_learn(self, now):
        """1x pro Minute: nach jedem Einschalten die Wirkung messen (Trend 10 min vorher gegen Trend ab Minute 3)."""
        for n, o in self.outs.items():
            if o.proportional or o.role not in DEV_ROLES:
                continue
            ep = self._ep.get(n)
            if o.state and ep is None:
                self._ep[n] = ep = {"on": now, "done": False}
            if ep and not ep["done"]:
                dur = (now - ep["on"]).total_seconds() / 60
                if dur >= DEV_ON_MIN or (not o.state and dur >= DEV_SETTLE_MIN + 6):
                    ep["done"] = True
                    self._dev_sample(n, o, ep["on"], now)
            if not o.state and ep is not None:
                self._ep.pop(n)

    def _dev_sample(self, name, o, t_on, t_end):
        rows = [r for r in list(self.minutes) if "temp" in r and "hum" in r]
        def win(a, b):
            return [r for r in rows if a <= datetime.fromisoformat(r["t"]) < b]
        before = win(t_on - timedelta(minutes=10), t_on)
        after = win(t_on + timedelta(minutes=DEV_SETTLE_MIN), t_end + timedelta(seconds=1))
        if len(before) < 6 or len(after) < 6:
            return self._dev_skip(name, "zu wenig Messwerte")
        if any(r.get(name) for r in before) or not all(r.get(name, 1) for r in after):
            return self._dev_skip(name, "Gerät hat zwischendurch geschaltet")
        others = [m for m, x in self.outs.items() if m != name and not x.proportional and x.role not in ("intake", "pump")]
        if any(len({r.get(m) for r in before + after}) > 1 for m in others):
            return self._dev_skip(name, "ein anderes Gerät hat geschaltet")
        light = self.cfg["light"]
        if len({rules.light_on(light, datetime.fromisoformat(r["t"])) for r in before + after}) > 1:
            return self._dev_skip(name, "Licht hat geschaltet")
        def rate(rs, f):
            t0 = datetime.fromisoformat(rs[0]["t"])
            return rules.slope([((datetime.fromisoformat(r["t"]) - t0).total_seconds() / 60, f(r)) for r in rs]) * 60
        temp = lambda r: r["temp"]
        ah = lambda r: rules.abs_hum(r["temp"], r["hum"])
        dt, da = rate(after, temp) - rate(before, temp), rate(after, ah) - rate(before, ah)
        if abs(dt) > 30 or abs(da) > 30:
            return self._dev_skip(name, "unplausibler Messwert")
        e = self.dev_eff.get(name, {"t": dt, "ah": da, "n": 0})
        k = 1 / (min(e["n"], 20) + 1)
        self.dev_eff[name] = {"t": round(e["t"] + (dt - e["t"]) * k, 2), "ah": round(e["ah"] + (da - e["ah"]) * k, 2),
                              "n": e["n"] + 1, "last": t_end.isoformat(timespec="minutes"), "skip": None}
        self.saved.setdefault("effect", {})["devices"] = self.dev_eff
        settings.save(self._p("settings.json"), self.saved)
        self.event("system", f"Wirkung {name} gelernt: {dt:+.1f} °C/h, {da:+.1f} g/m³/h")

    def _dev_skip(self, name, why):
        self.dev_eff.setdefault(name, {"t": None, "ah": None, "n": 0})["skip"] = why

    def _smart_ctx(self, day, h_target):
        """Alles, was die Klima-Logik zum Abwägen braucht: Trend, Geräte mit Leistung und gelernter Wirkung, Abluft-Reserve."""
        devs = {}
        for n, o in self.outs.items():
            if o.role in DEV_ROLES and o.role not in devs and o.mode == "auto":
                e = self.dev_eff.get(n, {})
                devs[o.role] = {"name": n, "power_w": float(o.info.get("power_w", DEV_POWER[o.role])),
                                "t": e.get("t") if e.get("n") else None, "ah": e.get("ah") if e.get("n") else None, "n": e.get("n", 0)}
        ex, r = self.cfg["exhaust"], self.readings
        o = next((x for x in self.outs.values() if x.proportional), None)
        exi = {"can_dry": False}
        t, rt, rh = r.get("temp"), r.get("room_temp"), r.get("room_hum")
        if o and o.mode == "auto" and ex["enabled"] and None not in (t, rt, rh):
            top = ex["max_w"] if day else ex["max_w_night"]
            now_w = o.watts if o.watts is not None else ex["min_w"]
            room_ah, goal_ah = rules.abs_hum(rt, rh), rules.abs_hum(t, h_target)
            curve = rules.effect_curve(self.effect["day" if day else "night"], 2)
            if curve:  # gelernt: wie trocken wird das Zelt mit voller Abluft?
                can = room_ah + rules.effect_at(curve, top) <= goal_ah - 0.3
                need_w = rules.watts_for_effect(curve, goal_ah - room_ah, ex["min_w"])
            else:      # noch nichts gelernt: nur bei deutlich trockenerer Raumluft
                can, need_w = rules.rh_at(t, room_ah) + 5 <= h_target, None
            exi = {"can_dry": can and now_w < top - 3, "extra_w": round(max(0.0, (need_w or top) - now_w), 1)}
        return {"trend": self._trend(), "devices": devs, "exhaust": exi, "has_dehum": self.has_dehum,
                "waited_min": (time.monotonic() - self._wait) / 60 if self._wait else 0}

    def start_sweep(self):
        o = next((x for x in self.outs.values() if x.proportional), None)
        if not o:
            raise ValueError("Kein stufenloser Abluft-Servo konfiguriert")
        w = self.fresh.get("exhaust_w")
        if w is None:
            raise ValueError("Keine Leistungsmessung der Abluft (Sensor mit prefix \"exhaust_\")")
        if w < MIN_W:
            raise ValueError(f"Abluft misst {w:.1f} W – ist die Abluft-Steckdose eingeschaltet?")
        with self.lock:
            prev = getattr(o, "_prev_mode", "auto") if o.mode == "manual" else o.mode
            self.sweep = {"out": o.name, "angles": list(range(0, 181, SWEEP_STEP)), "i": 0, "prev_mode": prev,
                          "started": time.monotonic(), "step_t": time.monotonic(), "points": []}
            o.mode, o.manual_angle = "manual", 0
            self.event("settings", "Automatische Abluft-Kalibrierung gestartet")

    def stop_sweep(self, msg="abgebrochen"):
        with self.lock:
            if not self.sweep:
                return
            o = self.outs[self.sweep["out"]]
            o.mode, o.manual_angle = self.sweep.get("prev_mode", "auto"), None
            self.sweep = None
            self.rev += 1
            self.event("settings", f"Abluft-Kalibrierung {msg}")

    def _sweep_step(self, o, mono):
        sw, vals = self.sweep, self._lrn.get("vals", [])
        if o.angle != sw["angles"][sw["i"]]:
            return  # Servo noch nicht am Messwinkel
        w = self.fresh.get("exhaust_w")
        if w is not None and w < MIN_W and mono - sw["step_t"] > SETTLE_S:
            self.stop_sweep(f"abgebrochen: Abluft misst {w:.1f} W – Steckdose aus?")
            return
        if len(vals) >= SAMPLES:
            sw["points"].append([o.angle, round(sum(vals[:SAMPLES]) / SAMPLES, 1)])  # Mittelwert
            sw["i"] += 1
            if sw["i"] >= len(sw["angles"]):
                self.learn = {str(a): [a, w_, 5] for a, w_ in sw["points"]}  # Messfahrt ersetzt alte Lernpunkte
                pts, err = settings.validate_cal(rules.learned_curve(self.learn, []))
                if not err:
                    self.cal = pts
                    self.saved["servo_cal"] = pts
                self._save_learn()
                self.stop_sweep("abgeschlossen: " + ", ".join(f"{a}°={w_:g} W" for a, w_ in self.cal))
                return
            o.manual_angle, sw["step_t"] = sw["angles"][sw["i"]], mono
        elif mono - sw["step_t"] > SETTLE_S + SWEEP_MAX_S:  # keine (neuen) Messwerte
            self.stop_sweep("abgebrochen: keine Leistungsmesswerte")

    def set_calibration(self, points):
        pts, err = settings.validate_cal(points)
        if err:
            return None, err
        with self.lock:
            self.cal = pts
            self.saved["servo_cal"] = pts
            self.learn = {}  # manuelle Kennlinie hat Vorrang, Lernstand neu beginnen
            self.saved["servo_learn"] = {}
            settings.save(self._p("settings.json"), self.saved)
            self.rev += 1
            self.event("settings", "Servo-Kennlinie: " + ", ".join(f"{a}°={w:g} W" for a, w in pts))
        return pts, None

    def servo_manual(self, name, angle):
        if self.sweep:
            raise ValueError("Automatische Kalibrierung läuft")
        o = self.outs.get(name)
        if not o or not o.proportional or not isinstance(angle, (int, float)) or isinstance(angle, bool) or not 0 <= angle <= 180:
            raise ValueError("ungültig")
        with self.lock:
            if o.mode != "manual":
                o._prev_mode = o.mode
            o._manual_t = time.monotonic()
            o.mode, o.manual_angle = "manual", int(angle)
            o.apply_level(o.manual_angle, round(rules.watts_for_angle(self.cal, o.manual_angle), 1), 0)

    def _pump_out(self):
        return next((o for o in self.outs.values() if o.role == "pump"), None)

    def _pump(self, now):
        if not self._pump_out():  # ohne Pumpe keine (scheinbaren) Bewässerungen protokollieren
            return False
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
        o = self._pump_out()
        if not o:
            raise ValueError("Keine Pumpe konfiguriert (Ausgang mit Rolle \"pump\")")
        if o.mode == "off":
            raise ValueError("Die Pumpe steht auf „Aus“ – erst auf Auto stellen")
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
        mono_now = time.monotonic()
        for name, (prefix, _s) in self.sensors.items():
            if prefix:  # Zusatz-Sensoren (Raum, Leistungsmessung); der Regel-Sensor hat den Alarm oben
                conds[f"sensor:{name}"] = (mono_now - self._sensor_ok[name] > 300, f"Sensor {name} liefert keine Daten", 0)
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
        row = {k: v for k, v in row.items() if v is not None and not k.endswith("seq")}
        self.live.append(row)
        if self._last_minute != (now.hour, now.minute):
            self._last_minute = (now.hour, now.minute)
            self.minutes.append(row)
            self._append_csv(now, row)

    def _append_csv(self, now, row):
        fields = ["t", "temp", "hum", "vpd", "room_temp", "room_hum", "exhaust_w"] + list(self.outs)
        try:
            # Ändert sich die Spaltenliste (Geräte/Sensoren), in eine neue Datei mit passender Kopfzeile schreiben
            for suffix in [""] + [f"-{c}" for c in "bcdefghijklmnopqrstuvwxyz"]:
                path = self._p(f"log-{now:%Y-%m-%d}{suffix}.csv")
                if not os.path.exists(path):
                    break
                with open(path) as f:
                    if f.readline().strip().split(",") == fields:
                        break
            new = not os.path.exists(path)
            if new:
                self._cleanup_logs(now)
            with open(path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(row)
        except OSError as e:
            log.warning("CSV: %s", e)

    def _cleanup_logs(self, now):
        """Verlaufsdateien älter als history_days (Standard 400 Tage) löschen."""
        cutoff = (now - timedelta(days=self.cfg.get("history_days", 400))).strftime("%Y-%m-%d")
        for p in glob.glob(self._p("log-*.csv")):
            if os.path.basename(p)[4:14] < cutoff:
                try:
                    os.remove(p)
                except OSError:
                    pass

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
                r[k] = max(vals) if k in self.outs else round(sum(vals) / len(vals), 2)  # Geräte: an, wenn je an
            out.append(r)
        return out

    def export_csv(self, rng):
        import io
        with self.lock:
            rows = self._rows(rng)
        cols = ["t", "temp", "hum", "vpd", "room_temp", "room_hum", "exhaust_w"] + list(self.outs)
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
            gday = starts_in = None
            if g["start_date"]:
                gday = (now.date() - datetime.strptime(g["start_date"], "%Y-%m-%d").date()).days + 1
                if gday < 1:  # Start liegt in der Zukunft
                    gday, starts_in = None, 1 - gday
            ir, pump = self.cfg["irrigation"], self._pump_out()
            nxt = rules.irrigation_next(ir, self.last_irrigation, now) if pump else None
            return {
                "time": now.isoformat(timespec="seconds"), "uptime_s": int(time.time() - self.started), "rev": self.rev,
                "readings": self.readings, "vpd": self.vpd(), "stale": self.stale(), "is_day": day,
                "targets": {"temp": cl[f"temp_{p}"], "hum": cl[f"hum_{p}"], "vpd": self.cfg["climate"][f"vpd_{p}"],
                            "control": cl["control"]},
                "exhaust": {"notes": self.exhaust_notes, "interlock": getattr(self, "exhaust_interlock", []),
                            "measured_w": self.fresh.get("exhaust_w"),
                            "cal": self.cal,
                            "learned": sum(1 for v in self.learn.values() if v[2] >= 3),
                            "sweep": {"i": self.sweep["i"], "n": len(self.sweep["angles"]),
                                      "angle": self.sweep["angles"][self.sweep["i"]],
                                      "vals": [round(v, 1) for v in self._lrn.get("vals", [])]} if self.sweep else None,
                            "effect": self._effect_status()},
                "smart": {**self.smart, "trend": self._trend(), "logic": self.cfg["climate"].get("logic", "smart"),
                          "devices": {n: {**self.dev_eff.get(n, {"n": 0}), "role": o.role,
                                          "power_w": float(o.info.get("power_w", DEV_POWER[o.role]))}
                                      for n, o in self.outs.items() if o.role in DEV_ROLES and not o.proportional}},
                "stats": self.stats(), "alarms": [{"id": k, **v} for k, v in self.alarms.items()],
                "grow": {**self._grow_status(g, gday), "starts_in": starts_in},
                "irrigation": {"last": self.last_irrigation and self.last_irrigation.isoformat(timespec="seconds"),
                               "next": nxt and nxt.isoformat(timespec="minutes"), "available": bool(pump),
                               "enabled": ir["enabled"], "pump_mode": pump.mode if pump else None,
                               "running": time.monotonic() < self.pump_until},
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
            self.rev += 1
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
            self.rev += 1
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
            self.rev += 1
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
        if self.sweep and self.sweep["out"] == name:
            self.stop_sweep()
        with self.lock:
            self.outs[name].mode = mode
            self.outs[name].manual_angle = None
            self.saved.setdefault("modes", {})[name] = mode
            settings.save(self._p("settings.json"), self.saved)
            self.rev += 1
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
