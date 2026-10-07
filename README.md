# Growcontroller (Raspberry Pi 3B+)

Eigenständiger Growcontroller in Python 3 – ohne Node-RED, nur Standardbibliothek + optionale Hardware-Pakete.

- Licht-Zeitplan (auch über Mitternacht), Tag/Nacht-Sollwerte
- Temperatur/Luftfeuchte: Heizung, Lüfter, Befeuchter mit Hysterese, Mindestschaltzeiten, Lüfter-Grundzyklus
- Bewässerung per Timer (Fenster, Laufzeit, Pumpen-Maximallaufzeit)
- Failsafe: veraltete Sensordaten → Heizung/Befeuchter aus; beim Beenden alle Relais aus
- Weboberfläche (`http://<pi>:8080`) mit Modus auto/on/off je Ausgang, optional Token
- CSV-Log pro Tag in `data/`, Bewässerungszeitpunkt überlebt Neustarts

## Ausgangs-Typen (`config.json` → `outputs`)
Jeder Ausgang hat `type` und optional `role` (Standard = Name). Rollen: `light`, `fan`, `heater`, `humidifier`,
`dehumidifier`, `pump`, `vent` (folgt dem Lüfterbedarf). Mehrere Ausgänge mit derselben Rolle schalten gemeinsam.

| type | Zweck | wichtige Felder |
|---|---|---|
| `gpio` | Relais am Pi | `pin`, `active_low` |
| `meross` | Meross-Steckdose (Cloud, `meross-iot`) | `device` (Name oder UUID), `channel`; Zugang im Block `meross` |
| `tuya` | Tuya-Entfeuchter lokal (`tinytuya`) | `dev_id`, `local_key`, `address`, `version`, `dp`, `on_dps` |
| `mqtt_servo` | Servo/Klappe per MQTT | `topic`, `angle_on`, `angle_off`, `payload` (`"{angle}"` oder JSON-Vorlage); Broker im Block `mqtt` |

Zugangsdaten als `${MEROSS_PASSWORD}`, `${TUYA_LOCAL_KEY}` usw. schreiben und als Umgebungsvariablen setzen
(beim systemd-Dienst per `EnvironmentFile=`). Tuya `local_key`/DPs: `python3 -m tinytuya wizard`.
Netzwerk-Ausgänge senden im Hintergrund mit Wiederholung (alle 10 s bei Fehler, Auffrischen alle 60 s); in der Oberfläche
zeigt ein ⚠ einen nicht erreichbaren Ausgang. Ist ein Gerät beim Start nicht verfügbar, läuft der Rest weiter.
Ist ein Entfeuchter konfiguriert, übernimmt er die Feuchte-Abfuhr; sonst lüftet der Lüfter bei zu hoher Feuchte.

## Start
```
python3 -m growcontroller -c config.json      # Standard: simulierter Sensor, GPIO wird ohne RPi.GPIO simuliert
python3 -m unittest discover -s tests
```
Auf dem Pi: `pip3 install -r requirements.txt` (nach Bedarf), in `config.json` den Sensor-Treiber
(`dht22`, `bme280`, `ds18b20`) und die BCM-Pins setzen. Relaisboards sind meist `active_low: true`.

Autostart: `deploy/growcontroller.service` nach `/etc/systemd/system/` kopieren, `systemctl enable --now growcontroller`.

API: `GET /api/status`, `GET /api/history`, `POST /api/output/<name>` mit `{"mode":"auto|on|off"}` (Header `X-Token`, falls gesetzt).

**Sicherheit:** Netzspannung nur mit geeigneten Relais/Absicherung (FI, Sicherung, Übertemperaturschutz in Hardware) – Software ersetzt keinen Hardware-Schutz.
