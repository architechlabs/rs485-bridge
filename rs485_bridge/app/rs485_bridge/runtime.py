"""Owns gateway workers, policies, unit state and lifecycle. No network on import."""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from rs485_tool.framing import SilenceFramer
from rs485_tool.modbus import decode_frame
from .events import EventStore
from .mqtt import MQTTBridge
from .schema import BridgeConfig, Profile, SiteBundle, atomic_json, validate_links
from .transport import Wire
from .diagnostics import failure_details
from . import __version__

LOG = logging.getLogger(__name__)

class Runtime:
    def __init__(self, data_dir: Path, profiles_dir: Path | None = None):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = data_dir / "settings.json"
        self.profiles_dir = profiles_dir or Path(__file__).resolve().parents[2] / "profiles"
        self.custom_profiles = data_dir / "profiles"
        self.custom_profiles.mkdir(exist_ok=True)
        self.import_journal=data_dir/'site-import-journal.json'
        self._recover_site_import()
        self.profiles = self.load_profiles()
        self.config = BridgeConfig.model_validate_json(self.settings_path.read_text()) if self.settings_path.exists() else BridgeConfig()
        validate_links(self.config, self.profiles)
        self.store = EventStore(data_dir / "captures.sqlite3")
        self.workers = {}
        self.states = {}
        self.mqtt = None
        self.configure_lock = asyncio.Lock()

    def _recover_site_import(self):
        if not self.import_journal.exists():
            return
        journal=json.loads(self.import_journal.read_text(encoding='utf-8'))
        config=BridgeConfig.model_validate(journal['configuration'])
        config.tx_enabled=False
        config.allow_writes=False
        for unit in config.units:
            unit.control_enabled=False
        restores={}
        for item in journal['profiles']:
            profile_id=item['id']
            if not isinstance(profile_id,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',profile_id):
                raise ValueError('Invalid profile ID in import recovery journal')
            data=item['previous']
            if data is not None:
                profile=Profile.model_validate(data)
                if profile.id!=profile_id:
                    raise ValueError('Import recovery profile ID mismatch')
            restores[self.custom_profiles/f'{profile_id}.json']=data
        # Validate the entire recovery record before touching any file.
        for path,data in restores.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_json(path,data)
        atomic_json(self.settings_path,config.model_dump())
        self.import_journal.unlink()
        LOG.warning('Recovered an interrupted site import; TX and writes remain locked')

    def load_profiles(self):
        profiles = {}
        for directory in (self.profiles_dir, self.custom_profiles):
            if directory.exists():
                for path in sorted(directory.glob("*.json")):
                    profile = Profile.model_validate_json(path.read_text(encoding="utf-8"))
                    profiles[profile.id] = profile
        return profiles

    async def start(self):
        self.states = {}
        for unit in self.config.units:
            gateway=next(g for g in self.config.gateways if g.id==unit.gateway)
            reason = ('Assign the indoor address from LG Info → Address' if unit.address is None else
                'Unit is disabled' if not unit.enabled else 'TX locked: enable read-only monitoring to receive live status' if not self.config.tx_enabled else
                'Polling disabled: use Read now or Start monitoring' if not gateway.polling_enabled else 'Waiting for first read')
            self.states[unit.id]={"available":False,"values":{},"error":reason,"updated_at":None,
                "phase":"unassigned" if unit.address is None else "waiting", "point_availability":{},"point_errors":{}}
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
        await self.import_bundle(SiteBundle(profiles=[profile],name=f'Profile {profile.id}'))

    def publish_all(self):
        if self.mqtt:
            for unit_id, state in self.states.items():
                self.mqtt.publish(unit_id, state)

    def publish_unit(self, unit_id):
        if self.mqtt:
            self.mqtt.publish(unit_id,self.states[unit_id])

    async def import_bundle(self, bundle, preserve_mqtt=True, preserve_instance_id=True):
        async with self.configure_lock:
            profiles={**self.profiles, **{p.id:p for p in bundle.profiles}}
            config=(bundle.configuration or self.config).model_copy(deep=True)
            if preserve_mqtt:
                config.mqtt=self.config.mqtt.model_copy(deep=True)
            if preserve_instance_id:
                config.instance_id=self.config.instance_id
            config.tx_enabled=False
            config.allow_writes=False
            for unit in config.units:
                unit.control_enabled=False
            validate_links(config, profiles)
            # Validate all mappings before stopping any worker or changing disk.
            writes={self.custom_profiles/f'{p.id}.json':p.model_dump() for p in bundle.profiles}
            backups={path:json.loads(path.read_text(encoding='utf-8')) if path.exists() else None for path in writes}
            await self.stop()
            safe_previous=self.config.model_copy(deep=True)
            safe_previous.tx_enabled=False
            safe_previous.allow_writes=False
            for unit in safe_previous.units:
                unit.control_enabled=False
            # Persist the lock BEFORE replacing any map. A process/power loss
            # mid-import must never reboot into old write authorization with
            # a mixture of new and old mappings.
            try:
                atomic_json(self.settings_path,safe_previous.model_dump())
            except Exception:
                await self.start()
                raise
            self.config=safe_previous
            try:
                atomic_json(self.import_journal,{'configuration':safe_previous.model_dump(),
                    'profiles':[{'id':path.stem,'previous':data} for path,data in backups.items()]})
                for path,data in writes.items():
                    atomic_json(path,data)
                atomic_json(self.settings_path,config.model_dump())
                self.import_journal.unlink()
            except Exception:
                restored=True
                for path,data in backups.items():
                    try:
                        if data is None:
                            path.unlink(missing_ok=True)
                        else:
                            atomic_json(path,data)
                    except OSError:
                        restored=False
                        LOG.exception('Profile rollback failed; persisted TX lock remains in place')
                # Keep the journal for boot recovery if any restore fails.
                if restored:
                    try:
                        atomic_json(self.settings_path,safe_previous.model_dump())
                        self.import_journal.unlink(missing_ok=True)
                    except OSError:
                        LOG.exception('Import cleanup deferred to startup recovery')
                await self.start()
                raise
            self.profiles=profiles
            self.config=config
            self.store.audit('site_imported',{'name':bundle.name,'units':len(config.units),'pending_addresses':sum(u.address is None for u in config.units)})
            await self.start()
            return {'imported':True,'units':len(config.units),'pending_addresses':sum(u.address is None for u in config.units),
                'tx_enabled':False,'allow_writes':False,'mqtt_preserved':preserve_mqtt}

    async def check_connection(self, gateway_id):
        worker=self.workers.get(gateway_id)
        if worker is None:
            raise ValueError('gateway not found')
        future=asyncio.get_running_loop().create_future()
        worker.diagnostics.put_nowait(future)
        return await asyncio.wait_for(future, timeout=45)

    async def command(self, unit_id, point_id, value, source="api"):
        unit = next((u for u in self.config.units if u.id == unit_id and u.enabled), None)
        if not unit:
            raise ValueError("unit not found")
        if unit.address is None:
            raise PermissionError('Assign a verified indoor address before any unit operation')
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
        return {"version":__version__, "tx_enabled":self.config.tx_enabled,
            "allow_writes":self.config.allow_writes, "mqtt_connected":bool(self.mqtt and self.mqtt.connected),
            "units":self.states, "gateways":{gid:{"status":w.status, "error":w.error, "queued":w.queue.qsize(),
                "diagnostic":w.last_diagnostic,"polling_enabled":w.gateway.polling_enabled,
                "assigned_units":sum(u.gateway==gid and u.enabled and u.address is not None for u in self.config.units),
                "pending_units":sum(u.gateway==gid and u.address is None for u in self.config.units)} for gid,w in self.workers.items()}}

class GatewayWorker:
    def __init__(self, runtime, gateway):
        self.runtime, self.gateway = runtime, gateway
        self.wire = Wire(gateway, runtime.store)
        self.queue = asyncio.Queue(maxsize=100)
        self.diagnostics=asyncio.Queue(maxsize=1)
        self.last_diagnostic=None
        self.stopping = False
        self.status, self.error = "idle", ""
        self.task = None
        self.framer = SilenceFramer(gateway.frame_gap)
        self.force_poll = False
        self.reconnect_requested = False

    async def _diagnose(self):
        if self.diagnostics.empty():
            return
        future=self.diagnostics.get_nowait()
        if future.cancelled():
            return
        try:
            result=await asyncio.to_thread(self.wire.check_connection)
        except OSError as error:
            result={'reachable':False,'modbus_bytes_sent':0,**failure_details(error,self.gateway.transport)}
        result['checked_at']=datetime.now(timezone.utc).isoformat()
        self.last_diagnostic=result
        self.runtime.store.audit('connection_test',{'gateway':self.gateway.id,**result})
        if not future.done():
            future.set_result(result)

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
                    self.runtime.states[unit.id]['point_availability'][point.id]=True
                except OSError as exc:
                    self.runtime.states[unit.id].update(available=False, error=f"Write acknowledged; readback failed: {exc}")
                results.append({"point":point.id, "acknowledged":True, "state_verified":verified, "readback":readback})
                self.runtime.store.audit("write_result", {"unit":unit.id, **results[-1]})
            self.runtime.publish_unit(unit.id)
            if not future.done():
                future.set_result({"unit":unit.id, "results":results})
        except Exception as exc:
            self.runtime.states[unit.id].update(available=False, error=f"Command failed; readback required: {exc}")
            self.runtime.publish_unit(unit.id)
            self.runtime.store.audit("command_failed", {"unit":unit.id, "error":str(exc), "source":source,
                "note":"No automatic write retry; execution may be uncertain after transport failure"})
            if not future.done():
                future.set_exception(exc)

    async def _poll_unit(self, unit):
        profile = self.runtime.profiles[unit.profile]
        state = self.runtime.states[unit.id]
        if unit.address is None:
            return
        errors={}
        cycle_availability={p.id:False for p in profile.points}
        try:
            # Read registers before coils so a failed FC01 doesn't hide useful
            # holding-register data. Each point keeps its own availability.
            for point in sorted(profile.points,key=lambda p:p.read_function in (1,2)):
                if self.stopping:
                    return
                await self._diagnose()
                await self._next_command()
                address = profile.base_address + unit.address * profile.address_stride + point.offset
                try:
                    values = await asyncio.to_thread(self.wire.exchange, unit, point.read_function, address, 1)
                except OSError as error:
                    errors[point.id]={'function':point.read_function,'address':address,**failure_details(error,self.gateway.transport)}
                    state['point_availability'][point.id]=False
                    if not point.optional:
                        state.update(available=False,phase='partial' if any(state['point_availability'].values()) else 'failed')
                    self.runtime.publish_unit(unit.id)
                    self.wire.close()
                    if 'Cannot connect to Modbus TCP' in str(error) or 'Cannot open ' in str(error):
                        for pending in profile.points:
                            if not cycle_availability[pending.id] and pending.id not in errors:
                                errors[pending.id]={'function':pending.read_function,
                                    'address':profile.base_address+unit.address*profile.address_stride+pending.offset,
                                    'code':'transport_unavailable','error':'Not read after connection/open failed',
                                    'hint':errors[point.id]['hint']}
                        break
                    continue
                state["values"][point.id] = point.decode(values[0])
                state['point_availability'][point.id]=True
                cycle_availability[point.id]=True
            required_errors={p.id:errors[p.id] for p in profile.points if not p.optional and p.id in errors}
            state.update(available=not required_errors,point_errors=errors,point_availability=cycle_availability,
                phase='ready' if not required_errors else 'partial' if any(cycle_availability.values()) else 'failed',
                error='; '.join(f'{key}: {value["error"]}' for key,value in required_errors.items()),
                updated_at=datetime.now(timezone.utc).isoformat())
            self.status, self.error = ('connected','') if not required_errors else ('partial' if state['phase']=='partial' else 'retrying',state['error'])
        except Exception as exc:
            state.update(available=False, error=str(exc),phase='failed')
            self.status, self.error = "retrying", str(exc)
            self.wire.close()
        self.runtime.publish_unit(unit.id)

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
                await self._diagnose()
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
                    assigned=[self.runtime.states[u.id] for u in self.runtime.config.units if u.enabled and u.gateway==self.gateway.id and u.address is not None]
                    self.error='; '.join(s['error'] for s in assigned if not s['available'])
                    self.status='connected' if assigned and not self.error else 'retrying' if self.error else 'No assigned units'
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
            while not self.diagnostics.empty():
                future=self.diagnostics.get_nowait()
                if not future.done():
                    future.set_exception(RuntimeError('service stopped'))
