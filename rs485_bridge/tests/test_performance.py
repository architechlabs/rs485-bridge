"""Responsive controls retain serialization, readback and write safety."""
import asyncio
from types import SimpleNamespace
import pytest
from rs485_bridge.runtime import Runtime
from rs485_bridge.schema import BridgeConfig,Gateway,Unit
from rs485_bridge.mock import MockController
from rs485_bridge.transport import Wire
from rs485_bridge.mqtt import MQTTBridge

def config(port,**kwargs):
    return BridgeConfig(tx_enabled=True,allow_writes=True,
        gateways=[Gateway(id='lg',name='Mock',host='127.0.0.1',port=port,request_delay=.3,
            connect_delay=.1,command_delay=.05,command_connect_delay=0,**kwargs)],
        units=[Unit(id='office',name='Office',gateway='lg',address=7,profile='lg-ac-smart5',control_enabled=True)])

def test_fast_command_pacing_does_not_change_poll_pacing(tmp_path,monkeypatch):
    import rs485_bridge.transport as module
    now=[100.0];sleeps=[]
    monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    def sleep(delay): sleeps.append(round(delay,4));now[0]+=delay
    monkeypatch.setattr(module.time,'sleep',sleep)
    class Sock:
        def sendall(self,request):
            body=request[6:8]+bytes([2,0,22]) if request[7]==3 else request[6:]
            self.response=request[:4]+len(body).to_bytes(2,'big')+body
        def recv(self,size):
            chunk=self.response[:size];self.response=self.response[size:];return chunk
        def settimeout(self,value): pass
        def close(self): pass
    runtime=Runtime(tmp_path)
    try:
        gateway=Gateway(id='lg',name='Mock',host='mock',request_delay=.3,connect_delay=.1,command_delay=.08,command_connect_delay=.02)
        wire=Wire(gateway,runtime.store)
        wire.client.open=lambda:setattr(wire.client,'sock',Sock())
        unit=Unit(id='office',name='Office',gateway='lg',profile='lg-ac-smart5',address=7)
        wire.last_tx=now[0]
        wire.exchange(unit,3,0x72,1,'poll')
        assert sleeps==[.3,.1]
        sleeps.clear()
        wire.exchange(unit,6,0x72,24,'api','command-id')
        assert sleeps==[.08,.02]
        assert runtime.store.recent()[0]['details']['command_id']=='command-id'
    finally:runtime.store.close()

def test_commands_wake_idle_worker_and_benchmark_is_read_only(tmp_path):
    async def run():
        mock=MockController();server=await asyncio.start_server(mock.serve,'127.0.0.1',0)
        runtime=Runtime(tmp_path)
        try:
            await runtime.configure(config(server.sockets[0].getsockname()[1]))
            await asyncio.sleep(.05)
            result=await runtime.command('office','fan','low')
            assert result['results'][0]['state_verified']
            assert result['metrics']['command_status']=='verified'
            assert result['metrics']['queue_wait_ms']<350
            assert result['metrics']['command_latency_ms']<700
            assert [r[7] for r in mock.requests]==[6,3]
            before=len(mock.requests)
            benchmark=await runtime.benchmark('lg')
            assert benchmark['writes_sent']==0 and len(benchmark['samples'])==4
            assert all(r[7]==3 for r in mock.requests[before:])
            assert runtime.config.gateways[0].request_delay==.3
        finally:await runtime.stop();runtime.store.close();server.close();await server.wait_closed()
    asyncio.run(run())

def test_delayed_readback_retries_reads_never_writes(tmp_path):
    async def run():
        requests=[];reads=0
        async def delayed(reader,writer):
            nonlocal reads
            try:
                header=await reader.readexactly(6);body=await reader.readexactly(int.from_bytes(header[4:6],'big'))
                requests.append(body[1])
                if body[1]==6:reply=body
                else:
                    reads+=1
                    reply=bytes([10,3,2,0,3 if reads==1 else 1])
                writer.write(header[:4]+len(reply).to_bytes(2,'big')+reply);await writer.drain()
            finally:writer.close();await writer.wait_closed()
        server=await asyncio.start_server(delayed,'127.0.0.1',0)
        runtime=Runtime(tmp_path)
        try:
            await runtime.configure(config(server.sockets[0].getsockname()[1],readback_attempts=3,readback_delay=.05))
            result=await runtime.command('office','fan','low')
            assert result['results'][0]['state_verified'] and requests==[6,3,3]
            assert runtime.states['office']['values']['fan']=='low'
        finally:await runtime.stop();runtime.store.close();server.close();await server.wait_closed()
    asyncio.run(run())

def test_compound_control_publishes_each_verified_point(tmp_path):
    async def run():
        mock=MockController();mock.coils[0x70]=0
        server=await asyncio.start_server(mock.serve,'127.0.0.1',0);runtime=Runtime(tmp_path)
        try:
            await runtime.configure(config(server.sockets[0].getsockname()[1]))
            runtime.states['office']['values'].update(power='OFF',mode='cool')
            observed=[];original=runtime.publish_unit
            def publish(unit_id):
                observed.append((dict(runtime.states[unit_id]['values']),[r[7] for r in mock.requests]))
                original(unit_id)
            runtime.publish_unit=publish
            result=await runtime.command('office','hvac','heat')
            assert all(r['state_verified'] for r in result['results'])
            assert any(values.get('mode')=='heat' and values.get('power')=='OFF' and 5 not in functions for values,functions in observed)
            assert runtime.states['office']['values']['power']=='ON'
        finally:await runtime.stop();runtime.store.close();server.close();await server.wait_closed()
    asyncio.run(run())

def test_mqtt_does_not_resend_unchanged_availability(tmp_path):
    async def run():
        runtime=Runtime(tmp_path);runtime.config=config(15020)
        bridge=MQTTBridge(runtime);messages=[]
        class Client:
            def publish(self,topic,payload,**kwargs):messages.append((topic,payload));return SimpleNamespace(rc=0)
        bridge.client=Client();bridge.connected=True
        try:
            state={'available':True,'values':{'fan':'high'},'point_availability':{'fan':True}}
            bridge.publish('office',state);count=len(messages)
            bridge.publish('office',state);assert len(messages)==count
            state['values']['fan']='low';bridge.publish('office',state)
            assert len(messages)==count+1 and messages[-1][0].endswith('/state')
        finally:runtime.store.close()
    asyncio.run(run())
