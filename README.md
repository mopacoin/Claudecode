# Growcontroller (Raspberry Pi 3B+)

Eigenständiger Growcontroller in Python 3 – ohne Node-RED, nur Standardbibliothek + optionale Hardware-Pakete.

- Licht-Zeitplan (auch über Mitternacht), Tag/Nacht-Sollwerte
- Temperatur/Luftfeuchte: Heizung, Lüfter, Befeuchter mit Hysterese, Mindestschaltzeiten, Lüfter-Grundzyklus
- Bewässerung per Timer (Fenster, Laufzeit, Pumpen-Maximallaufzeit)
- Failsafe: veraltete Sensordaten → Heizung/Befeuchter aus; beim Beenden alle Relais aus
- Dashboard (`http://<pi>:8080`), siehe unten
- CSV-Log pro Tag in `data/`, Bewässerungszeitpunkt überlebt Neustarts

## Dashboard
Läuft im Controller selbst (keine externen Bibliotheken, funktioniert offline), responsiv für Handy und Desktop.

- **Übersicht:** Temperatur, Luftfeuchte und VPD mit Soll, Ampelfarbe und 24-h-Min/Max/Ø; VPD-Bereichsanzeige;
  Verlaufsdiagramm (1 Std / 6 Std / 24 Std / 7 Tage, Hover-Tooltip) und Schaltzeiten-Zeitleiste aller Geräte
- **Geräte:** je Gerät Auto/Ein/Aus (bleibt nach Neustart erhalten), Erreichbarkeit, „Jetzt gießen“
- **Klima:** Regelmodus Temperatur+Feuchte **oder VPD** (Ziel-VPD wird bei aktueller Temperatur in eine Feuchte-Vorgabe
  umgerechnet, mit Blatt-Temperaturversatz), Tag/Nacht-Sollwerte, Hysteresen, Lüfter-Grundlauf
- **Abluft stufenlos (Servo):** Grundlast bis Maximum (Tag/Nacht getrennt), gleitend nach Temperatur-/Feuchte-Abweichung.
  Die Watt werden über eine **Kennlinie aus Lernpunkten** (Winkel → gemessene Watt, linear interpoliert) in den Servo-Winkel
  umgerechnet. Kalibrieren unter Geräte → „Abluft kalibrieren“: Winkel anfahren, Leistung eintragen, speichern.
  Startwerte: 0° = 20 W, 90° = 25 W, 180° = 85 W. Mit Raum-Sensor regelt sie nur auf das per Raumluft Erreichbare
  (Zelt ≥ Raumtemperatur + Abstand; Feuchte über absolute Feuchte) und zeigt einen Hinweis, wenn das Ziel so nicht erreichbar ist.
  **Automatische Kennlinie:** Mit Leistungsmessung an der Abluft-Steckdose (Sensor `meross_power` mit `"prefix": "exhaust_"`)
  fährt „Automatisch kalibrieren“ 0–180° in 15°-Schritten ab (je 8 s einschwingen, Mittelwert aus 4 Messungen); im Betrieb lernt
  die Kennlinie nach demselben Prinzip weiter (geglättet, monoton steigend erzwungen).
  **Zuluft:** läuft mit, sobald die Abluft mindestens `intake_pct` (40 %) ihrer Leistung erreicht (Unterdruck bleibt erhalten).
  **Gerätekopplung (Interlock):** Läuft der Entfeuchter, entfeuchtet die Abluft nicht mit (Stufe 2: hilft erst, wenn er nach
  20 min nicht hinterherkommt); Befeuchter/Heizung an → Abluft auf Grundlast; Temperatur hat Vorrang; sanfte Rampe (15 W/min). Gerät „Ein“ = Maximum, „Aus“ = Grundlast.
  **Gelernte Wirksamkeit (Klima → Abluft-Wirksamkeit):** Mit Raum-Sensor lernt der Controller je Leistungsstufe (5 W), wie viel
  wärmer (°C) und feuchter (g/m³ absolut) das Zelt gegenüber dem Raum bleibt – getrennt für Tag und Nacht, nur in ruhigen
  Phasen (10 min gleiche Leistung und stabiles Klima, kein Entfeuchter/Befeuchter/Heizung an, ≥ 30 min nach Licht an/aus).
  Daraus ergibt sich die Leistung, die das Ziel bei der aktuellen Raumluft hält (**Vorsteuerung**); die Abluft regelt um diesen
  Wert herum statt immer ab Grundlast – ohne bleibende Abweichung. Ist das Ziel laut Lernkurve nicht erreichbar oder zu wenig
  gelernt, bleibt es beim bisherigen Regler. Abschaltbar (`learn_effect`, `use_effect`), Lernstand in `data/settings.json`.
- **Klima-Logik (Übersicht):** Entfeuchter, Heizung und Befeuchter schalten nicht an festen Schwellen, sondern nach dem Wert
  in `lookahead_min` (10) Minuten, berechnet aus dem gemessenen Trend (Ausgleichsgerade über 10 min, Feuchte als absolute Feuchte).
  Gleiches Schaltband wie bisher, aber vorausschauend: steigt die Feuchte schnell, geht der Entfeuchter früher an; fällt sie schon,
  bleibt er aus; er stoppt, sobald die Prognose das Ziel erreicht. Ist die Raumluft trocken genug, wird abgewogen, ob die Abluft
  günstiger entfeuchtet (Zusatz-Watt laut gelernter Abluft-Wirksamkeit gegen `power_w` des Entfeuchters, Standard 250 W);
  die Temperatur zählt mit (zu warm → Abluft, die kühlt; zu kalt → Entfeuchter, dessen Abwärme hilft). Schafft die Abluft es
  nach `dehum_wait_min` (15) nicht, übernimmt der Entfeuchter; weit über Ziel (Soll + 2 × Hysterese) läuft er immer.
  **Wirkung je Gerät** wird gelernt: Nach jedem Einschalten vergleicht der Controller den Trend der 10 min davor mit dem Trend
  ab Minute 3 (nur wenn kein anderes Gerät und nicht das Licht geschaltet hat) → °C/h und g/m³/h je Gerät. Zeigt der Entfeuchter
  kaum Wirkung, steht „Tank voll?“ in der Begründung. Jede Entscheidung wird im Klartext angezeigt. `logic: "static"` = alte
  feste Schwellen. Leistungsaufnahme je Ausgang optional als `"power_w"` in `config.json`.
- **Licht:** Zeitplan inkl. Schnellwahl 18/6, 20/4, 16/8, 12/12 · **Bewässerung:** Intervall, Dauer, Zeitfenster
- **Alarme:** Grenzwerte mit Verzögerung; Sensor-/Geräteausfall; aktive Alarme im Kopf der Seite
- **Zyklen:** Wachstumsphasen mit Tageszähler und Fortschrittsbalken. Zu den eingebauten Richtwerten (Keimling, Wachstum, Blüte,
  Spätblüte, Trocknung) lassen sich **eigene Zyklen erstellen, benennen, einfärben, bearbeiten, duplizieren und löschen**.
  Ein Zyklus enthält alle Klima-Werte (inkl. VPD), den Lichtplan und die Bewässerung, Notizen, optional eine **Dauer in Tagen**
  und einen **Folge-Zyklus** – dann wechselt der Controller nach Ablauf automatisch (Ereignisprotokoll). Eingebaute Zyklen sind
  schreibgeschützt (duplizieren und anpassen). Eigene Zyklen stehen in `data/settings.json` (max. 30).
- **System:** Ereignisprotokoll (Alarme, Schaltvorgänge, Einstellungen), Hardware-Übersicht, CSV-Export, Token

Einstellungen werden serverseitig geprüft (Grenzen stehen in `settings.py`), sofort übernommen und in `data/settings.json`
gespeichert; sie überschreiben die Werte aus `config.json`. Geräte/Pins/Zugangsdaten (`outputs`, `sensors`, `mqtt`)
bleiben bewusst in `config.json`. Verlauf: 1 Wert/min in `data/log-*.csv`, die letzten 7 Tage werden beim Start geladen.
Ohne `web.token` kann jeder im Netz Einstellungen ändern – bitte ein Token setzen (Header `X-Token`, die Oberfläche fragt danach).
Schreibzugriffe brauchen `Content-Type: application/json` und werden von fremden Webseiten abgewiesen (Schutz gegen CSRF).
Verlaufsdateien werden nach `history_days` (400) Tagen gelöscht, das Ereignisprotokoll bleibt unter ~1 MB.

## Sensoren (`config.json` → `sensors`)
| driver | Sensor | Felder |
|---|---|---|
| `govee` | Govee-Thermo-Hygrometer per Bluetooth (H5072/74/75, H5100–05, H5174/77, H5179), Paket `bleak` | `mac` |
| `bme280` / `dht22` / `ds18b20` | kabelgebunden | siehe `sensors.py` |
| `meross_power` | Leistungsmessung einer Meross-Steckdose (MSS305) im LAN, nur lesend | `ip`, `key`, `poll_s` |
| `sim` | simuliert (Test) | – |

`"prefix": "room_"` macht einen Sensor zum reinen Anzeige-Sensor (Raumklima), er regelt nicht.
`"temp_offset"` / `"hum_offset"` gleichen Sensor-Abweichungen aus (z. B. `-0.4` °C, `+2` %).
Werte, die länger als `sensor_timeout_s` (120 s) nicht aktualisiert wurden, verschwinden aus Anzeige und Regelung;
fällt ein Zusatz-Sensor 5 min aus, gibt es einen Alarm.
Govee-Sensoren testen: `.venv/bin/python -m growcontroller.govee_scan <MAC> …` zeigt Rohdaten und erkannte Werte.

## Ausgangs-Typen (`config.json` → `outputs`)
Jeder Ausgang hat `type` und optional `role` (Standard = Name). Rollen: `light`, `fan`, `heater`, `humidifier`,
`dehumidifier`, `pump`, `vent` und `intake` (Abluft-Klappe/Zuluft, folgen dem Lüfterbedarf). Mehrere Ausgänge mit derselben Rolle schalten gemeinsam.

| type | Zweck | wichtige Felder |
|---|---|---|
| `gpio` | Relais am Pi | `pin`, `active_low` |
| `meross_local` | Meross-Steckdose im LAN, nur `ip` + `key` (keine Zusatzpakete) | `ip`, `key`, `channel` |
| `meross` | Meross-Steckdose über die Cloud (`meross-iot`) | `device` (Name oder UUID), `channel`; Zugang im Block `meross` |
| `tuya` | Tuya-Entfeuchter lokal (`tinytuya`) | `dev_id`, `local_key`, `address` (`Auto`), `version` (`auto`), `dp`, `value_on`, `value_off` |
| `mqtt_servo` | Servo/Klappe per MQTT | `topic`, `angle_on`, `angle_off`, `payload` (`"{angle}"` oder JSON-Vorlage); Broker im Block `mqtt` |

Geheimnisse: `.env.example` nach `.env` kopieren (nie committen). Zugangsdaten als `${MEROSS_PASSWORD}`, `${TUYA_LOCAL_KEY}` usw. schreiben und als Umgebungsvariablen setzen
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

Autostart: `sudo sh deploy/install-service.sh` im Projektordner – richtet den Dienst für diesen Ordner und Benutzer ein
(startet nach Netzwerk/Zeitabgleich) und zeigt die Zeitzone an, die für die Lichtzeiten stimmen muss.

API: `GET /api/status|history?range=1h|6h|24h|7d|settings|events|export.csv`, `POST /api/settings` (`{"climate":{"temp_day":25}}`), `POST /api/output/<name>` (`{"mode":"auto|on|off"}`), `POST /api/presets` (anlegen/ändern), `POST /api/presets/<id>/apply`, `DELETE /api/presets/<id>`, `POST /api/calibration` (`{"points":[[0,20],[180,85]]}`), `POST /api/servo/<name>/angle` (`{"angle":90}`, Test), `POST /api/irrigation/run` (Schreibzugriffe mit Header `X-Token`, falls gesetzt).

**Sicherheit:** Netzspannung nur mit geeigneten Relais/Absicherung (FI, Sicherung, Übertemperaturschutz in Hardware) – Software ersetzt keinen Hardware-Schutz.
