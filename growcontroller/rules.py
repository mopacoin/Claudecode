"""Reine Regellogik (ohne Hardware) – einfach testbar."""
import math
from datetime import datetime, time as dtime


def _t(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def light_on(cfg, now):
    on, off = _t(cfg["on"]), _t(cfg["off"])
    t = now.time()
    return on <= t < off if on < off else (t >= on or t < off)


def hysteresis(value, current, low, high):
    """Schaltet an bei value<low, aus bei value>=high (Heizen/Befeuchten)."""
    if value is None:
        return False
    if value < low:
        return True
    if value >= high:
        return False
    return current


def hysteresis_high(value, current, low, high):
    """Schaltet an bei value>high, aus bei value<=low (Lüften/Kühlen)."""
    if value is None:
        return False
    if value > high:
        return True
    if value <= low:
        return False
    return current


def climate(cfg, day, temp, hum, cur, now, has_dehum=False):
    p = "day" if day else "night"
    tt, th = cfg[f"temp_{p}"], cfg["temp_hyst"]
    ht, hh = cfg[f"hum_{p}"], cfg["hum_hyst"]
    fan_hot = hysteresis_high(temp, cur.get("fan", False), tt, tt + th)
    fan_wet = not has_dehum and hysteresis_high(hum, cur.get("fan", False), ht, ht + hh)
    dehum = hysteresis_high(hum, cur.get("dehumidifier", False), ht, ht + hh)
    fan_cycle = now.minute < cfg.get("fan_min_per_hour", 0)
    return {
        "heater": hysteresis(temp, cur.get("heater", False), tt - th, tt),
        "humidifier": hysteresis(hum, cur.get("humidifier", False), ht - hh, ht) and not (fan_hot or fan_wet or dehum),
        "dehumidifier": dehum,
        "fan": fan_hot or fan_wet or fan_cycle,
    }


def irrigation_next(cfg, last_run, now):
    """Nächster Bewässerungszeitpunkt unter Beachtung des erlaubten Zeitfensters (oder None)."""
    from datetime import timedelta
    if not cfg.get("enabled", True):
        return None
    t = max(now, last_run + timedelta(minutes=cfg["interval_min"])) if last_run else now
    lo, hi = cfg.get("from_hour", 0), cfg.get("to_hour", 24)
    for _ in range(3):
        if t.hour < lo:
            return t.replace(hour=lo, minute=0, second=0, microsecond=0)
        if t.hour < hi:
            return t
        t = (t + timedelta(days=1)).replace(hour=lo, minute=0, second=0, microsecond=0)
    return t


def irrigation_due(cfg, last_run, now):
    """last_run: datetime|None. Pumpe läuft cfg['duration_s'] Sekunden, wenn fällig."""
    if not cfg.get("enabled", True):
        return False
    h = now.hour
    if not (cfg.get("from_hour", 0) <= h < cfg.get("to_hour", 24)):
        return False
    return last_run is None or (now - last_run).total_seconds() >= cfg["interval_min"] * 60


def svp(t):
    """Sättigungsdampfdruck in kPa (Tetens)."""
    return 0.6108 * math.exp(17.27 * t / (t + 237.3))


def vpd(temp, hum, leaf_offset=-2.0):
    """Blatt-VPD in kPa: SVP(Blatttemperatur) - tatsächlicher Dampfdruck der Luft."""
    if temp is None or hum is None:
        return None
    return round(max(0.0, svp(temp + leaf_offset) - hum / 100 * svp(temp)), 2)


def rh_for_vpd(temp, target, leaf_offset=-2.0):
    """Relative Feuchte, bei der die Luft bei `temp` das Ziel-VPD ergibt."""
    rh = (svp(temp + leaf_offset) - target) / svp(temp) * 100
    return min(95.0, max(20.0, round(rh, 1)))


def rh_delta_for_vpd(temp, delta):
    return max(1.0, round(delta / svp(temp) * 100, 1))


def _clamp(x):
    return max(0.0, min(1.0, x))


def watts_for_angle(cal, angle):
    """Lineare Interpolation der Kennlinie [[winkel, watt], …] (nach Winkel sortiert)."""
    if angle <= cal[0][0]:
        return cal[0][1]
    for (a0, w0), (a1, w1) in zip(cal, cal[1:]):
        if angle <= a1:
            return w0 + (w1 - w0) * (angle - a0) / (a1 - a0)
    return cal[-1][1]


def angle_for_watts(cal, watts):
    """Umkehrung der Kennlinie: kleinster Winkel, der die gewünschte Leistung erreicht."""
    if watts <= cal[0][1]:
        return cal[0][0]
    for (a0, w0), (a1, w1) in zip(cal, cal[1:]):
        if watts <= w1:
            return a1 if w1 == w0 else a0 + (a1 - a0) * (watts - w0) / (w1 - w0)
    return cal[-1][0]


def abs_hum(t, rh):
    """Absolute Feuchte in g/m³."""
    return 6.112 * math.exp(17.67 * t / (t + 243.5)) * rh * 2.1674 / (273.15 + t)


def rh_at(t, ah):
    """Relative Feuchte, die Luft mit absoluter Feuchte `ah` bei Temperatur `t` hätte."""
    return ah * (273.15 + t) / (6.112 * math.exp(17.67 * t / (t + 243.5)) * 2.1674)


def exhaust_plan(cfg, day, temp, hum, t_target, h_target, room_t=None, room_h=None, active=(), dehum_min=0, ff=None):
    """Abluft-Leistung in W + Hinweise, was per Abluft nicht erreichbar ist.

    Abluft zieht Raumluft nach: Das Zelt kann nicht kühler als der Raum (+ Abstand) und nicht
    trockener als Raumluft auf Zelttemperatur werden. Geregelt wird nur auf den erreichbaren Teil.

    ff: gelernte Vorsteuerung {"temp": W, "hum": W} – die Leistung, die das Ziel erfahrungsgemäß hält. Dann regelt der
    P-Anteil um diesen Wert herum (auch nach unten), statt immer bei der Grundlast zu beginnen: keine bleibende
    Abweichung mehr. Ohne gelernte Werte (None) bleibt es beim reinen P-Regler ab Grundlast."""
    lo, hi = cfg["min_w"], cfg["max_w"] if day else cfg["max_w_night"]
    if temp is None and hum is None:
        return hi, []  # ohne Messwerte lieber lüften
    notes, t_goal, h_goal, ff = [], t_target, h_target, dict(ff or {})
    if cfg.get("room_aware", True) and room_t is not None:
        floor_t = room_t + cfg.get("room_margin", 0.5)
        if temp is not None and temp > t_target and floor_t > t_target:
            t_goal = floor_t
            ff.pop("temp", None)  # Raumluft begrenzt: nicht auf ein unerreichbares Ziel vorsteuern
            notes.append({"key": "temp", "floor": round(floor_t, 1), "room": room_t, "useless": floor_t >= temp})
        if room_h is not None and temp is not None and hum is not None:
            floor_h = rh_at(temp, abs_hum(room_t, room_h)) + 2
            if hum > h_target and floor_h > h_target:
                h_goal = floor_h
                ff.pop("hum", None)
                notes.append({"key": "hum", "floor": round(min(floor_h, 100), 1), "room": room_h, "useless": floor_h >= hum})
    ft = (temp - t_goal) / cfg["temp_band"] if temp is not None else 0
    fh = (hum - h_goal) / cfg["hum_band"] if hum is not None else 0
    # Gerätekopplung (Interlock): Abluft arbeitet nicht gegen Entfeuchter, Befeuchter oder Heizung.
    keep_t = cfg.get("temp_priority", True)  # zu heiß -> Abluft darf trotzdem hoch
    if "dehumidifier" in active and cfg.get("interlock_dehum", True):
        assist = cfg.get("dehum_assist_min", 20)
        # Stufe 2: Entfeuchter kommt nach `assist` Minuten nicht hinterher -> Abluft hilft beim Rest über Soll + Band
        if assist and dehum_min >= assist and hum is not None and hum > h_goal + cfg["hum_band"]:
            fh = (hum - h_goal - cfg["hum_band"]) / cfg["hum_band"]
            notes.append({"key": "interlock", "dev": "dehumidifier", "assist": True})
        else:
            fh = 0
            notes.append({"key": "interlock", "dev": "dehumidifier", "assist": False})
        ff.pop("hum", None)  # Entfeuchter übernimmt die Feuchte
        if not keep_t:
            ft = 0
            ff.pop("temp", None)
    for dev, opt in (("humidifier", "interlock_hum"), ("heater", "interlock_heat")):
        if dev in active and cfg.get(opt, True):
            fh = 0
            ff.pop("hum", None)
            if not keep_t or dev == "heater":  # Heizung läuft = es ist zu kalt, Temperaturbedarf entfällt ohnehin
                ft = 0
                ff.pop("temp", None)
            notes.append({"key": "interlock", "dev": dev, "assist": False})
    span = max(hi, lo) - lo

    def part(f, base):
        if base is None:  # nichts gelernt: P-Regler ab Grundlast, nur nach oben
            return lo + span * _clamp(f)
        return base + span * max(-1.0, min(1.0, f))  # um die gelernte Leistung herum

    w = max(part(ft, ff.get("temp")), part(fh, ff.get("hum")))
    return round(max(lo, min(max(hi, lo), w)), 1), notes


def exhaust_watts(cfg, day, temp, hum, t_target, h_target, room_t=None, room_h=None, active=(), dehum_min=0, ff=None):
    return exhaust_plan(cfg, day, temp, hum, t_target, h_target, room_t, room_h, active, dehum_min, ff)[0]


def ramp(prev, target, max_step):
    """Begrenzt die Änderung pro Regeltakt (sanfte Rampe statt Sprüngen)."""
    if prev is None or max_step <= 0:
        return target
    return prev + max(-max_step, min(max_step, target - prev))


def isotonic(points):
    """Gewichtete monotone Regression (Pool Adjacent Violators): [(winkel, watt, gewicht)] nach Winkel sortiert
    -> [[winkel, watt]] mit nicht fallender Leistung. Glättet Messrauschen, ohne die Kurve zu verbiegen."""
    blocks = []
    for a, w, n in points:
        blocks.append([w * n, n, [a]])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, n2, al = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += n2
            blocks[-1][2] += al
    return [[a, round(s / n, 1)] for s, n, al in blocks for a in al]


def learned_curve(bins, manual, min_n=3, near=8):
    """Kennlinie aus gelernten Messpunkten (bins: {key: [winkel, watt, n]}) plus manuellen Punkten, die weiter als
    `near` Grad von einem gelernten Punkt entfernt sind (Gewicht 1)."""
    pts = [(round(a), w, min(n, 20)) for a, w, n in bins.values() if n >= min_n]
    for a, w in manual:
        if all(abs(a - p[0]) > near for p in pts):
            pts.append((a, w, 1))
    pts.sort()
    merged = {}
    for a, w, n in pts:  # gleicher Winkel -> zusammenfassen
        s, m = merged.get(a, (0, 0))
        merged[a] = (s + w * n, m + n)
    return isotonic([(a, s / m, m) for a, (s, m) in sorted(merged.items())])



# --- Wirksamkeit der Abluft: wie stark senkt mehr Leistung den Abstand Zelt–Raum? ---
# Eingeschwungen gilt grob: Zelt − Raum ≈ Wärme- bzw. Feuchtelast / Luftmenge. Je Leistungsfach wird der Abstand
# für Temperatur (°C) und absolute Feuchte (g/m³) gemittelt; mehr Leistung darf den Abstand nie vergrößern.

def effect_curve(bins, idx, min_n=2):
    """bins {key: [watt, dT, dAH, n]} -> [[watt, abstand]] nach Watt sortiert, Abstand nicht steigend (idx 1 = °C, 2 = g/m³)."""
    pts = sorted((b[0], b[idx], min(b[3], 30)) for b in bins.values() if b[3] >= min_n)
    if len(pts) < 2:
        return []
    neg = isotonic([(w, -d, n) for w, d, n in pts])
    return [[w, round(-d, 2)] for w, d in neg]


def effect_at(curve, w):
    """Erwarteter Abstand Zelt–Raum bei Leistung w (linear, an den Rändern gehalten)."""
    if not curve:
        return None
    if w <= curve[0][0]:
        return curve[0][1]
    for (w0, d0), (w1, d1) in zip(curve, curve[1:]):
        if w <= w1:
            return d0 + (d1 - d0) * (w - w0) / (w1 - w0) if w1 > w0 else d1
    return curve[-1][1]


def watts_for_effect(curve, need, lo, near=5, min_span=10):
    """Leistung, bei der der Abstand Zelt–Raum auf `need` sinkt – oder None, wenn das außerhalb des Gelernten liegt.
    Reicht selbst die größte gelernte Leistung nicht, ebenfalls None: dann regelt der P-Anteil allein, statt dauerhaft
    auf Maximum vorzusteuern (sonst liefe die Abluft auch dann laut, wenn das Zelt schon unter dem Ziel ist)."""
    if len(curve) < 2 or curve[-1][0] - curve[0][0] < min_span or need is None:
        return None
    if need >= curve[0][1]:  # schon die kleinste gelernte Leistung reicht
        return lo if curve[0][0] <= lo + near else None
    if need < curve[-1][1]:
        return None
    for (w0, d0), (w1, d1) in zip(curve, curve[1:]):
        if d1 <= need <= d0:
            return round(w0 if d0 == d1 else w0 + (w1 - w0) * (d0 - need) / (d0 - d1), 1)
    return None


# --- Klima-Logik: vorausschauend und nach Wirkung/Kosten abgewogen statt fester Schaltschwellen ---

def slope(points):
    """Steigung (pro Minute) der Ausgleichsgeraden durch [(minute, wert)] oder None."""
    n = len(points)
    if n < 3:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    return sum((p[0] - mx) * (p[1] - my) for p in points) / sxx if sxx else None


def smart_climate(cfg, day, temp, hum, cur, now, ctx):
    """Entscheidet Heizung, Befeuchter und Entfeuchter anhand von Prognose, gelernter Wirkung und Kosten.

    ctx: trend {"dT": °C/h, "dAH": g/m³/h} (gemessen, None = unbekannt) · devices {rolle: {"power_w", "t", "ah", "n"}}
    (gelernte Wirkung je Stunde, falls vorhanden) · exhaust {"can_dry", "extra_w"} (kann die Abluft das selbst und was
    kostet es zusätzlich) · waited_min (so lange setzt die Logik schon auf die Abluft statt auf den Entfeuchter).
    Gleiches Schaltband wie die feste Hysterese, aber auf den Wert in `lookahead_min` Minuten bezogen: Steigt die Feuchte,
    geht der Entfeuchter früher an; fällt sie schon, bleibt er aus. -> (want, info)"""
    base = climate(cfg, day, temp, hum, cur, now, ctx.get("has_dehum", "dehumidifier" in ctx.get("devices", {})))
    if temp is None or hum is None:
        return base, {"reasons": {}, "forecast": None}
    p = "day" if day else "night"
    tt, th, ht, hh = cfg[f"temp_{p}"], cfg["temp_hyst"], cfg[f"hum_{p}"], cfg["hum_hyst"]
    tr, devs, ex = ctx.get("trend") or {}, ctx.get("devices", {}), ctx.get("exhaust") or {}
    h = cfg.get("lookahead_min", 10) / 60
    ah = abs_hum(temp, hum)
    tf = temp + (tr.get("dT") or 0) * h
    hf = min(100.0, max(0.0, rh_at(tf, ah + (tr.get("dAH") or 0) * h)))
    want, why = dict(base), {}

    # Heizung (Abwärme des Entfeuchters steckt schon im gemessenen Trend)
    if "heater" in devs:
        if cur.get("heater"):
            want["heater"] = not (tf >= tt or temp >= tt + th / 2)
        else:
            want["heater"] = tf < tt - th or temp <= tt - 2 * th
        why["heater"] = f"heizt bis Prognose ≥ {tt:g} °C" if want["heater"] else f"aus – Prognose {tf:.1f} °C reicht"

    # Entfeuchter: Abluft oder Entfeuchter? Nach Leistung gewichtet, Nebenwirkung auf die Temperatur zählt mit
    prefer_ex = False
    if "dehumidifier" in devs:
        d = devs["dehumidifier"]
        if cur.get("dehumidifier"):
            on = not (hf <= ht or hum <= ht - hh)
            msg = f"läuft bis Prognose ≤ {ht:g} %" if on else f"aus – Prognose {hf:.0f} % erreicht Ziel"
            if on and d.get("ah") is not None and d.get("n", 0) >= 2 and d["ah"] > -0.2:
                msg += " · zeigt kaum Wirkung (Tank voll?)"
        elif hum >= ht + 2 * hh:
            on, msg = True, f"Feuchte {hum:.0f} % weit über Ziel"
        elif hf > ht + hh:
            warm = (tf - tt) / max(th, 0.1)  # >0 zu warm, <0 zu kalt
            heat = d.get("t") if d.get("t") is not None else 1.0  # Entfeuchter heizt (gelernt oder Annahme)
            cost_d = d.get("power_w", 250) * (1 + 2 * max(0.0, warm) * (heat > 0))
            if ex.get("can_dry"):
                cost_e = max(5.0, ex.get("extra_w", 0)) * (1 + 2 * max(0.0, -warm))
                if cost_e < cost_d and ctx.get("waited_min", 0) < cfg.get("dehum_wait_min", 15):
                    on, prefer_ex = False, True
                    msg = f"Abluft entfeuchtet günstiger (≈ +{ex.get('extra_w', 0):.0f} W statt {d.get('power_w', 250):.0f} W)"
                else:
                    on = True
                    msg = (f"Abluft schafft es seit {ctx.get('waited_min', 0):.0f} min nicht – Entfeuchter übernimmt"
                           if cost_e < cost_d else "Entfeuchter günstiger als Abluft (Temperatur mitgewichtet)")
            else:
                on, msg = True, f"Prognose {hf:.0f} % über {ht + hh:g} % – Raumluft zu feucht für Abluft"
        else:
            on, msg = False, (f"aus – Feuchte fällt bereits (Prognose {hf:.0f} %)" if hum > ht + hh else "aus – Prognose im Ziel")
        want["dehumidifier"] = on
        why["dehumidifier"] = msg

    # Befeuchter: nicht gegen Entfeuchter oder kräftig kühlende Abluft arbeiten
    if "humidifier" in devs:
        busy = want.get("dehumidifier") or tf > tt + th
        if cur.get("humidifier"):
            on = not (hf >= ht or busy)
        else:
            on = (hf < ht - hh or hum <= ht - 2 * hh) and not busy
        want["humidifier"] = on
        why["humidifier"] = (f"befeuchtet bis Prognose ≥ {ht:g} %" if on else
                             "gesperrt – Entfeuchter oder Kühlung aktiv" if busy else "aus – Prognose im Ziel")
    return want, {"reasons": why, "forecast": {"temp": round(tf, 1), "hum": round(hf, 1), "min": cfg.get("lookahead_min", 10)},
                  "prefer_exhaust": prefer_ex}
