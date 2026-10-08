"""Prüft einen Tuya-Ausgang aus config.json, nur lesend (schaltet nichts).

    set -a; . ./.env; set +a
    python -m growcontroller.tuya_check                # Ausgang "dehumidifier"
    python -m growcontroller.tuya_check <ausgang> [config.json]
"""
import json
import sys

from .__main__ import expand


def main(name="dehumidifier", cfg_path="config.json"):
    import tinytuya
    cfg = expand(json.load(open(cfg_path)))
    o = cfg["outputs"].get(name)
    if not o or o.get("type") != "tuya":
        raise SystemExit(f"Kein Tuya-Ausgang '{name}' in {cfg_path}")
    if not o.get("local_key") or o["local_key"].startswith("$"):
        raise SystemExit("local_key fehlt – .env geladen? (set -a; . ./.env; set +a)")
    addr = o.get("address", "Auto")
    versions = [3.3, 3.4, 3.5, 3.1] if str(o.get("version", "auto")) == "auto" else [float(o["version"])]
    print(f"Gerät {o['dev_id']} @ {addr}")
    for v in versions:
        d = tinytuya.Device(o["dev_id"], addr, o["local_key"], version=v)
        d.set_socketTimeout(5)
        st = d.status()
        if isinstance(st, dict) and "dps" in st:
            print(f"OK mit Protokoll {v}. Datenpunkte: {json.dumps(st['dps'], ensure_ascii=False)}")
            dp = str(o.get("dp", 1))
            print(f"DP {dp} (geschaltet vom Controller) steht auf: {st['dps'].get(dp)!r}"
                  f"  – AN = {o.get('value_on', True)!r}, AUS = {o.get('value_off', False)!r}")
            if str(o.get("version", "auto")) == "auto":
                print(f'Tipp: in config.json "version": {v} eintragen.')
            return
        print(f"Protokoll {v}: {st.get('Error') if isinstance(st, dict) else st}")
    raise SystemExit("Keine Verbindung. IP/local_key richtig? Hält Node-RED noch eine Tuya-Verbindung?")


if __name__ == "__main__":
    main(*sys.argv[1:])
