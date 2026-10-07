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
    fan_hot = hysteresis_high(temp, cur["fan"], tt, tt + th)
    fan_wet = not has_dehum and hysteresis_high(hum, cur["fan"], ht, ht + hh)
    dehum = hysteresis_high(hum, cur.get("dehumidifier", False), ht, ht + hh)
    fan_cycle = now.minute < cfg.get("fan_min_per_hour", 0)
    return {
        "heater": hysteresis(temp, cur["heater"], tt - th, tt),
        "humidifier": hysteresis(hum, cur["humidifier"], ht - hh, ht) and not (fan_hot or fan_wet or dehum),
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
