"""Zeigt Govee-Sensoren in Bluetooth-Reichweite mit Rohdaten und dekodierten Werten.

    python -m growcontroller.govee_scan                 # alle Govee-Geräte, 30 s
    python -m growcontroller.govee_scan D6:7D:81:C6:3A:78 EC:2E:85:06:37:0F
"""
import asyncio
import sys

from .sensors import decode_govee

GOVEE_IDS = (0xEC88, 0x0001, 0x8801)


async def main(macs, secs=30):
    from bleak import BleakScanner
    seen = set()

    def cb(dev, adv):
        mac = dev.address.upper()
        name = dev.name or adv.local_name or ""
        if macs and mac not in macs:
            return
        for mid, data in adv.manufacturer_data.items():
            data = bytes(data)
            if mid == 0x004C:  # Apple-iBeacon, senden manche Govee-Modelle zusätzlich – ohne Messwerte
                continue
            if not macs and mid not in GOVEE_IDS and not name.startswith(("GV", "Govee", "ihoment")):
                continue
            key = (mac, mid, data)
            if key in seen:
                continue
            seen.add(key)
            r = decode_govee(mid, data)
            print(f"{mac}  {name[:14]:14} rssi={adv.rssi:4}  id=0x{mid:04X} len={len(data)} raw={data.hex()}  ->  {r or 'NICHT ERKANNT'}", flush=True)

    print(f"Suche {secs} s nach Govee-Sensoren ..." + (f" (nur {', '.join(macs)})" if macs else ""), flush=True)
    async with BleakScanner(detection_callback=cb, bluez={"filters": {"DuplicateData": True}}):
        await asyncio.sleep(secs)
    if not seen:
        print("Nichts empfangen. Bluetooth an? (bluetoothctl show) Sensor in Reichweite?")


if __name__ == "__main__":
    asyncio.run(main([m.upper() for m in sys.argv[1:]]))
