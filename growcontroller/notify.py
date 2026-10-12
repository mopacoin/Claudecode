"""Push-Benachrichtigungen (ntfy oder Telegram) für Alarme – im Hintergrund, damit die Regelung nie wartet.

config.json:
  "notify": {
    "ntfy": {"topic": "mein-grow-xyz", "url": "https://ntfy.sh", "token": null},
    "telegram": {"token": "${TELEGRAM_TOKEN}", "chat_id": "123456789"},
    "repeat_min": 30
  }
Gleiche Meldungen gehen höchstens alle `repeat_min` Minuten raus.
"""
import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("grow.notify")


class Notifier:
    def __init__(self, cfg=None, start=True):
        cfg = cfg or {}
        self.ntfy = cfg.get("ntfy") if (cfg.get("ntfy") or {}).get("topic") else None
        tg = cfg.get("telegram") or {}
        self.telegram = tg if tg.get("token") and tg.get("chat_id") else None
        self.repeat_s = float(cfg.get("repeat_min", 30)) * 60
        self.sent, self.error, self.count = {}, None, 0
        self.q = queue.Queue()
        if start and self.configured:
            threading.Thread(target=self._run, daemon=True, name="notify").start()

    @property
    def configured(self):
        return bool(self.ntfy or self.telegram)

    def channels(self):
        return [n for n, c in (("ntfy", self.ntfy), ("telegram", self.telegram)) if c]

    def send(self, title, msg, prio="high"):
        """Nicht blockierend; doppelte Meldungen innerhalb von repeat_min werden verworfen."""
        if not self.configured:
            return False
        now = time.monotonic()
        if now - self.sent.get(msg, -1e9) < self.repeat_s:
            return False
        self.sent[msg] = now
        self.q.put((title, msg, prio))
        return True

    def send_now(self, title, msg, prio="default"):
        """Blockierend (für den Test-Knopf). Gibt Fehlertext oder None zurück."""
        if not self.configured:
            return "Keine Benachrichtigung eingerichtet (Abschnitt \"notify\" in config.json)"
        try:
            self._deliver(title, msg, prio)
            return None
        except Exception as e:
            return str(e)

    def _run(self):
        while True:
            title, msg, prio = self.q.get()
            for attempt in range(3):
                try:
                    self._deliver(title, msg, prio)
                    self.error, self.count = None, self.count + 1
                    break
                except Exception as e:
                    self.error = str(e)
                    log.warning("Benachrichtigung: %s", e)
                    time.sleep(10 * (attempt + 1))

    def _deliver(self, title, msg, prio):
        if self.ntfy:
            url = self.ntfy.get("url", "https://ntfy.sh").rstrip("/") + "/" + urllib.parse.quote(self.ntfy["topic"])
            # Kopfzeilen sind latin-1: Titel deshalb als RFC-2047-freie ASCII-Fassung, Text im Body (UTF-8)
            headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": prio, "Tags": "seedling"}
            if self.ntfy.get("token"):
                headers["Authorization"] = "Bearer " + self.ntfy["token"]
            urllib.request.urlopen(urllib.request.Request(url, msg.encode(), headers), timeout=10).read()
        if self.telegram:
            url = f"https://api.telegram.org/bot{self.telegram['token']}/sendMessage"
            body = json.dumps({"chat_id": self.telegram["chat_id"], "text": f"{title}\n{msg}"}).encode()
            urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": "application/json"}), timeout=10).read()
