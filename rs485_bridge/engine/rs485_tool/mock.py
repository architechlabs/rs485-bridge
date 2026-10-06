"""Socket-backed Modbus slave for exercising the CLI without COM hardware."""
from __future__ import annotations
import argparse, socket, threading, time
from .crc import append_crc, check_crc

class MockModbusServer:
    def __init__(self, host="127.0.0.1",port=5020,delay=0.03,crc_error=False,malformed=False):
        self.host,self.port,self.delay,self.crc_error,self.malformed=host,port,delay,crc_error,malformed
        self.registers={i:(i*3+22) for i in range(256)}; self.stop_event=threading.Event(); self.server=None
    def serve_forever(self):
        self.server=socket.socket(); self.server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); self.server.bind((self.host,self.port)); self.server.listen()
        self.server.settimeout(.5); print(f"Mock Modbus server listening at {self.host}:{self.port}; protocol: mock://{self.host}:{self.port}")
        while not self.stop_event.is_set():
            try: client,_=self.server.accept()
            except socket.timeout: continue
            except OSError:
                if self.stop_event.is_set(): break
                raise
            threading.Thread(target=self._client,args=(client,),daemon=True).start()
    def _client(self,client):
        with client:
            buf=bytearray(); client.settimeout(.5)
            while not self.stop_event.is_set():
                try: chunk=client.recv(256)
                except socket.timeout: continue
                except OSError: break
                if not chunk: break
                buf.extend(chunk)
                while len(buf)>=8:
                    request=bytes(buf[:8]); del buf[:8]
                    if not check_crc(request): continue
                    time.sleep(self.delay); response=self.respond(request)
                    try: client.sendall(response)
                    except OSError: return
    def respond(self,request: bytes)->bytes:
        slave,fn=request[0],request[1]; addr=int.from_bytes(request[2:4],"big"); amount=int.from_bytes(request[4:6],"big")
        if fn in (3,4):
            vals=[self.registers.get(addr+i,0) for i in range(min(amount,125))]
            body=bytes((slave,fn,len(vals)*2))+b"".join(v.to_bytes(2,"big") for v in vals)
        elif fn==6:
            self.registers[addr]=amount; body=request[:-2]
        else: body=bytes((slave,fn|0x80,1))
        response=append_crc(body)
        if self.malformed: return response[:-1]
        if self.crc_error: return response[:-1]+bytes((response[-1]^0xFF,))
        return response
    def stop(self):
        self.stop_event.set()
        if self.server: self.server.close()

def main():
    p=argparse.ArgumentParser();p.add_argument("--host",default="127.0.0.1");p.add_argument("--port",type=int,default=5020);p.add_argument("--delay",type=float,default=.03);p.add_argument("--crc-error",action="store_true");p.add_argument("--malformed",action="store_true");a=p.parse_args()
    try: MockModbusServer(a.host,a.port,a.delay,a.crc_error,a.malformed).serve_forever()
    except KeyboardInterrupt: pass

if __name__=="__main__": main()
