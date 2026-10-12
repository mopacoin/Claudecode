"""Tagesbericht: Wie lange lagen Temperatur, Luftfeuchte und VPD im Zielbereich (Soll ± Hysterese, Tag/Nacht)?"""
from datetime import datetime

from . import rules

GOOD, OK = 90, 70   # Anteil im Ziel: ab 90 % grün, ab 70 % gelb, darunter rot


def _fmt(v, nd=1):
    return f"{v:.{nd}f}".replace(".", ",")


def build(rows, climate, light, extra=None):
    """rows: Minutenwerte [{t, temp, hum, vpd, …}], climate/light: aktuelle Einstellungen.
    extra: {"labels": {prefix: Name}, "alarms": n, "irrigations": n, "dryback": x}
    -> (Titel, Text, alles_grün)"""
    extra = extra or {}
    if len(rows) < 60:
        return "Grow-Tagesbericht", "Zu wenig Messwerte für einen Bericht (weniger als 1 Stunde).", False
    vpd_mode = climate.get("control") == "vpd"
    checks = [("vpd", "VPD", "kPa", 2), ("temp", "Temperatur", "°C", 1)] + ([] if vpd_mode else [("hum", "Luftfeuchte", "%", 0)])
    lines, all_good, worst = [], True, []
    for key, name, unit, nd in checks:
        vals, inside = [], 0
        for r in rows:
            v = r.get(key)
            if v is None:
                continue
            p = "day" if rules.light_on(light, datetime.fromisoformat(r["t"])) else "night"
            target, hyst = climate[f"{key}_{p}"], climate[f"{key}_hyst"]
            vals.append(v)
            inside += abs(v - target) <= hyst
        if not vals:
            continue
        share = round(100 * inside / len(vals))
        icon = "🟢" if share >= GOOD else "🟡" if share >= OK else "🔴"
        if share < GOOD:
            all_good = False
            worst.append(name)
        lines.append(f"{icon} {name}: {share} % im Ziel · Ø {_fmt(sum(vals) / len(vals), nd)} {unit} "
                     f"({_fmt(min(vals), nd)}–{_fmt(max(vals), nd)})")
    # Boden je Controller (Mittelwerte des Tages)
    for prefix, label in (extra.get("labels") or {}).items():
        hum = [r[prefix + "soil_hum"] for r in rows if r.get(prefix + "soil_hum") is not None]
        ec = [r[prefix + "soil_ec"] for r in rows if r.get(prefix + "soil_ec") is not None]
        if hum or ec:
            parts = []
            if hum:
                parts.append(f"Feuchte Ø {_fmt(sum(hum) / len(hum))} % ({_fmt(min(hum))}–{_fmt(max(hum))})")
            if ec:
                parts.append(f"EC Ø {_fmt(sum(ec) / len(ec), 2)} mS/cm")
            lines.append(f"🌱 {label}: " + " · ".join(parts))
    if extra.get("dryback") is not None:
        lines.append(f"💧 Dryback letzte Nacht: {_fmt(extra['dryback'])} %")
    lines.append(f"⚠️ Alarme: {extra.get('alarms', 0)} · Bewässerungen: {extra.get('irrigations', 0)}")
    hours = round(len(rows) / 60)
    lines.insert(0, f"Letzte {hours} Std, Ziel = Soll ± Hysterese ({'VPD-Modus' if vpd_mode else 'Temperatur + Feuchte'})")
    title = "Grow-Tagesbericht: alles im grünen Bereich ✅" if all_good else f"Grow-Tagesbericht: {', '.join(worst)} prüfen"
    return title, "\n".join(lines), all_good
