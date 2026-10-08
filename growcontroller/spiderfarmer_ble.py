"""Spider Farmer GGS per Bluetooth (lokal, ohne Cloud) – für Geräte, die nur per Bluetooth mit der App sprechen (z. B. PS5).

Protokoll nach github.com/cr0ssn0tice/Spider-Farmer-GGS-Controller-MQTT: JSON-Befehl auf FF02 schreiben,
Antwort als Benachrichtigung auf FF01 (kann in Stücken und mit Bytes vor dem JSON kommen).

Test auf dem Pi (App am Handy vorher schließen – es geht nur eine Bluetooth-Verbindung gleichzeitig):
    python -m growcontroller.spiderfarmer_ble              # sucht "SF-GGS-…"
    python -m growcontroller.spiderfarmer_ble AA:BB:CC:DD:EE:FF
"""
import asyncio
import json
import sys
import time

from .spiderfarmer import parse_status

NOTIFY = "0000ff01-0000-1000-8000-00805f9b34fb"
WRITE = "0000ff02-0000-1000-8000-00805f9b34fb"


class JsonAssembler:
    """Setzt JSON-Objekte aus Bluetooth-Fragmenten zusammen (Klammerzählung, Bytes davor werden verworfen)."""

    def __init__(self, limit=16384):
        self.buf, self.limit = "", limit

    def feed(self, data):
        self.buf += data.decode("utf-8", errors="ignore") if isinstance(data, (bytes, bytearray)) else str(data)
        out = []
        while True:
            start = self.buf.find("{")
            if start < 0:
                self.buf = ""
                break
            self.buf = self.buf[start:]
            depth, in_str, esc, end = 0, False, False, -1
            for i, ch in enumerate(self.buf):
                if in_str:
                    esc = (ch == "\\") and not esc
                    if ch == '"' and not esc:
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end < 0:
                if len(self.buf) > self.limit:
                    self.buf = ""
                break
            chunk, self.buf = self.buf[:end + 1], self.buf[end + 1:]
            try:
                out.append(json.loads(chunk))
            except ValueError:
                pass
        return out


async def _find(target, timeout=15):
    from bleak import BleakScanner
    print(f"Suche {target or 'SF-GGS-…'} ({timeout} s) …")
    for d, adv in (await BleakScanner.discover(timeout=timeout, return_adv=True)).values():
        name = d.name or adv.local_name or ""
        if (target and d.address.upper() == target.upper()) or (not target and name.upper().startswith("SF-GGS")):
            print(f"Gefunden: {name} [{d.address}] RSSI {adv.rssi}")
            return d
    raise SystemExit("Nicht gefunden. Leiste in Reichweite? App am Handy geschlossen (Bluetooth frei)?")


async def main(target=None, secs=30):
    from bleak import BleakClient
    dev = await _find(target)
    asm = JsonAssembler()

    def on_notify(char, data):
        print(f"[{time.strftime('%H:%M:%S')}] {getattr(char, 'uuid', char)[4:8]} roh: {bytes(data)[:120].hex()}  {bytes(data)[:120]!r}")
        for obj in asm.feed(bytes(data)):
            print("  JSON:", json.dumps(obj, ensure_ascii=False)[:2000])
            print("  Erkannt:", parse_status(obj))

    async with BleakClient(dev, timeout=25) as c:
        print("Verbunden. Dienste:")
        notifiable = []
        for s in c.services:
            print(f"  Dienst {s.uuid}")
            for ch in s.characteristics:
                print(f"    {ch.uuid}  {','.join(ch.properties)}")
                if "notify" in ch.properties or "indicate" in ch.properties:
                    notifiable.append(ch.uuid)
        for u in notifiable:
            try:
                await c.start_notify(u, on_notify)
                print(f"Benachrichtigungen an: {u}")
            except Exception as e:
                print(f"  {u}: {e!r}")
        for cmd in ({"method": "getDevSta"}, {"method": "getSysSta"}):
            raw = json.dumps(cmd, separators=(",", ":")).encode()
            try:
                await c.write_gatt_char(WRITE, raw, response=True)
                print(f"-> {raw.decode()}")
            except Exception as e:
                print(f"Schreiben fehlgeschlagen ({raw.decode()}): {e!r}")
            await asyncio.sleep(4)
        print(f"Warte {secs} s auf weitere Meldungen …")
        await asyncio.sleep(secs)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
