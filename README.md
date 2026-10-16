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
| `spiderfarmer` | Spider Farmer GGS-Controller per Bluetooth (lokal, ohne App/Cloud): `temp`, `hum`, `vpd`, Boden `soil_temp`/`soil_hum`/`soil_ec` (Mittelwert) und je Sonde `soil1_…` | `mac` (Bluetooth), `uid` (Benutzer-ID aus der App, optional) |
| `sim` | simuliert (Test) | – |

`"prefix": "room_"` macht einen Sensor zum reinen Anzeige-Sensor (Raumklima), er regelt nicht.
`"temp_offset"` / `"hum_offset"` gleichen Sensor-Abweichungen aus (z. B. `-0.4` °C, `+2` %).
`"label"` ist der Anzeige-Name im Dashboard (z. B. `"GGS 1"` auf der Substrat-Karte; Standard: Name des Sensors).
`"fallback": true` (bei einem Zusatz-Sensor mit `prefix`): Liefert der Haupt-Sensor 90 s lang nichts, regelt dieser Sensor
Temperatur/Feuchte weiter (Alarm „Ersatzsensor regelt“), bis der Haupt-Sensor zurück ist. Nur für einen Sensor im selben Zelt!
Bodenwerte (`…soil…`) stehen mit im CSV-Log und lassen sich im Verlauf als Kurven einblenden (Bodenfeuchte, EC je Controller).
Werte, die länger als `sensor_timeout_s` (120 s) nicht aktualisiert wurden, verschwinden aus Anzeige und Regelung;
fällt ein Zusatz-Sensor 5 min aus, gibt es einen Alarm.
Govee-Sensoren testen: `.venv/bin/python -m growcontroller.govee_scan <MAC> …` zeigt Rohdaten und erkannte Werte.

## Ausgangs-Typen (`config.json` → `outputs`)
Jeder Ausgang hat `type` und optional `role` (Standard = Name). Rollen: `light`, `fan`, `heater`, `humidifier`,
`dehumidifier`, `pump`, `vent` und `intake` (Abluft-Klappe/Zuluft, folgen dem Lüfterbedarf). Mehrere Ausgänge mit derselben Rolle schalten gemeinsam.
Rolle `manual`: keine Automatik, nur Ein/Aus von Hand im Dashboard; „Frei“ lässt das Gerät in Ruhe (auch beim Start und Beenden des
Dienstes), angezeigt wird der vom Gerät gemeldete Zustand.

| type | Zweck | wichtige Felder |
|---|---|---|
| `gpio` | Relais am Pi | `pin`, `active_low` |
| `meross_local` | Meross-Steckdose im LAN, nur `ip` + `key` (keine Zusatzpakete) | `ip`, `key`, `channel` |
| `meross` | Meross-Steckdose über die Cloud (`meross-iot`) | `device` (Name oder UUID), `channel`; Zugang im Block `meross` |
| `tuya` | Tuya-Entfeuchter lokal (`tinytuya`) | `dev_id`, `local_key`, `address` (`Auto`), `version` (`auto`), `dp`, `value_on`, `value_off` |
| `mqtt_servo` | Servo/Klappe per MQTT | `topic`, `angle_on`, `angle_off`, `payload` (`"{angle}"` oder JSON-Vorlage); Broker im Block `mqtt` |
| `spiderfarmer` | Steckdose O1–O5 eines Spider Farmer GGS-Controllers per Bluetooth | `mac`, `outlet` (1–5), `uid` |
| `sanlight` | SANlight-LED-Dimmer per Bluetooth Mesh, dimmbar (Rolle `light`) | `address` (Mesh-Adresse), `max` (%); Keys im Block `sanlight` |

**Spider Farmer GGS:** Sensor und Steckdosen teilen sich eine dauerhafte Bluetooth-Verbindung (`growcontroller/spiderfarmer.py`,
Protokoll dort beschrieben; Paket `cryptography`). Sie geht direkt über einen L2CAP-Socket, weil die BlueZ-Dienstsuche bei diesem
Gerät hängen bleibt – der Dienst muss dafür als root laufen. Solange der Pi verbunden ist, kann sich die Spider-Farmer-App nicht per
Bluetooth verbinden. Ein Ausgang vom Typ `spiderfarmer` schreibt nur, wenn der gemeldete Zustand abweicht; je nach Leistenmodus
(`psmode`) schaltet er direkt (`setOutlet`, psmode 0) oder stellt die Steckdose in der Konfiguration auf **manuell** (psmode 1 –
die Automatik des GGS für diese Steckdose ist dann aus). Ein Konto ist nicht nötig, auch ein GGS ohne Sensoren funktioniert.
**Modi je Steckdose** (Geräte → „Modus …“): **Manuell** (Ein/Aus), **Zeitfenster** (bis 5 Fenster), **Zyklus** (Start, an/aus,
Wiederholungen), **Tropfbewässerung** (Zeitraum, Pumpstoß/Pause, gießt unter „trocken“ bis „Ziel“ %, Bodensonde oder Mittelwert) –
diese regelt der GGS selbst – oder **Growcontroller** (Rolle wählen, z. B. Befeuchter; dann schaltet die Regel-Logik). Achtung:
`setConfigField` ersetzt die ganze Konfiguration einer Steckdose, deshalb liest growcontroller sie und schreibt sie zusammengeführt
zurück. Zeitfenster/Zyklus/Bewässerung gehen nur bei Leisten in `psmode` 1. Rolle und Modus stehen in `data/settings.json`.
API: `POST /api/ggs/<ausgang>` mit `{"mode":"manual","on":1}`, `{"mode":"time","periods":[{"start":"06:00","end":"18:00"}]}`,
`{"mode":"cycle","start":"04:00","on_s":420,"off_s":1320,"times":13}`,
`{"mode":"drip","start":"08:00","end":"20:00","on_s":95,"off_s":600,"dry":90,"target":95,"sensor":"<Sonden-ID oder leer>"}`
oder `{"mode":"grow","role":"humidifier"}`. Getestet mit Firmware 3.2 (Hardware S-ZC5-V2.5);
neuere Firmware verschlüsselt anders.
```json
"sensors": { "ggs": { "driver": "spiderfarmer", "mac": "80:F1:B2:B1:C7:F2", "uid": "123456", "prefix": "ggs_" } },
"outputs": { "ggs_o5": { "type": "spiderfarmer", "mac": "80:F1:B2:B1:C7:F2", "uid": "123456", "outlet": 5, "role": "manual" } }
```

**SANlight-LED-Dimmer (SLD, Bluetooth Mesh):** Ausgangstyp `sanlight` mit Rolle `light` – der Licht-Zeitplan schaltet
die Lampen nicht nur, er **dimmt** sie (`growcontroller/sanlight.py`, Protokoll aus der SANlight-Mesh-App 3.4.0 rekonstruiert,
geprüft mit SLD V5 / Firmware 5; Pakete `bleak`, `cryptography`). Gesendet wird die Max-Helligkeit des Dimmers wie mit dem
Schieberegler der App. Die Helligkeit stellt der Regler je Lampe in der Übersicht ein (Startwert `max` aus config.json).
Im Licht-Tab legst du **Sonnenaufgang** und **Sonnenuntergang** als Verlauf fest – je ein Graph mit ziehbaren Punkten
„nach X min → Y %“ (Sonnenuntergang: „X min vor Licht aus → Y %“), Vorlagen (Gleichmäßig, Sanft, Schnell hell, Langsam),
Dauer zum Strecken und Punkttabelle. Zwischen den Punkten wird gleichmäßig gedimmt, der letzte Punkt ist 100 %; leer = aus.
Ein Punkt bei **0 min** ist der Startwert (z. B. sofort mit Mindestleistung starten, Vorlage „Ab Mindestleistung“); die rote
Linie im Graphen zeigt, ab welchem Verlaufswert die Lampen tatsächlich angehen.
Gespeichert als `light.sunrise` / `light.sunset` = `[[Minuten, Prozent], …]` (bis 12 Punkte, 0–600 min).
Die Prozent im Verlauf beziehen sich auf den Reglerwert der Lampe (100 % = Reglerwert). **Mindestleistung:** Liegt die tatsächliche
Leistung (Verlauf × Regler) unter 20 %, geht die Lampe ganz aus statt zu glimmen; beim Sonnenaufgang geht sie erst an, wenn
der Verlauf 20 % erreicht. Je Ausgang einstellbar mit `"min_pct"` (Standard 20, 0 = aus). Die Gerätekarte zeigt die gesendete Helligkeit und
was der Dimmer meldet (Helligkeit, Dimmer-Uhr). Gerät „Ein“ = 100 %, „Aus“ = 0 %.
In der **Übersicht** hat jede Lampe einen Schieberegler (0–100 %, wie in der SANlight-App): Er setzt die Helligkeit der Lampe
bei Licht an (ersetzt `max`, bleibt nach Neustart in `data/settings.json`); nachts wird der Wert gespeichert und ab Licht an
verwendet. API: `POST /api/dim/<ausgang>` mit `{"pct": 60}`.
NetKey/AppKey stammen aus dem Mesh-Export der SANlight-App (`SANlightMesh.json`, Einstellungen → „Export mesh“). `src_address`
muss eine im Mesh **freie** Unicast-Adresse sein, die kein anderes Programm benutzt (Standard `0x0011`); die Sequenznummer liegt
in `data/sanlight_seq.json` – nicht löschen, sonst verwerfen die Dimmer die Befehle. Den IV-Index liest der Controller aus dem
Beacon der Dimmer. Hängen die Lampen am Licht-Relais, sind die Dimmer nachts stromlos – das gilt dann nicht als Störung.
Beim Beenden/Neustart des Dienstes bleiben die Lampen, wie sie sind. Die Dimmer haben keine Echtzeituhr: ihr eigener
Tageslichtzyklus zählt ab dem Einschalten – dort am besten konstant 100 % lassen, den Verlauf macht der Growcontroller.
Eine Bluetooth-Verbindung je Dimmer: Solange der Pi verbunden ist, nimmt die App ggf. einen anderen Dimmer als Proxy.
```json
"sanlight": { "net_key": "${SANLIGHT_NET_KEY}", "app_key": "${SANLIGHT_APP_KEY}", "src_address": "0x0011" },
"outputs": {
  "lampe_links":  { "type": "sanlight", "address": "0x0003", "max": 51, "role": "light" },
  "lampe_rechts": { "type": "sanlight", "address": "0x0004", "max": 51, "role": "light" }
}
```

Geheimnisse: `.env.example` nach `.env` kopieren (nie committen). Zugangsdaten als `${MEROSS_PASSWORD}`, `${TUYA_LOCAL_KEY}` usw. schreiben und als Umgebungsvariablen setzen
(beim systemd-Dienst per `EnvironmentFile=`). Tuya `local_key`/DPs: `python3 -m tinytuya wizard`.
Netzwerk-Ausgänge senden im Hintergrund mit Wiederholung (alle 10 s bei Fehler, Auffrischen alle 60 s); in der Oberfläche
zeigt ein ⚠ einen nicht erreichbaren Ausgang. Ist ein Gerät beim Start nicht verfügbar, läuft der Rest weiter.
Ist ein Entfeuchter konfiguriert, übernimmt er die Feuchte-Abfuhr; sonst lüftet der Lüfter bei zu hoher Feuchte.

## Benachrichtigungen (`config.json` → `notify`)
Alarme (und „Behoben“) gehen aufs Handy, gleiche Meldungen höchstens alle `repeat_min` (30) Minuten:
```json
"notify": { "ntfy": { "topic": "mein-grow-ein-langer-zufallsname" }, "repeat_min": 30 }
```
**ntfy:** App „ntfy“ installieren, denselben Kanal (`topic`) abonnieren – fertig (eigener Server: `"url"`, Zugriffsschutz: `"token"`).
Der Kanal ist öffentlich lesbar, wer den Namen kennt – daher einen langen Zufallsnamen nehmen.
**Telegram:** `"telegram": {"token": "${TELEGRAM_TOKEN}", "chat_id": "123456789"}` (Bot über @BotFather anlegen).
Test: System → Benachrichtigungen → „Test senden“ (`POST /api/notify/test`).
**Tagesbericht** (System → Tagesbericht, Standard 08:00): für die letzten 24 Std der Anteil der Zeit, in dem VPD, Temperatur und
Luftfeuchte im Zielbereich (Soll ± Hysterese, Tag/Nacht; im VPD-Modus ohne Feuchte) lagen – 🟢 ab 90 %, 🟡 ab 70 %, 🔴 darunter –
mit Ø und Min/Max, Bodenwerten je Controller, Dryback (bei Crop Steering), Alarmen und Bewässerungen. Sofort: `POST /api/report/send`.

## Crop Steering (Bewässerung → Crop Steering)
Gießt nach Bodenfeuchte statt nach Timer und ersetzt dann die Intervall-Bewässerung (Ausgang mit Rolle `pump`):
**P0** warten bis `p1_delay_min` nach Licht an → **P1** Schüsse (`shot_s`, Abstand `gap_min`) bis `target` % → **P2** nachgießen,
wenn die Feuchte `p2_drop` % unter das Ziel fällt → **P3** ab `stop_min` vor Licht aus kein Wasser (Dryback über Nacht).
Angezeigt werden Phase, Schüsse und der gemessene **Dryback** (Feuchte am Ende der Lichtphase minus am nächsten Morgen).
Ohne gültigen Bodenwert wird nicht gegossen; höchstens `max_shots` Schüsse pro Tag. Standardmäßig aus.

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
