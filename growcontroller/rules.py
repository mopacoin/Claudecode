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


def exhaust_plan(cfg, day, temp, hum, t_target, h_target, room_t=None, room_h=None):
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
    return round(lo + (max(hi, lo) - lo) * max(ft, fh), 1), notes


def exhaust_watts(cfg, day, temp, hum, t_target, h_target, room_t=None, room_h=None):
    return exhaust_plan(cfg, day, temp, hum, t_target, h_target, room_t, room_h)[0]
