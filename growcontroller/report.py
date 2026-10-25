"""Tagesbericht: Wie lange lagen Temperatur, Luftfeuchte und VPD – jeder Wert einzeln und alle gleichzeitig –
im Zielbereich (Soll ± Hysterese, Tag/Nacht), und in welche Richtung lagen sie daneben?"""
from datetime import datetime

from . import rules

GOOD, OK = 90, 70   # Anteil im Ziel: ab 90 % grün, ab 70 % gelb, darunter rot


def _fmt(v, nd=1):
    return f"{v:.{nd}f}".replace(".", ",")


def build(rows, climate, light, extra=None):
    """rows: Minutenwerte [{t, temp, hum, vpd, …}], climate/light: aktuelle Einstellungen.
    extra: {"labels": {prefix: Name}, "alarms": n, "irrigations": n, "dryback": x}
    Jeder Wert wird einzeln gegen sein Ziel (Soll ± Hysterese, Tag/Nacht) geprüft – mit Richtung der
    Abweichung –, dazu der Anteil der Zeit, in der alle geprüften Werte gleichzeitig im Ziel lagen.
    -> (Titel, Text, alles_grün)"""
    extra = extra or {}
    if len(rows) < 60:
        return "Grow-Tagesbericht", "Zu wenig Messwerte für einen Bericht (weniger als 1 Stunde).", False
    vpd_mode = climate.get("control") == "vpd"
    checks = [("temp", "Temperatur", "°C", 1, "zu kalt", "zu warm"), ("hum", "Luftfeuchte", "%", 0, "zu trocken", "zu feucht"),
              ("vpd", "VPD", "kPa", 2, "zu niedrig", "zu hoch")]
    if vpd_mode:   # im VPD-Modus folgt das Feuchte-Soll der Temperatur – nur VPD und Temperatur prüfen
        checks = [c for c in checks if c[0] != "hum"]
    stats = {c[0]: {"n": 0, "in": 0, "lo": 0, "hi": 0, "vals": [], "day": [0, 0], "night": [0, 0]} for c in checks}
    together = [0, 0]   # [alle im Ziel, Minuten mit allen Werten]
    for r in rows:
        p = "day" if rules.light_on(light, datetime.fromisoformat(r["t"])) else "night"
        ok_all, have_all = True, True
        for key, *_ in checks:
            v = r.get(key)
            if v is None:
                have_all = False
                continue
            s, target, hyst = stats[key], climate[f"{key}_{p}"], climate[f"{key}_hyst"]
            inside = abs(v - target) <= hyst
            s["n"] += 1
            s["vals"].append(v)
            s["in"] += inside
            s["lo"] += v < target - hyst
            s["hi"] += v > target + hyst
            s[p][0] += inside
            s[p][1] += 1
            ok_all &= inside
        if have_all:
            together[0] += ok_all
            together[1] += 1
    icon = lambda share: "🟢" if share >= GOOD else "🟡" if share >= OK else "🔴"
    pct = lambda a, b: round(100 * a / b) if b else 0
    lines, problems = [], []
    if together[1]:
        both = pct(*together)
        names = " + ".join(c[1] for c in checks)
        lines.append(f"{icon(both)} Alle gleichzeitig im Ziel ({names}): {both} %")
    for key, name, unit, nd, low, high in checks:
        s = stats[key]
        if not s["n"]:
            continue
        share, lo, hi = pct(s["in"], s["n"]), pct(s["lo"], s["n"]), pct(s["hi"], s["n"])
        if share < GOOD:
            problems.append(f"{name} {low if lo >= hi else high}")
        phases = " · ".join(f"{lab} {pct(*s[p])} %" for p, lab in (("day", "Tag"), ("night", "Nacht")) if s[p][1])
        vals = s["vals"]
        lines.append(f"{icon(share)} {name}: {share} % im Ziel · {lo} % {low} · {hi} % {high}\n"
                     f"      {phases} · Ø {_fmt(sum(vals) / len(vals), nd)} {unit} ({_fmt(min(vals), nd)}–{_fmt(max(vals), nd)})")
    if vpd_mode:
        hum = [r["hum"] for r in rows if r.get("hum") is not None]
        if hum:
            lines.append(f"ℹ️ Luftfeuchte (folgt im VPD-Modus der Temperatur): Ø {_fmt(sum(hum) / len(hum), 0)} % "
                         f"({_fmt(min(hum), 0)}–{_fmt(max(hum), 0)})")
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
    all_good = not problems and (not together[1] or pct(*together) >= GOOD)
    if all_good:
        title = "Grow-Tagesbericht: alles im grünen Bereich ✅"
    elif problems:
        title = "Grow-Tagesbericht: " + ", ".join(problems)
    else:
        title = f"Grow-Tagesbericht: nur {pct(*together)} % gleichzeitig im Ziel"
    return title, "\n".join(lines), all_good


def shares(rows, climate, light):
    """Anteile im Ziel je Wert (für die Statistik-Seite): {key: {"in", "lo", "hi", "n"} in %, "all": %, "n": Minuten}.
    Gleiche Regeln wie der Tagesbericht: Soll ± Hysterese je Tag/Nacht, im VPD-Modus ohne Feuchte."""
    keys = ["temp", "vpd"] if climate.get("control") == "vpd" else ["temp", "hum", "vpd"]
    st = {k: [0, 0, 0, 0] for k in keys}   # in, lo, hi, n
    together = [0, 0]
    for r in rows:
        p = "day" if rules.light_on(light, datetime.fromisoformat(r["t"])) else "night"
        ok_all, have_all = True, True
        for k in keys:
            v = r.get(k)
            if v is None:
                have_all = False
                continue
            target, hyst = climate[f"{k}_{p}"], climate[f"{k}_hyst"]
            s = st[k]
            s[3] += 1
            if v < target - hyst:
                s[1] += 1
                ok_all = False
            elif v > target + hyst:
                s[2] += 1
                ok_all = False
            else:
                s[0] += 1
        if have_all:
            together[0] += ok_all
            together[1] += 1
    pct = lambda a, b: round(100 * a / b) if b else None
    out = {k: {"in": pct(s[0], s[3]), "lo": pct(s[1], s[3]), "hi": pct(s[2], s[3]), "n": s[3]} for k, s in st.items()}
    out["all"] = pct(*together)
    out["n"] = len(rows)
    return out
