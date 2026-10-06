"""Owns gateway workers, policies, unit state and lifecycle. No network on import."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from rs485_tool.framing import SilenceFramer
from rs485_tool.modbus import decode_frame
from .events import EventStore
from .mqtt import MQTTBridge
from .schema import BridgeConfig, Profile, atomic_json, validate_links
from .transport import Wire

LOG = logging.getLogger(__name__)

class Runtime:
    def __init__(self, data_dir: Path, profiles_dir: Path | None = None):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = data_dir / "settings.json"
        self.profiles_dir = profiles_dir or Path(__file__).resolve().parents[2] / "profiles"
        self.custom_profiles = data_dir / "profiles"
        self.custom_profiles.mkdir(exist_ok=True)
        self.profiles = self.load_profiles()
        self.config = BridgeConfig.model_validate_json(self.settings_path.read_text()) if self.settings_path.exists() else BridgeConfig()
        validate_links(self.config, self.profiles)
        self.store = EventStore(data_dir / "captures.sqlite3")
        self.workers = {}
        self.states = {}
        self.mqtt = None
        self.configure_lock = asyncio.Lock()

    def load_profiles(self):
        profiles = {}
        for directory in (self.profiles_dir, self.custom_profiles):
            if directory.exists():
                for path in sorted(directory.glob("*.json")):
                    profile = Profile.model_validate_json(path.read_text(encoding="utf-8"))
                    profiles[profile.id] = profile
        return profiles

    async def start(self):
        self.states = {u.id:{"available":False, "values":{}, "error":"Not polled", "updated_at":None} for u in self.config.units}
        self.mqtt = MQTTBridge(self)
        self.mqtt.start()
        for gateway in self.config.gateways:
            worker = GatewayWorker(self, gateway)
            self.workers[gateway.id] = worker
            worker.task = asyncio.create_task(worker.run(), name=f"gateway:{gateway.id}")

    async def stop(self):
        for worker in self.workers.values():
            worker.stopping = True
        await asyncio.gather(*(w.task for w in self.workers.values()), return_exceptions=True)
        self.workers.clear()
        if self.mqtt:
            await asyncio.to_thread(self.mqtt.stop)
            self.mqtt = None

    async def configure(self, config: BridgeConfig):
        validate_links(config, self.profiles)
        async with self.configure_lock:
            await self.stop()
            atomic_json(self.settings_path, config.model_dump())
            self.config = config
            self.store.audit("configuration_updated", {"tx_enabled":config.tx_enabled, "allow_writes":config.allow_writes,
                "gateways":[g.id for g in config.gateways], "units":[u.id for u in config.units]})
            await self.start()

    async def import_profile(self, profile: Profile):
        profiles = {**self.profiles, profile.id:profile}
        validate_links(self.config, profiles)
        # Stop transactions before replacing a mapping to prevent a command from
        # using a mixture of old addresses and new values.
        async with self.configure_lock:
            await self.stop()
            atomic_json(self.custom_profiles / f"{profile.id}.json", profile.model_dump())
            self.profiles = profiles
            if any(u.profile == profile.id for u in self.config.units):
                # A profile change may alter live write addresses. Require a
                # new deliberate authorization before any bus transmission.
                self.config.tx_enabled = False
                self.config.allow_writes = False
                atomic_json(self.settings_path, self.config.model_dump())
            self.store.audit("profile_imported", {"profile":profile.id, "version":profile.version})
            await self.start()

    def publish_all(self):
        if self.mqtt:
            for unit_id, state in self.states.items():
                self.mqtt.publish(unit_id, state)

    async def command(self, unit_id, point_id, value, source="api"):
        unit = next((u for u in self.config.units if u.id == unit_id and u.enabled), None)
        if not unit:
            raise ValueError("unit not found")
        if not self.config.tx_enabled or not self.config.allow_writes or not unit.control_enabled:
            self.store.audit("command_blocked", {"unit":unit_id, "point":point_id, "source":source})
            raise PermissionError("Control is locked. Enable transmission, writes and control for this unit in Settings.")
        gateway = next(g for g in self.config.gateways if g.id == unit.gateway)
        if gateway.passive:
            raise PermissionError("Passive capture cannot transmit")
        profile = self.profiles[unit.profile]
        roles = {p.role:p for p in profile.points if p.role}
        if point_id == "hvac":
            if "power" not in roles or "mode" not in roles:
                raise ValueError("profile has no climate power/mode roles")
            if value == "off":
                operations = [(roles["power"], "OFF")]
            elif value in roles["mode"].commands:
                operations = [(roles["mode"], value), (roles["power"], "ON")]
            else:
                raise ValueError("unsupported HVAC mode")
        else:
            point = next((p for p in profile.points if p.id == point_id), None)
            if not point:
                raise ValueError("point not found")
            operations = [(point, value)]
        for point, requested in operations:
            point.encode(requested)
        future = asyncio.get_running_loop().create_future()
        worker = self.workers[unit.gateway]
        worker.queue.put_nowait((unit, operations, source, time.monotonic() + 15, future))
        return await asyncio.wait_for(future, timeout=30)

    def snapshot(self):
        return {"version":"0.2.0", "tx_enabled":self.config.tx_enabled,
            "allow_writes":self.config.allow_writes, "mqtt_connected":bool(self.mqtt and self.mqtt.connected),
            "units":self.states, "gateways":{gid:{"status":w.status, "error":w.error, "queued":w.queue.qsize()} for gid,w in self.workers.items()}}

class GatewayWorker:
    def __init__(self, runtime, gateway):
        self.runtime, self.gateway = runtime, gateway
        self.wire = Wire(gateway, runtime.store)
        self.queue = asyncio.Queue(maxsize=100)
        self.stopping = False
        self.status, self.error = "idle", ""
        self.task = None
        self.framer = SilenceFramer(gateway.frame_gap)
        self.force_poll = False
        self.reconnect_requested = False

    async def _next_command(self):
        if self.queue.empty():
            return
        unit, operations, source, expires, future = self.queue.get_nowait()
        if future.cancelled() or expires < time.monotonic() or self.stopping:
            if not future.done():
                future.set_exception(TimeoutError("command expired before transmission"))
            return
        try:
            cfg = self.runtime.config
            if not cfg.tx_enabled or not cfg.allow_writes or not unit.control_enabled or self.gateway.passive:
                raise PermissionError("TX locked before command execution")
            results = []
            profile = self.runtime.profiles[unit.profile]
            for point, value in operations:
                address = profile.base_address + unit.address * profile.address_stride + point.offset
                raw = point.encode(value)
                self.runtime.store.audit("write_requested", {"unit":unit.id, "point":point.id, "address":address, "raw_value":raw, "source":source})
                await asyncio.to_thread(self.wire.exchange, unit, point.write_function, address, raw, source)
                # Readback is its own read-only transaction, never a write retry.
                verified, readback = False, None
                try:
                    values = await asyncio.to_thread(self.wire.exchange, unit, point.read_function, address, 1, "write_readback")
                    readback = point.decode(values[0])
                    expected = point.decode(1 if point.write_function == 5 and raw else raw)
                    verified = readback == expected
                    self.runtime.states[unit.id]["values"][point.id] = readback
                except OSError as exc:
                    self.runtime.states[unit.id].update(available=False, error=f"Write acknowledged; readback failed: {exc}")
                results.append({"point":point.id, "acknowledged":True, "state_verified":verified, "readback":readback})
                self.runtime.store.audit("write_result", {"unit":unit.id, **results[-1]})
            self.runtime.publish_all()
            if not future.done():
                future.set_result({"unit":unit.id, "results":results})
        except Exception as exc:
            self.runtime.states[unit.id].update(available=False, error=f"Command failed; readback required: {exc}")
            self.runtime.publish_all()
            self.runtime.store.audit("command_failed", {"unit":unit.id, "error":str(exc), "source":source,
                "note":"No automatic write retry; execution may be uncertain after transport failure"})
            if not future.done():
                future.set_exception(exc)

    async def _poll_unit(self, unit):
        profile = self.runtime.profiles[unit.profile]
        state = self.runtime.states[unit.id]
        try:
            for point in profile.points:
                if self.stopping:
                    return
                await self._next_command()
                address = profile.base_address + unit.address * profile.address_stride + point.offset
                try:
                    values = await asyncio.to_thread(self.wire.exchange, unit, point.read_function, address, 1)
                except OSError:
                    if point.optional:
                        state["values"].pop(point.id, None)
                        continue
                    raise
                state["values"][point.id] = point.decode(values[0])
            state.update(available=True, error="", updated_at=datetime.now(timezone.utc).isoformat())
            self.status, self.error = "connected", ""
        except Exception as exc:
            state.update(available=False, error=str(exc))
            self.status, self.error = "retrying", str(exc)
            self.wire.close()
        self.runtime.publish_all()

    async def _passive(self):
        try:
            chunk = await asyncio.to_thread(self.wire.passive_read)
            now = time.monotonic()
            if chunk:
                self.runtime.store.event(self.gateway.id, None, "rx", chunk, {"kind":"raw_chunk", "transport":"serial", "settings":self.gateway.model_dump()})
                frames = self.framer.feed(chunk, now)
                if len(self.framer.buffer) >= 65536:
                    frames.extend(self.framer.flush())
            else:
                frames = self.framer.flush_if_idle(now)
            for raw, gap in frames:
                parsed = decode_frame(raw)
                self.runtime.store.event(self.gateway.id, None, "rx", raw, {"kind":"timing_frame", "status":parsed.status, "crc_valid":parsed.crc_valid, "gap":gap})
            self.status, self.error = "passive", ""
        except Exception as exc:
            self.wire.close()
            self.status, self.error = "retrying", str(exc)
            await asyncio.sleep(2)

    async def run(self):
        next_poll = 0.0
        failures = 0
        try:
            while not self.stopping:
                if self.gateway.passive:
                    await self._passive()
                    await asyncio.sleep(0.01)
                    continue
                await self._next_command()
                if self.reconnect_requested:
                    self.wire.close()
                    self.reconnect_requested = False
                    next_poll = 0
                if self.runtime.config.tx_enabled and (self.force_poll or (self.gateway.polling_enabled and time.monotonic() >= next_poll)):
                    self.force_poll = False
                    for unit in self.runtime.config.units:
                        if unit.enabled and unit.gateway == self.gateway.id:
                            await self._poll_unit(unit)
                    failures = min(failures + 1, 6) if self.error else 0
                    next_poll = time.monotonic() + max(self.gateway.poll_interval, min(120, 2 ** failures))
                elif not self.runtime.config.tx_enabled:
                    self.status = "TX locked"
                await asyncio.sleep(0.1)
        finally:
            self.wire.close()
            pending = self.framer.flush()
            for raw, gap in pending:
                self.runtime.store.event(self.gateway.id, None, "rx", raw, {"kind":"timing_frame", "note":"shutdown flush", "gap":gap})
            while not self.queue.empty():
                _, _, _, _, future = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(RuntimeError("configuration changed or service stopped"))
