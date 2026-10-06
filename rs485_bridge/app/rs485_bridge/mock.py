"""Real Modbus TCP mock; local-only by default. No connection to real HVAC."""
import argparse
import asyncio
from .schema import BridgeConfig, Gateway, Unit, atomic_json
from pathlib import Path

class MockController:
    def __init__(self, close_after_write=False, fragment=False):
        self.coils = {0x70:1, 0x80:0, 0x76:0, 0x86:0}
        self.registers = {0x70:1, 0x71:3, 0x72:22, 0x75:26, 0x76:0,
                          0x80:1, 0x81:2, 0x82:23, 0x85:25, 0x86:0}
        self.close_after_write = close_after_write
        self.fragment = fragment
        self.requests = []

    async def serve(self, reader, writer):
        try:
            while True:
                header = await reader.readexactly(6)
                length = int.from_bytes(header[4:6], 'big')
                if not 2 <= length <= 254:
                    break
                tail = await reader.readexactly(length)
                unit, function = tail[0], tail[1]
                self.requests.append(header + tail)
                if len(tail) != 6:
                    response = bytes([function | 128, 3])
                else:
                    address, value = int.from_bytes(tail[2:4],'big'), int.from_bytes(tail[4:6],'big')
                    response = self.respond(function, address, value)
                packet = header[:4] + (len(response)+1).to_bytes(2,'big') + bytes([unit]) + response
                if self.fragment:
                    for byte in packet:
                        writer.write(bytes([byte]))
                        await writer.drain()
                        await asyncio.sleep(.001)
                else:
                    writer.write(packet)
                    await writer.drain()
                if self.close_after_write and function in (5,6):
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    def respond(self, fn, address, value):
        def error(code): return bytes([fn|128,code])
        if fn in (1,3):
            collection = self.coils if fn==1 else self.registers
            if not 1 <= value <= (2000 if fn==1 else 125): return error(3)
            if any(address+i not in collection for i in range(value)): return error(2)
            values = [collection[address+i] for i in range(value)]
            if fn == 3:
                raw = b''.join(v.to_bytes(2,'big') for v in values)
            else:
                data = bytearray((value+7)//8)
                for i,v in enumerate(values): data[i//8] |= v << (i%8)
                raw = bytes(data)
            return bytes([fn,len(raw)]) + raw
        if fn == 5:
            if address not in self.coils: return error(2)
            if value not in (0,65280): return error(3)
            self.coils[address] = int(bool(value))
        elif fn == 6:
            if address not in self.registers: return error(2)
            if address & 15 == 1 and value not in (1,2,3,4): return error(3)
            if address & 15 == 2 and not 16 <= value <= 30: return error(3)
            if address & 15 in (5,6): return error(2)
            self.registers[address] = value
        else:
            return error(1)
        return bytes([fn]) + address.to_bytes(2,'big') + value.to_bytes(2,'big')

async def launch(args):
    controller = MockController(args.close_after_write, args.fragment)
    server = await asyncio.start_server(controller.serve, args.host, args.port)
    if args.create_demo:
        config = BridgeConfig(gateways=[Gateway(id='demo', name='Local mock', host=args.host, port=args.port)],
            units=[Unit(id='office',name='OFFICE · demo',gateway='demo',address=7,profile='lg-ac-smart5'),
                   Unit(id='second',name='Second room · demo',gateway='demo',address=8,profile='lg-ac-smart5')])
        path = args.create_demo / 'settings.json'
        if path.exists():
            raise ValueError('Demo settings already exist; use a new data directory to preserve your settings')
        atomic_json(path, config.model_dump())
        print(f'Demo configured at {path}. TX remains locked; enable polling explicitly in the UI.')
    print(f'Mock controller listening on {args.host}:{args.port}')
    async with server:
        await server.serve_forever()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=15020)
    parser.add_argument('--close-after-write',action='store_true')
    parser.add_argument('--fragment',action='store_true')
    parser.add_argument('--create-demo',type=Path)
    asyncio.run(launch(parser.parse_args()))

if __name__ == '__main__':
    main()
