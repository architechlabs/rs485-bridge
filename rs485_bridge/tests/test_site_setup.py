"""Setup regressions: safe pending rooms, preserved MQTT, host diagnostics and partial reads."""
import asyncio
import json
from pathlib import Path
import pytest
from aiohttp.test_utils import TestClient, TestServer
from rs485_bridge.runtime import Runtime
from rs485_bridge.schema import SiteBundle, BridgeConfig, Gateway, Unit, MQTT
from rs485_bridge.server import create_app
from rs485_bridge.mock import MockController
from rs485_bridge.mqtt import discovery_records


def test_site_import_preserves_mqtt_and_unknown_rooms_never_transmit(tmp_path):
    async def run():
        runtime=Runtime(tmp_path)
        runtime.config.mqtt=MQTT(enabled=False,host='existing-broker',username='existing-user',password='keep-private')
        runtime.config.instance_id='existing_site'
        runtime.config.gateways=[Gateway(id='existing_gateway',name='Existing',host='192.168.29.136')]
        runtime.config.units=[Unit(id='unit_1',name='Office',gateway='existing_gateway',address=7,profile='lg-ac-smart5')]
        bundle=SiteBundle.model_validate_json((Path(__file__).parents[1]/'sites/sherry-singh-site.json').read_text(encoding='utf-8'))
        try:
            result=await runtime.import_bundle(bundle)
            assert result['units']==12 and result['pending_addresses']==11
            assert runtime.config.instance_id=='existing_site' and runtime.config.mqtt.password=='keep-private'
            assert runtime.config.mqtt.host=='existing-broker'
            assert not runtime.config.tx_enabled and not runtime.config.allow_writes
            assert len(discovery_records(runtime.config,runtime.profiles))==96
            assert sum(u.address is None for u in runtime.config.units)==11
            assert next(u for u in runtime.config.units if u.address==7).id=='unit_1'
            for unit in runtime.config.units:
                if unit.address is None:
                    assert runtime.states[unit.id]['phase']=='unassigned'
                    await runtime.workers[unit.gateway]._poll_unit(unit)
                    with pytest.raises(PermissionError): await runtime.command(unit.id,'fan','high')
            assert runtime.store.recent()==[]
            broken=bundle.model_copy(deep=True)
            broken.configuration.units[0].profile='missing-map'
            before=runtime.settings_path.read_bytes()
            with pytest.raises(ValueError): await runtime.import_bundle(broken)
            assert runtime.settings_path.read_bytes()==before
        finally: await runtime.stop();runtime.store.close()
    asyncio.run(run())


def test_locked_transport_diagnostic_sends_zero_modbus_bytes(tmp_path):
    async def run():
        mock=MockController();server=await asyncio.start_server(mock.serve,'127.0.0.1',0)
        runtime=Runtime(tmp_path)
        try:
            await runtime.configure(BridgeConfig(gateways=[Gateway(id='lg',name='Mock',host='127.0.0.1',port=server.sockets[0].getsockname()[1])]))
            result=await runtime.check_connection('lg')
            assert result['reachable'] and result['modbus_bytes_sent']==0
            assert not runtime.config.tx_enabled and mock.requests==[] and runtime.store.recent()==[]
        finally: await runtime.stop();runtime.store.close();server.close();await server.wait_closed()
    asyncio.run(run())


def test_explicit_read_monitoring_does_not_enable_writes(tmp_path):
    async def run():
        mock=MockController();server=await asyncio.start_server(mock.serve,'127.0.0.1',0)
        runtime=Runtime(tmp_path)
        runtime.config=BridgeConfig(gateways=[Gateway(id='lg',name='Mock',host='127.0.0.1',port=server.sockets[0].getsockname()[1],request_delay=.1,connect_delay=0)],
            units=[Unit(id='office',name='OFFICE',gateway='lg',address=7,profile='lg-ac-smart5')])
        client=TestClient(TestServer(create_app(runtime,'test-token')));await client.start_server()
        headers={'Authorization':'Bearer test-token','X-Bridge-Request':'1'}
        try:
            data=await (await client.get('/api/config',headers=headers)).json()
            response=await client.post('/api/monitor',headers=headers,json={'gateway':'lg','revision':data['revision']})
            assert response.status==403
            response=await client.post('/api/monitor',headers=headers,json={'gateway':'lg','revision':data['revision'],'confirmation':'ENABLE_READ_MONITORING'})
            assert response.status==200 and runtime.config.tx_enabled and not runtime.config.allow_writes
            deadline=asyncio.get_running_loop().time()+10
            while not runtime.states['office']['available']:
                assert asyncio.get_running_loop().time()<deadline
                await asyncio.sleep(.05)
            assert all(frame[7] in (1,3) for frame in mock.requests)
            with pytest.raises(PermissionError): await runtime.command('office','fan','low')
        finally: await client.close();server.close();await server.wait_closed()
    asyncio.run(run())


def test_register_data_survives_unsupported_coil_function(tmp_path):
    async def run():
        async def limited(reader,writer):
            try:
                header=await reader.readexactly(6)
                body=await reader.readexactly(int.from_bytes(header[4:6],'big'))
                unit,function=body[:2]
                if function==3:
                    address=int.from_bytes(body[2:4],'big')
                    value={0x70:1,0x71:3,0x72:24,0x75:26,0x76:0}[address]
                    reply=bytes([unit,3,2])+value.to_bytes(2,'big')
                else: reply=bytes([unit,function|128,1])
                writer.write(header[:4]+len(reply).to_bytes(2,'big')+reply);await writer.drain()
            finally: writer.close();await writer.wait_closed()
        server=await asyncio.start_server(limited,'127.0.0.1',0)
        runtime=Runtime(tmp_path)
        try:
            await runtime.configure(BridgeConfig(tx_enabled=True,gateways=[Gateway(id='lg',name='Mock',host='127.0.0.1',port=server.sockets[0].getsockname()[1],request_delay=.1,connect_delay=0)],
                units=[Unit(id='office',name='OFFICE',gateway='lg',address=7,profile='lg-ac-smart5')]))
            await runtime.workers['lg']._poll_unit(runtime.config.units[0])
            state=runtime.states['office']
            assert state['phase']=='partial' and not state['available']
            assert state['values']['current']==26 and state['values']['target']==24
            assert state['point_availability']['current'] and not state['point_availability']['power']
            assert state['point_errors']['power']['code']=='modbus_exception'
            records=discovery_records(runtime.config,runtime.profiles)
            sensor=next(v for k,v in records.items() if k.endswith('/current/config'))
            assert sensor['availability'][1]['topic'].endswith('/point/current/availability')
        finally: await runtime.stop();runtime.store.close();server.close();await server.wait_closed()
    asyncio.run(run())


def test_unassigned_control_and_duplicate_addresses_rejected():
    with pytest.raises(ValueError): Unit(id='a',name='A',gateway='g',profile='p',address=None,control_enabled=True)
    with pytest.raises(ValueError):
        BridgeConfig(gateways=[Gateway(id='g',name='G',host='localhost')],units=[
            Unit(id='a',name='A',gateway='g',profile='p',address=7),Unit(id='b',name='B',gateway='g',profile='p',address=7)])


def test_mapping_import_persists_lock_before_profile_write(tmp_path,monkeypatch):
    async def run():
        import rs485_bridge.runtime as module
        runtime=Runtime(tmp_path)
        runtime.config=BridgeConfig(tx_enabled=True,allow_writes=True)
        original=module.atomic_json
        def fail_profile(path,data):
            if path.parent==runtime.custom_profiles:
                persisted=json.loads(runtime.settings_path.read_text())
                assert not persisted['tx_enabled'] and not persisted['allow_writes']
                raise OSError('simulated profile storage failure')
            original(path,data)
        monkeypatch.setattr(module,'atomic_json',fail_profile)
        try:
            with pytest.raises(OSError):
                await runtime.import_profile(runtime.profiles['lg-ac-smart5'])
            assert not runtime.config.tx_enabled and not runtime.config.allow_writes
            assert not json.loads(runtime.settings_path.read_text())['tx_enabled']
            assert not (runtime.custom_profiles/'lg-ac-smart5.json').exists()
        finally: await runtime.stop();runtime.store.close()
    asyncio.run(run())


def test_total_response_deadline_retains_partial_bytes(tmp_path):
    async def run():
        from rs485_bridge.transport import Wire
        async def slow(reader,writer):
            try:
                header=await reader.readexactly(6)
                body=await reader.readexactly(int.from_bytes(header[4:6],'big'))
                response=header[:4]+bytes.fromhex('0005')+bytes([body[0],3,2,0,24])
                for byte in response:
                    writer.write(bytes([byte]));await writer.drain();await asyncio.sleep(.07)
            except (asyncio.IncompleteReadError,ConnectionError): pass
            finally:
                writer.close()
                try: await writer.wait_closed()
                except ConnectionError: pass
        server=await asyncio.start_server(slow,'127.0.0.1',0)
        runtime=Runtime(tmp_path)
        try:
            wire=Wire(Gateway(id='lg',name='Slow',host='127.0.0.1',port=server.sockets[0].getsockname()[1],timeout=.2,connect_delay=0),runtime.store)
            unit=Unit(id='office',name='OFFICE',gateway='lg',address=7,profile='lg-ac-smart5')
            with pytest.raises(TimeoutError): await asyncio.to_thread(wire.exchange,unit,3,0x72,1)
            assert any(e['direction']=='rx' and e['details'].get('status')=='partial' for e in runtime.store.recent())
        finally: wire.close();runtime.store.close();server.close();await server.wait_closed()
    asyncio.run(run())


def test_interrupted_import_recovery_restores_maps_and_locks(tmp_path):
    from rs485_bridge.schema import atomic_json
    runtime=Runtime(tmp_path)
    old=runtime.profiles['lg-ac-smart5'].model_copy(deep=True)
    changed=old.model_copy(deep=True);changed.version='9.0.0'
    atomic_json(runtime.import_journal,{'configuration':BridgeConfig(tx_enabled=True,allow_writes=True).model_dump(),
        'profiles':[{'id':old.id,'previous':None}]})
    atomic_json(runtime.custom_profiles/f'{old.id}.json',changed.model_dump())
    runtime.store.close()
    recovered=Runtime(tmp_path)
    try:
        assert recovered.profiles[old.id].version==old.version
        assert not recovered.config.tx_enabled and not recovered.config.allow_writes
        assert not recovered.import_journal.exists()
    finally: recovered.store.close()


def test_import_ui_events_and_failure_logs_do_not_leak_credentials(tmp_path,caplog):
    async def run():
        import logging
        runtime=Runtime(tmp_path)
        client=TestClient(TestServer(create_app(runtime,'test-token')))
        await client.start_server()
        headers={'Authorization':'Bearer test-token','X-Bridge-Request':'1'}
        try:
            with caplog.at_level(logging.INFO):
                response=await client.post('/api/ui-events',headers=headers,json={'event':'import_file_selected'})
                assert response.status==200
                data=await (await client.get('/api/config',headers=headers)).json()
                response=await client.post('/api/import',headers=headers,json={'revision':data['revision'],
                    'bundle':{'configuration':{'mqtt':{'password':'must-never-appear-in-logs'},'unknown':'invalid'}}})
                assert response.status==400
            assert 'import_file_selected' in caplog.text and 'API validation failed: /api/import' in caplog.text
            assert 'must-never-appear-in-logs' not in caplog.text
        finally: await client.close()
    asyncio.run(run())
