"""Home Assistant MQTT discovery and non-retained, allowlisted command routing."""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
import paho.mqtt.client as mqtt
from .schema import atomic_json
from . import __version__

LOG = logging.getLogger(__name__)

def discovery_records(config, profiles):
    records = {}
    prefix = f"{config.mqtt.topic_prefix}/{config.instance_id}"
    for unit in config.units:
        if not unit.enabled:
            continue
        profile = profiles[unit.profile]
        base = f"{prefix}/{unit.id}"
        device_id = f"rs485_{config.instance_id}_{unit.id}"
        shared = {
            "device": {"identifiers":[device_id], "name":unit.name, "manufacturer":profile.manufacturer, "model":profile.model},
            "availability": [{"topic":f"{prefix}/availability"}, {"topic":f"{base}/availability"}],
            "availability_mode":"all", "origin":{"name":"RS485 Bridge", "sw_version":__version__},
        }
        for point in profile.points:
            component = point.entity
            body = {**shared, "name":point.name, "unique_id":f"{device_id}_{point.id}"}
            body['availability']=[{'topic':f'{prefix}/availability'}, {'topic':f'{base}/point/{point.id}/availability'}]
            if component != "button":
                body.update(state_topic=f"{base}/state", value_template="{{ value_json." + point.id + " | default('') }}")
            if point.write_function:
                body["command_topic"] = f"{base}/command/{point.id}"
                body["retain"] = False
            if component == "switch":
                body.update(payload_on="ON", payload_off="OFF")
            elif component == "select":
                body["options"] = list(point.commands)
            elif component == "number":
                body.update(min=point.minimum, max=point.maximum, step=point.step, mode="box")
                if point.unit:
                    body["unit_of_measurement"] = point.unit
            elif component == "button":
                body["payload_press"] = next(iter(point.commands))
            elif component == "sensor" and point.unit:
                body["unit_of_measurement"] = point.unit
                if point.unit == "°C":
                    body.update(device_class="temperature", state_class="measurement")
            records[f"{config.mqtt.discovery_prefix}/{component}/{device_id}/{point.id}/config"] = body
        roles = {p.role:p for p in profile.points if p.role}
        if {"power", "mode", "target", "current"}.issubset(roles):
            mode, target, current = roles["mode"], roles["target"], roles["current"]
            body = {**shared, "name":None, "unique_id":f"{device_id}_climate",
                "mode_command_topic":f"{base}/command/hvac",
                "mode_state_topic":f"{base}/state",
                "mode_state_template":"{{ 'off' if value_json." + roles['power'].id + " == 'OFF' else value_json." + mode.id + " }}",
                "modes":["off", *mode.commands], "temperature_unit":"C",
                "temperature_command_topic":f"{base}/command/{target.id}",
                "temperature_state_topic":f"{base}/state",
                "temperature_state_template":"{{ value_json." + target.id + " }}",
                "current_temperature_topic":f"{base}/state",
                "current_temperature_template":"{{ value_json." + current.id + " }}",
                "min_temp":target.minimum, "max_temp":target.maximum, "temp_step":target.step,
                "retain":False}
            if "fan" in roles:
                fan = roles["fan"]
                body.update(fan_mode_command_topic=f"{base}/command/{fan.id}", fan_mode_state_topic=f"{base}/state",
                    fan_mode_state_template="{{ value_json." + fan.id + " }}", fan_modes=list(fan.commands))
            records[f"{config.mqtt.discovery_prefix}/climate/{device_id}/climate/config"] = body
    return records

class MQTTBridge:
    def __init__(self, runtime):
        self.runtime = runtime
        self.client = None
        self.connected = False
        self.loop = asyncio.get_running_loop()
        self.registry = runtime.data_dir / "discovery-topics.json"

    def start(self):
        config = self.runtime.config
        options = config.mqtt
        if not options.enabled or not options.host:
            return
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"rs485_bridge_{config.instance_id}", clean_session=True)
        if options.username:
            self.client.username_pw_set(options.username, options.password)
        if options.tls:
            self.client.tls_set()
        prefix = f"{options.topic_prefix}/{config.instance_id}"
        self.client.will_set(f"{prefix}/availability", "offline", qos=1, retain=True)
        self.client.on_connect = self._connected
        self.client.on_disconnect = self._disconnected
        self.client.on_message = self._message
        self.client.reconnect_delay_set(1, 60)
        self.client.connect_async(options.host, options.port, keepalive=30)
        self.client.loop_start()

    def _connected(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.warning("MQTT connection rejected: %s", reason_code)
            return
        self.connected = True
        cfg = self.runtime.config
        prefix = f"{cfg.mqtt.topic_prefix}/{cfg.instance_id}"
        client.subscribe(f"{prefix}/+/command/+", qos=1)
        client.subscribe("homeassistant/status", qos=1)
        self.publish_discovery()
        client.publish(f"{prefix}/availability", "online", qos=1, retain=True)
        self.loop.call_soon_threadsafe(self.runtime.publish_all)

    def _disconnected(self, client, userdata, flags, reason_code, properties):
        self.connected = False

    def _message(self, client, userdata, message):
        if message.topic == "homeassistant/status" and message.payload == b"online":
            self.publish_discovery()
            self.loop.call_soon_threadsafe(self.runtime.publish_all)
            return
        # Discovery/state are retained. Control messages are never replayed.
        if message.retain or message.dup or len(message.payload) > 512:
            return
        prefix = f"{self.runtime.config.mqtt.topic_prefix}/{self.runtime.config.instance_id}/"
        if not message.topic.startswith(prefix):
            return
        parts = message.topic[len(prefix):].split("/")
        if len(parts) != 3 or parts[1] != "command":
            return
        try:
            value = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            return
        task = asyncio.run_coroutine_threadsafe(self.runtime.command(parts[0], parts[2], value, "mqtt"), self.loop)
        def completed(result):
            try:
                result.result()
            except Exception as exc:
                LOG.warning("MQTT control rejected or failed: %s", exc)
        task.add_done_callback(completed)

    def publish_discovery(self):
        records = discovery_records(self.runtime.config, self.runtime.profiles)
        # Clean up entities removed or renamed by the current configuration.
        try:
            old = set(json.loads(self.registry.read_text()) if self.registry.exists() else [])
        except (OSError, ValueError, TypeError):
            LOG.warning("Discovery topic cache is unreadable; rebuilding it.")
            old = set()
        for topic in old - records.keys():
            self.client.publish(topic, "", qos=1, retain=True)
        for topic, body in records.items():
            self.client.publish(topic, json.dumps(body, ensure_ascii=False), qos=1, retain=True)
        atomic_json(self.registry, list(records))

    def publish(self, unit_id, state):
        if not self.client or not self.connected:
            return
        base = f"{self.runtime.config.mqtt.topic_prefix}/{self.runtime.config.instance_id}/{unit_id}"
        self.client.publish(f"{base}/availability", "online" if state.get("available") else "offline", qos=1, retain=True)
        unit=next((u for u in self.runtime.config.units if u.id==unit_id),None)
        if unit:
            for point in self.runtime.profiles[unit.profile].points:
                available=state.get('point_availability',{}).get(point.id,False)
                self.client.publish(f'{base}/point/{point.id}/availability','online' if available else 'offline',qos=1,retain=True)
        if state.get("values"):
            self.client.publish(f"{base}/state", json.dumps(state["values"], ensure_ascii=False), qos=1, retain=True)

    def stop(self):
        if self.client:
            cfg = self.runtime.config
            if self.connected:
                msg = self.client.publish(f"{cfg.mqtt.topic_prefix}/{cfg.instance_id}/availability", "offline", qos=1, retain=True)
                try:
                    msg.wait_for_publish(timeout=2)
                except RuntimeError:
                    pass
            self.client.disconnect()
            self.client.loop_stop()
            self.client = None
        self.connected = False
