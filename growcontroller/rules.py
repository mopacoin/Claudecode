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


def exhaust_plan(cfg, day, temp, hum, t_target, h_target, room_t=None, room_h=None, active=(), dehum_min=0):
    """Abluft-Leistung in W + Hinweise, was per Abluft nicht erreichbar ist.

    Abluft zieht Raumluft nach: Das Zelt kann nicht kühler als der Raum (+ Abstand) und nicht
    trockener als Raumluft auf Zelttemperatur werden. Geregelt wird nur auf den erreichbaren Teil."""
    lo, hi = cfg["min_w"], cfg["max_w"] if day else cfg["max_w_night"]
    if temp is None and hum is None:
        return hi, []  # ohne Messwerte lieber lüften
    notes, t_goal, h_goal = [], t_target, h_target
    if cfg.get("room_aware", True) and room_t is not None:
        floor_t = room_t + cfg.get("room_margin", 0.5)
        if temp is not None and temp > t_target and floor_t > t_target:
            t_goal = floor_t
            notes.append({"key": "temp", "floor": round(floor_t, 1), "room": room_t, "useless": floor_t >= temp})
        if room_h is not None and temp is not None and hum is not None:
            floor_h = rh_at(temp, abs_hum(room_t, room_h)) + 2
            if hum > h_target and floor_h > h_target:
                h_goal = floor_h
                notes.append({"key": "hum", "floor": round(min(floor_h, 100), 1), "room": room_h, "useless": floor_h >= hum})
    ft = _clamp((temp - t_goal) / cfg["temp_band"]) if temp is not None else 0
    fh = _clamp((hum - h_goal) / cfg["hum_band"]) if hum is not None else 0
    # Gerätekopplung (Interlock): Abluft arbeitet nicht gegen Entfeuchter, Befeuchter oder Heizung.
    keep_t = cfg.get("temp_priority", True)  # zu heiß -> Abluft darf trotzdem hoch
    if "dehumidifier" in active and cfg.get("interlock_dehum", True):
        assist = cfg.get("dehum_assist_min", 20)
        # Stufe 2: Entfeuchter kommt nach `assist` Minuten nicht hinterher -> Abluft hilft beim Rest über Soll + Band
        if assist and dehum_min >= assist and hum is not None and hum > h_goal + cfg["hum_band"]:
            fh = _clamp((hum - h_goal - cfg["hum_band"]) / cfg["hum_band"])
            notes.append({"key": "interlock", "dev": "dehumidifier", "assist": True})
        else:
            fh = 0
            notes.append({"key": "interlock", "dev": "dehumidifier", "assist": False})
        if not keep_t:
            ft = 0
    for dev, opt in (("humidifier", "interlock_hum"), ("heater", "interlock_heat")):
        if dev in active and cfg.get(opt, True):
            fh = 0
            if not keep_t or dev == "heater":  # Heizung läuft = es ist zu kalt, Temperaturbedarf entfällt ohnehin
                ft = 0
            notes.append({"key": "interlock", "dev": dev, "assist": False})
    return round(lo + (max(hi, lo) - lo) * max(ft, fh), 1), notes


def exhaust_watts(cfg, day, temp, hum, t_target, h_target, room_t=None, room_h=None, active=(), dehum_min=0):
    return exhaust_plan(cfg, day, temp, hum, t_target, h_target, room_t, room_h, active, dehum_min)[0]


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
