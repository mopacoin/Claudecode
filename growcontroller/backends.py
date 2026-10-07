"""Schalt-Backends. Netzwerk-Backends senden in einem eigenen Thread mit Wiederholung,
damit die Regelschleife nie auf Cloud/LAN/Broker warten muss."""
import asyncio
import logging
import threading

log = logging.getLogger("grow.backend")


class GpioBackend:
    def __init__(self, gpio, pin, active_low=True, **_):
        self.gpio, self.pin, self.active_low = gpio, pin, active_low
        gpio.setup(pin, gpio.OUT, initial=self._level(False))

    def _level(self, on):
        return self.gpio.LOW if on == self.active_low else self.gpio.HIGH

    def set(self, on):
        self.gpio.output(self.pin, self._level(on))

    def close(self):
        pass


class WorkerBackend(threading.Thread):
    """Sendet den gewünschten Zustand, wiederholt bei Fehlern und frischt periodisch auf
    (falls jemand die Steckdose von Hand schaltet)."""

    def __init__(self, resend_s=60, retry_s=10, **_):
        super().__init__(daemon=True)
        self.resend_s, self.retry_s = resend_s, retry_s
        self.desired, self.sent = None, None
        self.wake, self.stopping = threading.Event(), False
        self.healthy = None
        self.start()

    def _send(self, on):  # in Unterklassen
        raise NotImplementedError

    def set(self, on):
        self.desired = on
        self.wake.set()

    def run(self):
        timeout = self.resend_s
        while not self.stopping:
            self.wake.wait(timeout)
            self.wake.clear()
            if self.stopping or self.desired is None:
                continue
            timeout = self._try()
        if self.desired is not None and self.sent != self.desired:
            self._try()

    def _try(self):
        want = self.desired
        try:
            self._send(want)
            self.sent, self.healthy = want, True
            return self.resend_s
        except Exception as e:
            self.healthy = False
            log.warning("%s: %s", type(self).__name__, e)
            return self.retry_s

    def close(self):
        self.stopping = True
        self.wake.set()
        self.join(timeout=15)


class MerossHub:
    """Eine Cloud-Sitzung (asyncio in eigenem Thread) für alle Meross-Steckdosen."""

    def __init__(self, email, password, api_base_url="https://iotx-eu.meross.com"):
        self.args = (email, password, api_base_url)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.manager = self.http = None
        self.lock = threading.Lock()

    async def _connect(self):
        from meross_iot.http_api import MerossHttpClient
        from meross_iot.manager import MerossManager
        email, password, url = self.args
        self.http = await MerossHttpClient.async_from_user_password(api_base_url=url, email=email, password=password)
        self.manager = MerossManager(http_client=self.http)
        await self.manager.async_init()
        await self.manager.async_device_discovery()

    async def _set(self, name, channel, on):
        if self.manager is None:
            await self._connect()
        devs = self.manager.find_devices(device_name=name) or self.manager.find_devices(device_uuids=[name])
        if not devs:
            raise RuntimeError(f"Meross-Gerät '{name}' nicht gefunden")
        dev = devs[0]
        await dev.async_update()
        await (dev.async_turn_on if on else dev.async_turn_off)(channel=channel)

    def set(self, name, channel, on):
        asyncio.run_coroutine_threadsafe(self._set(name, channel, on), self.loop).result(timeout=30)

    def close(self):
        async def _c():
            if self.manager:
                self.manager.close()
            if self.http:
                await self.http.async_logout()
        try:
            asyncio.run_coroutine_threadsafe(_c(), self.loop).result(timeout=10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


class MerossBackend(WorkerBackend):
    def __init__(self, hub, device, channel=0, **kw):
        self.hub, self.device, self.channel = hub, device, channel
        super().__init__(**kw)

    def _send(self, on):
        self.hub.set(self.device, self.channel, on)


class TuyaBackend(WorkerBackend):
    """Tuya-Gerät im lokalen Netz (tinytuya). on_dps: zusätzliche Datenpunkte beim Einschalten,
    z. B. {"4": 55} für Ziel-Feuchte – DP-Nummern per `python3 -m tinytuya wizard` ermitteln."""

    def __init__(self, dev_id, local_key, address="Auto", version=3.3, dp=1, on_dps=None, **kw):
        import tinytuya
        self.dev = tinytuya.Device(dev_id, address, local_key, version=version)
        self.dev.set_socketPersistent(False)
        self.dp, self.on_dps = int(dp), {int(k): v for k, v in (on_dps or {}).items()}
        super().__init__(**kw)

    def _send(self, on):
        dps = {self.dp: bool(on)}
        if on:
            dps.update(self.on_dps)
        res = self.dev.set_multiple_values(dps) if len(dps) > 1 else self.dev.set_value(self.dp, bool(on))
        if isinstance(res, dict) and res.get("Error"):
            raise RuntimeError(f"Tuya: {res['Error']}")


class MqttHub:
    """Ein MQTT-Client pro Broker, mit automatischem Wiederverbinden."""

    def __init__(self, host, port=1883, username=None, password=None):
        import paho.mqtt.client as mqtt
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)  # paho >= 2
        except AttributeError:
            self.client = mqtt.Client()
        if username:
            self.client.username_pw_set(username, password)
        self.client.reconnect_delay_set(1, 30)
        self.client.connect_async(host, port)
        self.client.loop_start()

    def publish(self, topic, payload, retain=False):
        if not self.client.is_connected():
            raise RuntimeError("MQTT nicht verbunden")
        info = self.client.publish(topic, payload, qos=1, retain=retain)
        info.wait_for_publish(timeout=5)
        if not info.is_published():
            raise RuntimeError("MQTT publish fehlgeschlagen")

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


class MqttServoBackend(WorkerBackend):
    """Servo (z. B. Lüftungsklappe) über MQTT. Entweder Winkel (angle_on/angle_off, mit
    payload-Vorlage, z. B. "{angle}" oder '{"angle":{angle}}') oder feste payload_on/payload_off."""

    def __init__(self, hub, topic, angle_on=90, angle_off=0, payload="{angle}",
                 payload_on=None, payload_off=None, retain=True, **kw):
        self.hub, self.topic, self.retain = hub, topic, retain
        self.p_on = payload_on if payload_on is not None else payload.replace("{angle}", str(angle_on))
        self.p_off = payload_off if payload_off is not None else payload.replace("{angle}", str(angle_off))
        super().__init__(**kw)

    def _send(self, on):
        self.hub.publish(self.topic, self.p_on if on else self.p_off, self.retain)
