"""Geräte-Editor und Sicherung: liest/schreibt config.json (die DATEI mit ${ENV}-Platzhaltern, nie die im Speicher
eingesetzten Geheimnisse), settings.json und state.json. Änderungen an Geräten werden erst nach einem Neustart aktiv."""
import json
import os
import re
import shutil
import time
from datetime import datetime

MASK = "••••"
SECRET_KEYS = ("key", "local_key", "password", "token", "net_key", "app_key", "secret")
NAME = re.compile(r"^[a-z0-9_]{1,40}$")

# Vorlagen je Typ (Startwerte beim Anlegen); neue Ausgänge starten als "manual", damit die Regelung sie nicht ungefragt schaltet
OUTPUT_TYPES = {
    "gpio": {"pin": 17, "active_low": True, "role": "manual"},
    "meross_local": {"ip": "192.168.178.", "key": "${MEROSS_KEY}", "channel": 0, "role": "manual"},
    "meross": {"device": "", "channel": 0, "role": "manual"},
    "tuya": {"dev_id": "", "local_key": "${TUYA_LOCAL_KEY}", "address": "Auto", "version": 3.4, "dp": 1, "role": "manual"},
    "mqtt_servo": {"topic": "servo/xyz/set", "angle_on": 90, "angle_off": 0, "payload": "{angle}", "role": "manual"},
    "spiderfarmer": {"mac": "", "uid": "", "outlet": 1, "role": "manual"},
    "sanlight": {"address": "0x0002", "max": 100, "role": "manual"},
}
SENSOR_TYPES = {
    "govee": {"mac": ""},
    "spiderfarmer": {"mac": "", "uid": "", "prefix": "ggs_", "label": "GGS"},
    "meross_power": {"ip": "192.168.178.", "key": "${MEROSS_KEY}", "prefix": "exhaust_"},
    "dht22": {"pin": 4}, "bme280": {"address": 118}, "ds18b20": {"device_id": ""}, "sim": {},
}
# Pflichtfelder je Typ (ohne sie startet das Gerät nicht)
REQUIRED = {"gpio": ("pin",), "meross_local": ("ip", "key"), "tuya": ("dev_id", "local_key"), "mqtt_servo": ("topic",),
            "spiderfarmer": ("mac",), "sanlight": ("address",), "govee": ("mac",), "meross_power": ("ip", "key"), "dht22": ("pin",)}
ROLES = ("light", "fan", "heater", "humidifier", "dehumidifier", "pump", "vent", "intake", "manual", "timer")


def _is_secret(k, v):
    return k in SECRET_KEYS and isinstance(v, str) and v and not v.startswith("${")


def masked(section):
    """Geheimnisse im Klartext durch MASK ersetzen (Platzhalter wie ${X} bleiben sichtbar)."""
    return {n: {k: (MASK if _is_secret(k, v) else v) for k, v in c.items()} for n, c in section.items()}


def unmask(new, old):
    """MASK wieder durch den bisherigen Wert ersetzen (nur wenn das Gerät/Feld schon existierte)."""
    out = {}
    for n, c in new.items():
        prev = old.get(n, {})
        out[n] = {k: (prev.get(k) if v == MASK else v) for k, v in c.items() if not (v == MASK and k not in prev)}
    return out


def validate(outputs, sensors):
    """-> Fehlertext oder None. Bewusst tolerant: geprüft werden Name, Typ und die wenigen Pflichtfelder."""
    for kind, sec, types, tkey in (("Ausgang", outputs, OUTPUT_TYPES, "type"), ("Sensor", sensors, SENSOR_TYPES, "driver")):
        if not isinstance(sec, dict):
            return f"{kind}e: Objekt erwartet"
        for n, c in sec.items():
            if not NAME.match(n):
                return f"{kind} „{n}“: Name nur aus a–z, 0–9 und _ (max. 40 Zeichen)"
            if not isinstance(c, dict):
                return f"{kind} {n}: Objekt erwartet"
            t = c.get(tkey, "gpio" if kind == "Ausgang" else None)
            if t not in types:
                return f"{kind} {n}: unbekannter Typ „{t}“"
            missing = [k for k in REQUIRED.get(t, ()) if c.get(k) in (None, "")]
            if missing:
                return f"{kind} {n}: Feld fehlt: {', '.join(missing)}"
            if kind == "Ausgang" and c.get("role") is not None and not isinstance(c.get("role"), str):
                return f"Ausgang {n}: Rolle muss Text sein"
    if set(outputs) & set(sensors):
        return "Ein Name ist gleichzeitig Ausgang und Sensor"
    return None


def _backup_file(path, tag):
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak-{tag}-{datetime.now():%Y%m%d-%H%M%S}")


def read_devices(config_path):
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    return {"outputs": masked(cfg.get("outputs", {})), "sensors": masked(cfg.get("sensors", {})),
            "types": {"outputs": OUTPUT_TYPES, "sensors": SENSOR_TYPES}, "roles": ROLES, "mask": MASK}


def write_devices(config_path, body):
    """Geräte in config.json übernehmen (mit Sicherungskopie). -> Fehlertext oder None"""
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    outputs = unmask(body.get("outputs") or {}, cfg.get("outputs", {}))
    sensors = unmask(body.get("sensors") or {}, cfg.get("sensors", {}))
    err = validate(outputs, sensors)
    if err:
        return err
    _backup_file(config_path, "ui")
    cfg["outputs"], cfg["sensors"] = outputs, sensors
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config_path)
    return None


def make_backup(config_path, data_dir):
    """Sicherung: config.json (wie die Datei, Geheimnisse bleiben Platzhalter aus .env), settings.json, state.json."""
    def load(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    return {"growcontroller_backup": 1, "created": datetime.now().isoformat(timespec="seconds"),
            "config": load(config_path), "settings": load(os.path.join(data_dir, "settings.json")),
            "state": load(os.path.join(data_dir, "state.json")),
            "hinweis": "Geheimnisse aus .env (z. B. ${MEROSS_KEY}) sind nicht enthalten – .env separat sichern."}


def restore_backup(config_path, data_dir, b):
    """Sicherung zurückspielen (vorhandene Dateien werden vorher gesichert). -> Fehlertext oder None"""
    if not isinstance(b, dict) or b.get("growcontroller_backup") != 1:
        return "Keine growcontroller-Sicherung"
    cfg = b.get("config")
    if not isinstance(cfg, dict) or not isinstance(cfg.get("outputs"), dict) or not isinstance(cfg.get("sensors"), dict):
        return "Sicherung enthält keine gültige config.json"
    err = validate(cfg["outputs"], cfg["sensors"])
    if err:
        return "Sicherung: " + err
    stamp = time.strftime("%Y%m%d-%H%M%S")
    targets = [(config_path, cfg)]
    for name in ("settings", "state"):
        if isinstance(b.get(name), dict):
            targets.append((os.path.join(data_dir, f"{name}.json"), b[name]))
    for path, data in targets:
        if os.path.exists(path):
            shutil.copy2(path, f"{path}.bak-restore-{stamp}")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    return None
