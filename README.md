# Growcontroller (Raspberry Pi 3B+)

Eigenständiger Growcontroller in Python 3 – ohne Node-RED, nur Standardbibliothek + optionale Hardware-Pakete.

- Licht-Zeitplan (auch über Mitternacht), Tag/Nacht-Sollwerte
- Temperatur/Luftfeuchte: Heizung, Lüfter, Befeuchter mit Hysterese, Mindestschaltzeiten, Lüfter-Grundzyklus
- Bewässerung per Timer (Fenster, Laufzeit, Pumpen-Maximallaufzeit)
- Failsafe: veraltete Sensordaten → Heizung/Befeuchter aus; beim Beenden alle Relais aus
- Weboberfläche (`http://<pi>:8080`) mit Modus auto/on/off je Ausgang, optional Token
- CSV-Log pro Tag in `data/`, Bewässerungszeitpunkt überlebt Neustarts

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
