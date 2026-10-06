"""Hardware-free integration tests: actual TCP replies, HTTP API and policy gates."""
import asyncio
import json
from pathlib import Path
import pytest
from aiohttp.test_utils import TestClient, TestServer
from rs485_bridge.runtime import Runtime
from rs485_bridge.schema import BridgeConfig, Gateway, Unit, Profile, Point
from rs485_bridge.mock import MockController
from rs485_bridge.mqtt import discovery_records
from rs485_bridge.server import create_app
from rs485_bridge.transport import Wire

def example_config(port, **kwargs):
    return BridgeConfig(gateways=[Gateway(id='lg',name='Mock',host='127.0.0.1',port=port,request_delay=.1)],
        units=[Unit(id='office',name='OFFICE',gateway='lg',address=7,profile='lg-ac-smart5',control_enabled=True),
               Unit(id='room2',name='SECOND',gateway='lg',address=8,profile='lg-ac-smart5',control_enabled=True)], **kwargs)

def test_profiles_reject_unsafe_or_ambiguous_definitions():
    with pytest.raises(ValueError):
        Point(id='fan',name='Fan',entity='select',write_function=6,commands={'bad':65536})
    with pytest.raises(ValueError):
        Point(id='power',name='Power',entity='switch',write_function=5,commands={'ON':1,'OFF':0})
    point=Point(id='target',name='Target',entity='number',write_function=6,minimum=16,maximum=30,step=1)
    assert point.encode(23)==23
    for value in [15,31,22.5,float('nan')]:
        with pytest.raises(ValueError): point.encode(value)
    with pytest.raises(ValueError):
        BridgeConfig(gateways=[Gateway(id='a',name='a',host='localhost'),Gateway(id='b',name='b',host='localhost')])


def test_partial_serial_reply_is_preserved(tmp_path):
    runtime=Runtime(tmp_path)
    class BrokenSerial:
        is_open=True
        count=0
        def write(self, frame): pass
        def read(self, size):
            self.count+=1
            if self.count==1: return bytes.fromhex('0A050070')
            raise OSError('adapter unplugged')
        def close(self): self.is_open=False
    try:
        wire=Wire(Gateway(id='usb',name='Test USB',transport='serial',serial_port='COM_TEST'), runtime.store)
        wire.client=BrokenSerial()
        unit=Unit(id='office',name='Office',gateway='usb',address=7,profile='lg-ac-smart5')
        with pytest.raises(OSError): wire.exchange(unit,5,0x70,0)
        assert any(e['raw_hex']=='0A 05 00 70' and e['details'].get('status')=='partial' for e in runtime.store.recent())
    finally: runtime.store.close()

def test_locked_start_sends_nothing_and_blocks_controls(tmp_path):
    async def run():
        mock=MockController();server=await asyncio.start_server(mock.serve,'127.0.0.1',0)
        runtime=Runtime(tmp_path)
        try:
            await runtime.configure(example_config(server.sockets[0].getsockname()[1]))
            runtime.workers['lg'].force_poll=True
            await asyncio.sleep(.2)
            assert mock.requests==[]
            with pytest.raises(PermissionError): await runtime.command('office','fan','high')
            assert mock.requests==[]
        finally:
            await runtime.stop();runtime.store.close();server.close();await server.wait_closed()
    asyncio.run(run())

def test_multiunit_write_and_readback_on_fragmented_closing_controller(tmp_path):
    async def run():
        mock=MockController(close_after_write=True,fragment=True)
        server=await asyncio.start_server(mock.serve,'127.0.0.1',0)
        runtime=Runtime(tmp_path)
        try:
            await runtime.configure(example_config(server.sockets[0].getsockname()[1],tx_enabled=True,allow_writes=True))
            runtime.workers['lg'].force_poll=True
            until=asyncio.get_running_loop().time()+10
            while not all(s['available'] for s in runtime.states.values()):
                assert asyncio.get_running_loop().time()<until
                await asyncio.sleep(.05)
            assert runtime.states['office']['values']['target']==22
            assert runtime.states['room2']['values']['target']==23
            result=await runtime.command('office','fan','low')
            assert result['results'][0]['state_verified'] is True
            assert result['results'][0]['readback']=='low'
            assert mock.registers[0x71]==1 and mock.registers[0x81]==2
            result=await runtime.command('office','target',23)
            assert result['results'][0]['readback']==23
            result=await runtime.command('office','power','OFF')
            assert result['results'][0]['state_verified'] and mock.coils[0x70]==0
            with pytest.raises(ValueError): await runtime.command('office','fan','6')
            events=runtime.store.recent(100)
            assert any(e['details'].get('request_id') for e in events if e['direction']=='rx')
            assert all(e['raw_hex'] for e in events)
            exported=[json.loads(x) for x in runtime.store.export()]
            assert len(exported)==len(events)
        finally:
            await runtime.stop();runtime.store.close();server.close();await server.wait_closed()
    asyncio.run(run())

def test_write_with_lost_ack_is_never_retried(tmp_path):
    async def run():
        requests=[]
        async def lost_ack(reader,writer):
            header=await reader.readexactly(6)
            requests.append(header+await reader.readexactly(int.from_bytes(header[4:6],'big')))
            writer.close();await writer.wait_closed()
        server=await asyncio.start_server(lost_ack,'127.0.0.1',0)
        runtime=Runtime(tmp_path)
        try:
            await runtime.configure(example_config(server.sockets[0].getsockname()[1],tx_enabled=True,allow_writes=True))
            with pytest.raises(OSError): await runtime.command('office','fan','high')
            assert len(requests)==1 and requests[0][7]==6
        finally:
            await runtime.stop();runtime.store.close();server.close();await server.wait_closed()
    asyncio.run(run())

def test_partial_reply_bytes_survive_failure(tmp_path):
    async def run():
        partial=bytes.fromhex('0001000000050A030200')
        async def broken(reader,writer):
            header=await reader.readexactly(6);await reader.readexactly(int.from_bytes(header[4:6],'big'))
            writer.write(partial);await writer.drain();writer.close();await writer.wait_closed()
        server=await asyncio.start_server(broken,'127.0.0.1',0)
        runtime=Runtime(tmp_path)
        try:
            await runtime.configure(example_config(server.sockets[0].getsockname()[1],tx_enabled=True))
            worker=runtime.workers['lg']
            with pytest.raises(OSError): await asyncio.to_thread(worker.wire.exchange,runtime.config.units[0],3,0x71,1)
            rows=runtime.store.recent()
            assert any(e['raw_hex']==partial.hex(' ').upper() and e['details'].get('status')=='partial' for e in rows)
        finally:
            await runtime.stop();runtime.store.close();server.close();await server.wait_closed()
    asyncio.run(run())

def test_mqtt_discovery_stable_and_native_entities(tmp_path):
    runtime=Runtime(tmp_path)
    try:
        config=example_config(15020)
        records=discovery_records(config,runtime.profiles)
        assert len(records)==16
        climates=[v for k,v in records.items() if '/climate/' in k]
        assert len(climates)==2 and all(c['retain'] is False for c in climates)
        assert all(c['temp_step']==1 and 'off' in c['modes'] for c in climates)
        renamed=config.model_copy(deep=True);renamed.units[0].name='New display name'
        assert records.keys()==discovery_records(renamed,runtime.profiles).keys()
    finally: runtime.store.close()

def test_http_auth_revision_and_policy_confirmation(tmp_path):
    async def run():
        runtime=Runtime(tmp_path)
        client=TestClient(TestServer(create_app(runtime,'test-token')))
        await client.start_server()
        try:
            response=await client.get('/api/config');assert response.status==401
            headers={'Authorization':'Bearer test-token','X-Bridge-Request':'1'}
            data=await (await client.get('/api/config',headers=headers)).json()
            data['configuration']['tx_enabled']=True
            response=await client.post('/api/config',json=data,headers=headers);assert response.status==403
            data['confirmation']='ENABLE_TRANSMISSION'
            response=await client.post('/api/config',json=data,headers=headers);assert response.status==200
            response=await client.post('/api/config',json=data,headers=headers);assert response.status==409
            response=await client.post('/api/config',json=data,headers={'Authorization':'Bearer test-token'});assert response.status==403
        finally: await client.close()
    asyncio.run(run())

def test_profile_update_relocks_transmission(tmp_path):
    async def run():
        runtime=Runtime(tmp_path)
        try:
            await runtime.configure(example_config(15020,tx_enabled=True,allow_writes=True))
            await runtime.import_profile(runtime.profiles['lg-ac-smart5'])
            assert not runtime.config.tx_enabled and not runtime.config.allow_writes
        finally: await runtime.stop();runtime.store.close()
    asyncio.run(run())


def test_simultaneous_settings_saves_have_one_winner(tmp_path):
    async def run():
        runtime=Runtime(tmp_path)
        client=TestClient(TestServer(create_app(runtime,'test-token')))
        await client.start_server()
        headers={'Authorization':'Bearer test-token','X-Bridge-Request':'1'}
        try:
            data=await (await client.get('/api/config',headers=headers)).json()
            first=json.loads(json.dumps(data));first['configuration']['instance_id']='first'
            second=json.loads(json.dumps(data));second['configuration']['instance_id']='second'
            responses=await asyncio.gather(client.post('/api/config',json=first,headers=headers),
                                           client.post('/api/config',json=second,headers=headers))
            assert sorted(r.status for r in responses)==[200,409]
        finally: await client.close()
    asyncio.run(run())
