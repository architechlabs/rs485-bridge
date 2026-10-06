"""Real loopback broker, discovery, commands and retained-command rejection."""
import asyncio
import json
import socket
import pytest

from rs485_bridge.runtime import Runtime
from rs485_bridge.mock import MockController
from rs485_bridge.schema import BridgeConfig, Gateway, Unit, MQTT


def test_real_broker_discovery_command_and_birth(tmp_path):
    pytest.importorskip('amqtt')
    from amqtt.broker import Broker
    import paho.mqtt.client as mqtt

    async def run():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        broker = Broker({'listeners': {'default': {'type': 'tcp', 'bind': f'127.0.0.1:{port}'}},
                         'plugins': {'amqtt.plugins.authentication.AnonymousAuthPlugin': {'allow_anonymous': True}}})
        await broker.start()
        mock = MockController()
        server = await asyncio.start_server(mock.serve, '127.0.0.1', 0)
        runtime = Runtime(tmp_path)
        messages = []
        observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='test-observer')
        observer.on_connect = lambda client, *args: client.subscribe('#', qos=1)
        observer.on_message = lambda client, userdata, msg: messages.append((msg.topic, msg.payload))
        observer.connect_async('127.0.0.1', port)
        observer.loop_start()

        async def until(condition):
            deadline = asyncio.get_running_loop().time() + 12
            while not condition():
                assert asyncio.get_running_loop().time() < deadline, messages[-5:]
                await asyncio.sleep(.05)

        try:
            await until(observer.is_connected)
            # Seed a retained control before the bridge subscribes. It must not
            # execute on connection/reconnection, even with writes authorized.
            observer.publish('rs485_bridge/site/office/command/fan', 'low', qos=1, retain=True)
            await until(lambda: any(t.endswith('/command/fan') for t, _ in messages))
            config = BridgeConfig(tx_enabled=True, allow_writes=True,
                mqtt=MQTT(enabled=True, host='127.0.0.1', port=port),
                gateways=[Gateway(id='lg', name='Loopback', host='127.0.0.1',
                    port=server.sockets[0].getsockname()[1], request_delay=.1)],
                units=[Unit(id='office', name='OFFICE', gateway='lg', address=7,
                    profile='lg-ac-smart5', control_enabled=True)])
            await runtime.configure(config)
            await until(lambda: runtime.mqtt.connected)
            await until(lambda: len({t for t, _ in messages if t.endswith('/config')}) == 8)
            await asyncio.sleep(.25)
            assert mock.requests == [], 'Retained command caused unexpected transmission'
            observer.publish('rs485_bridge/site/office/command/fan', '', qos=1, retain=True)
            runtime.workers['lg'].force_poll = True
            await until(lambda: runtime.states['office']['available'])
            await until(lambda: any(t.endswith('/office/state') for t, _ in messages))
            observer.publish('rs485_bridge/site/office/command/fan', 'low', qos=1, retain=False)
            await until(lambda: runtime.states['office']['values'].get('fan') == 'low')
            assert mock.registers[0x71] == 1
            states = [json.loads(p) for t, p in messages if t.endswith('/office/state')]
            await until(lambda: any(json.loads(p).get('fan') == 'low' for t, p in messages if t.endswith('/office/state')))
            before = sum(t.endswith('/config') for t, _ in messages)
            observer.publish('homeassistant/status', 'online', qos=1)
            await until(lambda: sum(t.endswith('/config') for t, _ in messages) >= before + 8)
            requests_before=len(mock.requests)
            await runtime.import_profile(runtime.profiles['lg-ac-smart5'])
            assert runtime.config.mqtt.enabled and runtime.config.mqtt.port==port
            assert not runtime.config.tx_enabled and not runtime.config.allow_writes
            await until(lambda: runtime.mqtt.connected)
            runtime.workers['lg'].force_poll=True
            await asyncio.sleep(.25)
            assert len(mock.requests)==requests_before
            await runtime.stop()
            await until(lambda: ('rs485_bridge/site/availability', b'offline') in messages)
        finally:
            await runtime.stop()
            runtime.store.close()
            observer.disconnect()
            await asyncio.to_thread(observer.loop_stop)
            server.close()
            await server.wait_closed()
            await broker.shutdown()
    asyncio.run(run())
