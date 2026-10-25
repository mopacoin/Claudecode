"""Bluetooth-Selbstheilung: Bleibt der Bluetooth-Chip des Pi hängen (Scan sieht kein einziges Gerät mehr),
wird der Bluetooth-Dienst samt Chip zurückgesetzt und per Push gemeldet.

Erkennung: Der gemeinsame Govee-Scanner empfängt normalerweise laufend Funkpakete aller Geräte in der Nähe.
Ohne Govee-Sensoren macht der Wächter alle 5 min einen kurzen eigenen Probe-Scan.
Absicherung: höchstens ein Reset je `cooldown_s`, höchstens `max_per_hour` je Stunde – danach nur noch Alarm
("Pi neu starten"), damit nie eine Reset-Schleife entsteht.

config.json (optional): "bluetooth": {"watchdog": true, "stale_s": 180, "adapter": "hci0"}
"""
import logging
import subprocess
import threading
import time

log = logging.getLogger("grow.btwatch")


def reset_bluetooth(adapter="hci0"):
    """Bluetooth-Dienst neu starten und den Chip zurücksetzen (braucht root, der Dienst läuft als root)."""
    for cmd in (["systemctl", "restart", "bluetooth"], ["hciconfig", adapter, "reset"], ["hciconfig", adapter, "up"]):
        try:
            subprocess.run(cmd, timeout=30, capture_output=True)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("%s: %s", " ".join(cmd), e)
        time.sleep(2)


def probe_scan(seconds=8):
    """Kurzer eigener Scan -> Anzahl gefundener Geräte (oder None bei Fehler)."""
    import asyncio
    from bleak import BleakScanner
    try:
        return len(asyncio.run(BleakScanner.discover(timeout=seconds)))
    except Exception as e:
        log.warning("Probe-Scan: %r", e)
        return 0


class BluetoothWatchdog:
    def __init__(self, event, activity=None, reset=reset_bluetooth, probe=probe_scan, on_reset=None,
                 stale_s=180, cooldown_s=600, max_per_hour=3, probe_every_s=300, adapter="hci0", start=True):
        """event(kind, msg): Ereignis/Alarm (geht per Push raus). activity(): monotonic des letzten
        empfangenen Funkpakets oder None (dann Probe-Scans). on_reset(): z. B. Scanner neu starten."""
        self.event, self.activity, self.reset, self.probe, self.on_reset = event, activity, reset, probe, on_reset
        self.stale_s, self.cooldown_s, self.max_per_hour = stale_s, cooldown_s, max_per_hour
        self.probe_every_s, self.adapter = probe_every_s, adapter
        self.resets = []            # Zeitpunkte (monotonic) der Resets
        self.waiting = False        # nach Reset: auf Erholung warten
        self.gave_up = False
        self._probe_t, self._probe_ok = None, None
        self.started = time.monotonic()
        if start:
            threading.Thread(target=self._loop, daemon=True, name="bt-watchdog").start()

    def _loop(self):
        while True:
            time.sleep(30)
            try:
                self.check(time.monotonic())
            except Exception:
                log.exception("Bluetooth-Wächter")

    def _last_seen(self, now):
        """monotonic des letzten Lebenszeichens."""
        if self.activity is not None:
            last = self.activity()
            return self.started if last is None else last
        if self._probe_t is None or now - self._probe_t >= self.probe_every_s:
            self._probe_t = now
            n = self.probe()
            if n:
                self._probe_ok = now
        return self._probe_ok or self.started

    def check(self, now):
        """Einmal prüfen (alle 30 s aus dem Thread; in Tests direkt aufrufbar)."""
        quiet = now - self._last_seen(now)
        if quiet < self.stale_s:
            if self.waiting:
                self.waiting, self.gave_up = False, False
                self.event("system", "Bluetooth läuft wieder – Geräte werden neu verbunden")
            return
        if self.gave_up:
            return  # aufgegeben: keine Resets mehr, bis Bluetooth wieder lebt (oder der Pi neu startet)
        self.resets = [t for t in self.resets if now - t < 3600]
        if self.resets and now - self.resets[-1] < self.cooldown_s:
            return
        if len(self.resets) >= self.max_per_hour:
            if not self.gave_up:
                self.gave_up = True
                self.event("alarm", "Bluetooth hängt weiterhin trotz automatischem Zurücksetzen – bitte den Pi neu starten")
            return
        self.resets.append(now)
        self.waiting = True
        self.event("alarm", f"Bluetooth reagierte {int(quiet // 60)} min nicht (kein Gerät mehr empfangen) – wird automatisch zurückgesetzt")
        log.warning("Bluetooth seit %d s stumm – setze %s zurück", quiet, self.adapter)
        self.reset(self.adapter)
        if self.on_reset:
            self.on_reset()

    def info(self):
        return {"resets_last_hour": len([t for t in self.resets if time.monotonic() - t < 3600]),
                "waiting": self.waiting, "gave_up": self.gave_up}
