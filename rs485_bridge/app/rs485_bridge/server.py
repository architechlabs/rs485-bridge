"""Authenticated local/Ingress UI, versioned configuration and engineering exports."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
from dataclasses import asdict
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout, ClientError
from rs485_tool.device_detection import detect_devices
from .runtime import Runtime
from .schema import BridgeConfig, Gateway, Unit, Profile, SiteBundle, atomic_json

LOG = logging.getLogger(__name__)
MAX_UPLOAD = 1024 * 1024

def revision(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

async def supervisor_mqtt():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        async with ClientSession(timeout=ClientTimeout(total=5)) as session:
            async with session.get("http://supervisor/services/mqtt", headers={"Authorization":f"Bearer {token}"}) as response:
                if response.status != 200:
                    return None
                data = (await response.json())["data"]
                if not all(key in data for key in ("host", "port", "username", "password")):
                    return None
                return data
    except (ClientError, TimeoutError, ValueError, KeyError, TypeError):
        LOG.warning("Supervisor MQTT discovery is unavailable; configure broker details in Settings.")
        return None

def create_app(runtime: Runtime, token: str, ingress=False):
    @web.middleware
    async def access(request, handler):
        if request.path == "/health" and request.remote in ("127.0.0.1", "::1"):
            return web.json_response({"ok":True})
        if ingress:
            # Supervisor authenticates the user; direct container connections
            # must not bypass that authentication by spoofing a header.
            if request.remote != "172.30.32.2":
                raise web.HTTPForbidden(text="Use the Home Assistant Open Web UI button")
        elif request.path.startswith("/api/"):
            provided = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(provided, token):
                return web.json_response({"error":"unauthorized"}, status=401)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("X-Bridge-Request") != "1":
                return web.json_response({"error":"missing request verification header"}, status=403)
            origin = request.headers.get("Origin")
            if origin:
                from urllib.parse import urlsplit
                if not ingress and urlsplit(origin).netloc != request.host:
                    return web.json_response({"error":"cross-origin request rejected"}, status=403)
        try:
            result = await handler(request)
            result.headers["X-Content-Type-Options"] = "nosniff"
            result.headers["Referrer-Policy"] = "same-origin"
            result.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
            return result
        except PermissionError as exc:
            return web.json_response({"error":str(exc)}, status=403)
        except asyncio.QueueFull:
            return web.json_response({"error":"Command queue is full. Wait for existing operations."}, status=429)
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({"error":str(exc)}, status=400)
        except TimeoutError:
            return web.json_response({"error":"Command timed out. Check capture/audit before repeating a write."}, status=504)
        except OSError as exc:
            return web.json_response({"error":str(exc)}, status=502)

    app = web.Application(middlewares=[access], client_max_size=MAX_UPLOAD)
    mutation_lock = asyncio.Lock()

    async def status(request):
        return web.json_response(runtime.snapshot())

    async def config(request):
        async with mutation_lock:
            return await configuration(request)

    async def configuration(request):
        current = runtime.config.model_dump()
        if request.method == "GET":
            redacted = json.loads(json.dumps(current))
            if redacted["mqtt"]["password"]:
                redacted["mqtt"]["password"] = "********"
            return web.json_response({"configuration":redacted, "revision":revision(current)})
        body = await request.json()
        if body.get("revision") != revision(current):
            return web.json_response({"error":"Settings changed. Reload before saving."}, status=409)
        data = body["configuration"]
        if data.get("mqtt", {}).get("password") == "********":
            data["mqtt"]["password"] = current["mqtt"]["password"]
        updated = BridgeConfig.model_validate(data)
        enables_tx = updated.tx_enabled and not runtime.config.tx_enabled
        enables_writes = updated.allow_writes and not runtime.config.allow_writes
        old_units = {u.id:u for u in runtime.config.units}
        enables_unit = any(u.control_enabled and (u.id not in old_units or not old_units[u.id].control_enabled) for u in updated.units)
        if (enables_tx or enables_writes or enables_unit) and body.get("confirmation") != "ENABLE_TRANSMISSION":
            raise PermissionError("Explicit ENABLE_TRANSMISSION confirmation is required to enable transmission or unit control")
        await runtime.configure(updated)
        return web.json_response({"saved":True})

    async def profiles(request):
        if request.method == "GET":
            return web.json_response([p.model_dump() for p in runtime.profiles.values()])
        async with mutation_lock:
            profile = Profile.model_validate(await request.json())
            await runtime.import_profile(profile)
        return web.json_response({"installed":profile.id, "version":profile.version})

    async def serial_devices(request):
        import asyncio
        devices = await asyncio.to_thread(detect_devices)
        links = {str(p.resolve()):str(p) for p in Path('/dev/serial/by-id').glob('*')} if os.name != 'nt' else {}
        return web.json_response([{**asdict(d), "stable_port":links.get(d.port, d.port)} for d in devices])

    async def command(request):
        body = await request.json()
        result = await runtime.command(body["unit"], body["point"], body["value"])
        return web.json_response(result)

    async def site_import(request):
        async with mutation_lock:
            body=await request.json()
            if body.get('revision')!=revision(runtime.config.model_dump()):
                return web.json_response({'error':'Settings changed. Reload before importing.'},status=409)
            bundle=SiteBundle.model_validate(body['bundle'])
            return web.json_response(await runtime.import_bundle(bundle,
                preserve_mqtt=body.get('preserve_mqtt',True) is not False,
                preserve_instance_id=body.get('preserve_instance_id',True) is not False))

    async def site_presets(request):
        folder=Path(__file__).parents[2]/'sites'
        return web.json_response([json.loads(path.read_text(encoding='utf-8')) for path in sorted(folder.glob('*.json'))])

    async def start_monitoring(request):
        async with mutation_lock:
            body=await request.json()
            if body.get('confirmation')!='ENABLE_READ_MONITORING':
                raise PermissionError('Explicit read-only monitoring confirmation is required; reads transmit requests')
            if body.get('revision')!=revision(runtime.config.model_dump()):
                return web.json_response({'error':'Settings changed. Reload first.'},status=409)
            updated=runtime.config.model_copy(deep=True)
            gateway=next((g for g in updated.gateways if g.id==body.get('gateway')),None)
            if not gateway:
                raise ValueError('gateway not found')
            if gateway.passive:
                raise PermissionError('Passive capture cannot poll')
            if not any(u.gateway==gateway.id and u.enabled and u.address is not None for u in updated.units):
                raise ValueError('Assign and enable at least one unit before starting monitoring')
            gateway.polling_enabled=True
            updated.tx_enabled=True
            updated.allow_writes=False
            await runtime.configure(updated)
            return web.json_response({'monitoring':True,'allow_writes':False})

    async def action(request):
        body = await request.json()
        worker = runtime.workers.get(body.get("gateway"))
        if not worker:
            raise ValueError("gateway not found")
        if request.match_info['action']=='test':
            return web.json_response(await runtime.check_connection(worker.gateway.id))
        if request.match_info["action"] == "poll":
            if not runtime.config.tx_enabled or worker.gateway.passive:
                raise PermissionError("TX locked or passive mode; reads also transmit requests")
            worker.force_poll = True
        else:
            worker.reconnect_requested = True
        return web.json_response({"queued":True})

    async def events(request):
        return web.json_response(runtime.store.recent(int(request.query.get("limit", 100))))

    async def export(request):
        response = web.StreamResponse(headers={"Content-Type":"application/x-ndjson", "Content-Disposition":"attachment; filename=rs485-capture.jsonl"})
        await response.prepare(request)
        for line in runtime.store.export():
            await response.write(line.encode())
        await response.write_eof()
        return response

    async def bundle(request):
        data = runtime.config.model_dump()
        data["mqtt"]["password"] = ""
        return web.json_response({"configuration":data, "profiles":[p.model_dump() for p in runtime.profiles.values()]},
            headers={"Content-Disposition":"attachment; filename=rs485-configuration.json"})

    async def saved(request):
        path = runtime.data_dir / "commands.json"
        items = json.loads(path.read_text()) if path.exists() else {}
        if request.method == "GET":
            return web.json_response(items)
        body = await request.json()
        name = str(body["name"])
        if not 1 <= len(name) <= 80:
            raise ValueError("command name must be 1..80 characters")
        if request.match_info.get("run"):
            if name not in items:
                raise ValueError("saved command not found")
            item = items[name]
            return web.json_response(await runtime.command(item["unit"], item["point"], item["value"], "saved_command"))
        unit = next((u for u in runtime.config.units if u.id == body["unit"]), None)
        if not unit:
            raise ValueError("unit not found")
        point = next((p for p in runtime.profiles[unit.profile].points if p.id == body["point"]), None)
        if not point:
            raise ValueError("point not found")
        point.encode(body["value"])
        items[name] = {k:body[k] for k in ("unit", "point", "value")}
        atomic_json(path, items)
        runtime.store.audit("command_saved", {"name":name, **items[name]})
        return web.json_response({"saved":name})

    async def home(request):
        return web.FileResponse(Path(__file__).parents[2] / "web" / "index.html")

    app.router.add_get("/", home)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/config", config)
    app.router.add_post("/api/config", config)
    app.router.add_get("/api/profiles", profiles)
    app.router.add_post("/api/profiles", profiles)
    app.router.add_get("/api/serial", serial_devices)
    app.router.add_post("/api/command", command)
    app.router.add_post("/api/import", site_import)
    app.router.add_get("/api/sites", site_presets)
    app.router.add_post("/api/monitor", start_monitoring)
    app.router.add_post("/api/gateway/{action:poll|reconnect|test}", action)
    app.router.add_get("/api/events", events)
    app.router.add_get("/api/export", export)
    app.router.add_get("/api/bundle", bundle)
    app.router.add_get("/api/commands", saved)
    app.router.add_post("/api/commands", saved)
    app.router.add_post("/api/commands/{run:run}", saved)
    app.router.add_get("/health", status)
    app.router.add_static("/assets/", Path(__file__).parents[2] / "web" / "assets")
    async def start(app):
        if ingress and not runtime.config.mqtt.host:
            detected = await supervisor_mqtt()
            if detected:
                runtime.config.mqtt = runtime.config.mqtt.model_copy(update={
                    "enabled":True, "host":detected["host"], "port":detected["port"],
                    "username":detected["username"], "password":detected["password"]})
        await runtime.start()
    async def stop(app):
        await runtime.stop()
        runtime.store.close()
    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return app

def main():
    parser = argparse.ArgumentParser(description="Home Assistant Modbus bridge and setup UI")
    parser.add_argument("--data", type=Path, default=Path("runtime/bridge"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--ingress", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    runtime = Runtime(args.data)
    path = args.data / "api-token"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
        try:
            path.chmod(0o600)
        except OSError:
            pass
    LOG.info("Local API token is stored in %s. New installations start TX locked.", path)
    web.run_app(create_app(runtime, path.read_text().strip(), args.ingress), host=args.host, port=args.port)

if __name__ == "__main__":
    main()
