"""Laufzeit-Einstellungen: Defaults, Validierung (eine Quelle für Server und UI) und Persistenz."""
import json
import os
import re
from datetime import datetime

PALETTE = ["#3ddc97", "#4db8ff", "#b794ff", "#ff8a5c", "#ffb547", "#ff5d8f", "#2dd4bf", "#a3e635"]
MAX_PRESETS = 30

# (Art, Parameter...): num(min,max) | int(min,max) | bool | enum(werte) | time | date | str(maxlen)
SPEC = {
    "climate": {
        "control": ("enum", ["temp_hum", "vpd"]),
        "temp_day": ("num", 5, 40), "temp_night": ("num", 5, 40), "temp_hyst": ("num", 0.2, 5),
        "hum_day": ("num", 10, 95), "hum_night": ("num", 10, 95), "hum_hyst": ("num", 1, 20),
        "vpd_day": ("num", 0.2, 2.5), "vpd_night": ("num", 0.2, 2.5), "vpd_hyst": ("num", 0.05, 0.5),
        "leaf_offset": ("num", -5, 0), "fan_min_per_hour": ("int", 0, 60),
    },
    "light": {"enabled": ("bool",), "on": ("time",), "off": ("time",)},
    "irrigation": {
        "enabled": ("bool",), "interval_min": ("int", 1, 10080), "duration_s": ("int", 1, 600),
        "from_hour": ("int", 0, 24), "to_hour": ("int", 0, 24),
    },
    "alarms": {
        "enabled": ("bool",), "temp_min": ("num", 0, 40), "temp_max": ("num", 10, 50),
        "hum_min": ("num", 0, 90), "hum_max": ("num", 20, 100), "delay_min": ("int", 0, 120),
    },
    "exhaust": {
        "enabled": ("bool",), "min_w": ("num", 0, 1000), "max_w": ("num", 0, 1000), "max_w_night": ("num", 0, 1000),
        "temp_band": ("num", 0.5, 10), "hum_band": ("num", 1, 30), "deadband_deg": ("int", 1, 20),
    },
    "grow": {"stage": ("str", 40), "start_date": ("date",)},  # stage = Zyklus-ID (eingebaut oder eigen), "" = keiner
}

DEFAULTS = {
    "climate": {"control": "temp_hum", "temp_day": 25, "temp_night": 20, "temp_hyst": 1.0,
                "hum_day": 60, "hum_night": 55, "hum_hyst": 5, "vpd_day": 1.0, "vpd_night": 0.9,
                "vpd_hyst": 0.1, "leaf_offset": -2.0, "fan_min_per_hour": 5},
    "light": {"enabled": True, "on": "06:00", "off": "00:00"},
    "irrigation": {"enabled": True, "interval_min": 720, "duration_s": 30, "from_hour": 6, "to_hour": 22},
    "alarms": {"enabled": True, "temp_min": 15, "temp_max": 32, "hum_min": 30, "hum_max": 80, "delay_min": 10},
    "exhaust": {"enabled": True, "min_w": 22, "max_w": 85, "max_w_night": 60, "temp_band": 3, "hum_band": 10, "deadband_deg": 3},
    "grow": {"stage": "", "start_date": ""},
}

DEFAULT_CAL = [[0, 20], [90, 25], [180, 85]]  # Abluft-Servo: [Winkel, Watt] – Startwerte, per Lernpunkt verfeinern


def validate_cal(points):
    """Kennlinie prüfen und sortieren. -> (punkte, fehler)"""
    if not isinstance(points, list) or not 2 <= len(points) <= 20:
        return None, "2 bis 20 Lernpunkte erforderlich"
    out = {}
    for p in points:
        if (not isinstance(p, list) or len(p) != 2 or _check(("int", 0, 180), p[0]) or _check(("num", 0, 1000), p[1])):
            return None, "Lernpunkt = [Winkel 0…180, Watt 0…1000]"
        out[int(p[0])] = float(p[1])
    pts = sorted([a, w] for a, w in out.items())
    if len(pts) < 2:
        return None, "mindestens 2 verschiedene Winkel"
    if any(w1 < w0 for (_, w0), (_, w1) in zip(pts, pts[1:])):
        return None, "Die Leistung muss mit steigendem Winkel gleich bleiben oder steigen"
    return pts, None

# Voreinstellungen je Wachstumsphase (nur Richtwerte – an die eigene Pflanze anpassen)
PRESETS = {
    "seedling": {"color": "#4db8ff", "label": "Keimling", "climate": {"temp_day": 25, "temp_night": 22, "hum_day": 70, "hum_night": 70, "vpd_day": 0.6, "vpd_night": 0.6},
                 "light": {"on": "06:00", "off": "00:00"}},
    "veg": {"color": "#3ddc97", "label": "Wachstum", "climate": {"temp_day": 26, "temp_night": 22, "hum_day": 62, "hum_night": 60, "vpd_day": 1.0, "vpd_night": 0.9},
            "light": {"on": "06:00", "off": "00:00"}},
    "flower": {"color": "#b794ff", "label": "Blüte", "climate": {"temp_day": 25, "temp_night": 21, "hum_day": 52, "hum_night": 50, "vpd_day": 1.2, "vpd_night": 1.0},
               "light": {"on": "08:00", "off": "20:00"}},
    "late": {"color": "#ff8a5c", "label": "Spätblüte", "climate": {"temp_day": 23, "temp_night": 19, "hum_day": 45, "hum_night": 45, "vpd_day": 1.4, "vpd_night": 1.2},
             "light": {"on": "08:00", "off": "20:00"}},
    "dry": {"color": "#ffb547", "label": "Trocknung", "climate": {"temp_day": 19, "temp_night": 18, "hum_day": 58, "hum_night": 58},
            "light": {"enabled": False}},
}

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _check(spec, v):
    kind = spec[0]
    if kind in ("num", "int"):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "Zahl erwartet"
        if kind == "int" and int(v) != v:
            return "ganze Zahl erwartet"
        if not spec[1] <= v <= spec[2]:
            return f"erlaubt: {spec[1]}…{spec[2]}"
    elif kind == "bool":
        return None if isinstance(v, bool) else "true/false erwartet"
    elif kind == "enum":
        return None if v in spec[1] else "ungültiger Wert"
    elif kind == "str":
        return None if isinstance(v, str) and len(v) <= spec[1] else "Text zu lang"
    elif kind == "time":
        return None if isinstance(v, str) and _TIME.match(v) else "Format HH:MM"
    elif kind == "date":
        if v == "":
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except (TypeError, ValueError):
            return "Format JJJJ-MM-TT"
    return None


def validate(patch):
    """patch: {abschnitt: {schlüssel: wert}} -> (bereinigt, fehler{pfad: text})"""
    clean, errors = {}, {}
    if not isinstance(patch, dict):
        return {}, {"": "Objekt erwartet"}
    for sec, vals in patch.items():
        if sec not in SPEC or not isinstance(vals, dict):
            errors[sec] = "unbekannter Abschnitt"
            continue
        for k, v in vals.items():
            if k not in SPEC[sec]:
                errors[f"{sec}.{k}"] = "unbekannte Einstellung"
                continue
            e = _check(SPEC[sec][k], v)
            if e:
                errors[f"{sec}.{k}"] = e
            else:
                clean.setdefault(sec, {})[k] = v
    return clean, errors


def cross_check(cfg):
    """Abhängigkeiten zwischen Werten (nach dem Zusammenführen)."""
    errors = {}
    if cfg["alarms"]["temp_min"] >= cfg["alarms"]["temp_max"]:
        errors["alarms.temp_min"] = "muss kleiner als Maximum sein"
    if cfg["alarms"]["hum_min"] >= cfg["alarms"]["hum_max"]:
        errors["alarms.hum_min"] = "muss kleiner als Maximum sein"
    if cfg["exhaust"]["min_w"] > min(cfg["exhaust"]["max_w"], cfg["exhaust"]["max_w_night"]):
        errors["exhaust.min_w"] = "Grundlast muss unter den Maxima liegen"
    if cfg["irrigation"]["from_hour"] >= cfg["irrigation"]["to_hour"]:
        errors["irrigation.from_hour"] = "Start muss vor Ende liegen"
    return errors


def merge(base, patch):
    out = {s: dict(v) for s, v in base.items()}
    for sec, vals in patch.items():
        out.setdefault(sec, {}).update(vals)
    return out


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_PRESET_KEYS = {"id", "label", "color", "notes", "days", "next", "climate", "light", "irrigation"}


def validate_preset(body, ids, own_id=None):
    """Prüft einen eigenen Zyklus. ids = alle existierenden Zyklus-IDs (für 'next')."""
    if not isinstance(body, dict):
        return None, {"": "Objekt erwartet"}
    errors, out = {}, {}
    for k in body:
        if k not in _PRESET_KEYS:
            errors[k] = "unbekanntes Feld"
    label = body.get("label")
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40:
        errors["label"] = "Name erforderlich (max. 40 Zeichen)"
    else:
        out["label"] = label.strip()
    color = body.get("color", PALETTE[0])
    if not (isinstance(color, str) and _HEX.match(color)):
        errors["color"] = "Farbe als #rrggbb"
    else:
        out["color"] = color.lower()
    notes = body.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 300:
        errors["notes"] = "max. 300 Zeichen"
    else:
        out["notes"] = notes
    days = body.get("days", 0)
    if _check(("int", 0, 365), days):
        errors["days"] = "ganze Zahl 0…365"
    else:
        out["days"] = int(days)
    nxt = body.get("next", "")
    if nxt and (nxt not in ids or nxt == own_id):
        errors["next"] = "unbekannter Folge-Zyklus"
    elif out.get("days", 0) == 0 and nxt:
        errors["next"] = "Folge-Zyklus braucht eine Dauer"
    else:
        out["next"] = nxt or ""
    for sec in ("climate", "light", "irrigation"):
        if sec in body:
            c, e = validate({sec: body[sec]})
            errors.update(e)
            if sec in c:
                out[sec] = c[sec]
    irr = out.get("irrigation", {})
    if "from_hour" in irr and "to_hour" in irr and irr["from_hour"] >= irr["to_hour"]:
        errors["irrigation.from_hour"] = "Start muss vor Ende liegen"
    return (None, errors) if errors else (out, {})
