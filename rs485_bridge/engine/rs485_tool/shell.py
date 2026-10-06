"""Interactive engineering shell; protocol logic remains in reusable modules."""
from __future__ import annotations
import shlex, time, json
from pathlib import Path
import yaml
from rich.console import Console
from rich.table import Table
from .config import load_config, settings_from
from .correlator import Correlator
from .device_detection import detect_devices, format_devices
from .models import Frame, new_session_id
from .modbus import FUNCTIONS, build_request, build_write_registers, decode_frame
from .safety import SafetyGate
from .serial_manager import SerialManager
from .storage import CaptureStore
from .capture import CaptureService
from .modbus_tcp import ModbusTcpClient, decode_tcp_frame

EXCEPTION_NAMES={1:"Illegal Function",2:"Illegal Data Address",3:"Illegal Data Value",4:"Server Device Failure",5:"Acknowledge",6:"Server Device Busy",10:"Gateway Path Unavailable",11:"Gateway Target Failed to Respond"}

HELP="""Commands:
  detect | devices | use <COMx|tcp://host:502/unit-id> | config [show|set baud|parity|preset|host|unit]
  open | close | status | capture start [label] | capture stop | capture status
  frames [session] | frame <id> | decode <id> | search <hex bytes>
  read coils|discrete|holding|input <unit-id> <zero-based-address> <count>
  write coil <unit-id> <zero-based-address> on|off | write register <slave> <zero-based-address> <value> | write registers <slave> <address> <v...>
  send-hex "01 03 ..." [--dry-run|--force] | modbus-build <slave> <function> <address> <quantity/value>
  tx lock|unlock | sessions | mark "annotation" | export <session> <file.jsonl|csv|json|txt>
  replay frame <id> [--tx --force] | replay session <id> [--tx --force] [--speed N]
  observe begin "label" | observe end | diff <session-A> <session-B> | commands | save-command <name> <hex> | run <name>
  quit"""

def _number(value: str) -> int:
    try: return int(value,0)
    except ValueError: return int(value,16)

class EngineeringShell:
    def __init__(self, config_path=None, console=None):
        self.config_path=Path(config_path) if config_path else None
        self.config=load_config(config_path); self.settings=settings_from(self.config); self.console=console or Console()
        self.store=CaptureStore(self.config["database_path"]); self.gate=SafetyGate(True); self.serial=SerialManager(self.settings)
        self.network=self.config["network"];self.transport_mode="serial";self.tcp_client=None
        self.active_session=None; self.capture=None; self.correlator=Correlator(); self.observing=None
        self.console.print("[bold]RS485 Toolkit[/bold] — serial passive capture is RX only; Modbus TCP mode records this client's exchanges. TX LOCKED.")
        if self.settings.port is None:
            devices=detect_devices(); likely=[d for d in devices if d.likely_rs485]
            if len(likely)==1:
                self.settings.port=likely[0].port; self.console.print(f"Detected one likely adapter: [cyan]{self.settings.port}[/cyan] (selected; inspect with detect)")
        self.console.print("Type help. TX remains locked until 'tx unlock'.")

    def _print_frame(self, frame, error):
        if error: self.console.print(f"[red]{error}[/red]"); return
        if frame is None: return
        d=frame.decode
        self.console.print(f"{frame.timestamp_local} {frame.direction.upper():2} {frame.hex}  [{d.status}]")
        if d.valid:
            detail=f"slave {d.slave}, function {d.function:02X} {d.function_name or 'exception'}"
            if d.address is not None: detail+=f", protocol address {d.address} (zero-based)"
            if d.quantity is not None: detail+=f", quantity {d.quantity}"
            if d.values: detail+=f", values {d.values}"
            if d.exception_code is not None: detail+=f", exception {d.exception_code:02X} {EXCEPTION_NAMES.get(d.exception_code,'Unknown exception')}"
            if d.details.get("transaction_id") is not None: detail+=f", transaction ID {d.details['transaction_id']}"
            crc_text="N/A (Modbus TCP)" if d.crc_valid is None and d.details.get("transport")=="Modbus TCP" else ("valid" if d.crc_valid else "invalid")
            self.console.print("  "+detail+f"; CRC={crc_text}")

    def _require_open(self):
        if self.transport_mode=="modbus-tcp":
            if not self.network.get("host"): raise ValueError("Set controller IP with 'use tcp://192.168.29.136:502/10' or 'config set host <IP>'")
            if not self.tcp_client:self.tcp_client=ModbusTcpClient(self.network["host"],int(self.network["modbus_tcp_port"]),float(self.network["timeout"]))
            if not self.tcp_client.is_open:self.tcp_client.open()
        elif not self.serial.is_open: self.serial.open()

    def _transmit(self, raw: bytes, description: str, force=False, dangerous=False, tcp_pdu: bytes|None=None, tcp_unit_id: int|None=None):
        wire=raw
        if self.transport_mode=="modbus-tcp" and tcp_pdu is not None:
            if not self.tcp_client:self.tcp_client=ModbusTcpClient(self.network["host"],int(self.network["modbus_tcp_port"]),float(self.network["timeout"]))
            wire=self.tcp_client.prepare(tcp_pdu,int(tcp_unit_id if tcp_unit_id is not None else self.network["unit_id"]))
        target=(f"{self.network['host']}:{self.network['modbus_tcp_port']} Unit ID {tcp_unit_id if tcp_unit_id is not None else self.network['unit_id']}" if self.transport_mode=="modbus-tcp" else (self.settings.port or "(unset)"))
        self.console.print(f"[bold yellow]TX {'WRITE' if dangerous else 'FRAME'}[/bold yellow]\n{description}\nTarget: {target}\nRaw: {wire.hex(' ').upper()}")
        if self.gate.locked: raise PermissionError("TX LOCKED. Run 'tx unlock' to explicitly enable transmission.")
        if dangerous and not force and input("Type CONFIRM to transmit this write: ").strip()!="CONFIRM":
            self.console.print("Cancelled."); return
        if not dangerous and not force and input("Transmit? Type CONFIRM: ").strip()!="CONFIRM":
            self.console.print("Cancelled."); return
        self._require_open()
        frame_port=f"tcp://{self.network['host']}:{self.network['modbus_tcp_port']}" if self.transport_mode=="modbus-tcp" else self.settings.port
        frame=Frame(raw=wire,session_id=self.active_session or new_session_id(),port=frame_port,
                    baudrate=self.settings.baudrate if self.transport_mode == "serial" else None,
                    bytesize=self.settings.bytesize if self.transport_mode == "serial" else None,
                    parity=self.settings.parity if self.transport_mode == "serial" else None,
                    stopbits=self.settings.stopbits if self.transport_mode == "serial" else None,direction="tx")
        frame.decode=decode_tcp_frame(wire) if self.transport_mode=="modbus-tcp" else decode_frame(wire); frame.parser_status=frame.decode.status
        if not self.active_session:
            session_metadata = {}
            if self.transport_mode == "modbus-tcp":
                session_metadata = {
                    "host": self.network["host"],
                    "tcp_port": int(self.network["modbus_tcp_port"]),
                    "unit_id": int(tcp_unit_id if tcp_unit_id is not None else self.network["unit_id"]),
                }
            self.store.create_session(frame.session_id,self.settings,"manual TX",
                                      transport=self.transport_mode,endpoint=frame_port,
                                      metadata=session_metadata)
        self.store.add_frame(frame)
        if self.transport_mode=="modbus-tcp":
            self._print_frame(frame,None)
            if tcp_pdu is None:
                self.tcp_client.sock.sendall(wire)
                self.console.print("Raw TCP bytes sent exactly; no MBAP wrapping or response parsing was added.")
                return
            self.correlator.add(frame)
            try: response_raw=self.tcp_client.exchange(wire)
            except Exception as exc:
                self.console.print(f"No valid Modbus TCP response: {exc}");return
            response=Frame(raw=response_raw,session_id=frame.session_id,port=frame_port,baudrate=frame.baudrate,
                           bytesize=frame.bytesize,parity=frame.parity,stopbits=frame.stopbits,direction="rx")
            response.decode=decode_tcp_frame(response_raw);response.parser_status=response.decode.status;self.store.add_frame(response);self._print_frame(response,None)
            match=self.correlator.add(response)
            if match:
                self.store.correlate(match.request_id,match.response_id,match.round_trip_seconds,match.confidence)
                self.console.print(f"Request/response match: RTT={match.round_trip_seconds} s ({match.confidence})")
            return
        self.serial.write(wire); self._print_frame(frame,None)
        self.correlator.add(frame)
        deadline=time.monotonic()+self.settings.receive_timeout; data=bytearray(); last=None
        while time.monotonic()<deadline:
            chunk=self.serial.read(256)
            if chunk:
                data.extend(chunk); last=time.monotonic()
            elif data and last and time.monotonic()-last>=self.settings.frame_timeout: break
        if data:
            response=Frame(raw=bytes(data),session_id=frame.session_id,port=self.settings.port,baudrate=self.settings.baudrate,
                           bytesize=self.settings.bytesize,parity=self.settings.parity,stopbits=self.settings.stopbits,direction="rx")
            response.decode=decode_frame(response.raw); response.parser_status=response.decode.status; self.store.add_frame(response); self._print_frame(response,None)
            match=self.correlator.add(response)
            if match:
                self.store.correlate(match.request_id,match.response_id,match.round_trip_seconds,match.confidence)
                self.console.print(f"Request/response match: RTT={match.round_trip_seconds} s ({match.confidence})")
        else: self.console.print("No response received before timeout; request is stored. Check wiring, slave address, baud/parity, and bus master/slave configuration.")

    def execute(self, line: str):
        args=shlex.split(line)
        if not args: return True
        cmd=args.pop(0).lower()
        try:
            if cmd in ("quit","exit"): return False
            if cmd in ("help","?"): self.console.print(HELP)
            elif cmd in ("detect","devices"): self.console.print(format_devices(detect_devices()))
            elif cmd in ("use","select"):
                selected=args[0]
                if self.serial.is_open:self.serial.close()
                if self.tcp_client:self.tcp_client.close();self.tcp_client=None
                if selected.startswith("tcp://"):
                    from urllib.parse import urlsplit
                    url=urlsplit(selected);self.transport_mode="modbus-tcp";self.network["host"]=url.hostname
                    if url.port:self.network["modbus_tcp_port"]=url.port
                    if url.path.strip("/"):self.network["unit_id"]=int(url.path.strip("/"),0)
                    if not self.network["host"]:raise ValueError("TCP target must include an IP/hostname")
                    self.config["network"]=self.network;self._save_config()
                    self.console.print(f"Selected Modbus TCP {self.network['host']}:{self.network['modbus_tcp_port']} Unit ID {self.network['unit_id']} (TX LOCKED)")
                else:
                    self.transport_mode="serial";self.settings.port=selected;self.serial.settings=self.settings;self.config["serial"]["port"]=self.settings.port;self._save_config();self.console.print(f"Selected serial {self.settings.port}")
            elif cmd=="config":
                if not args or args[0]=="show": self.console.print(f"transport={self.transport_mode}; serial={self.settings}; network={self.network}")
                elif args[0]=="set" and len(args)>=3:
                    key,val=args[1],args[2]
                    if key=="baud": self.settings.baudrate=int(val)
                    elif key=="parity":
                        if val.upper() not in ("N","E","O"): raise ValueError("parity must be N, E, or O")
                        self.settings.parity=val.upper()
                    elif key=="frame-timeout": self.settings.frame_timeout=float(val)
                    elif key=="rs485": self.settings.rs485_mode=val.lower() in ("1","true","yes","on")
                    elif key=="host": self.network["host"]=val;self.config["network"]=self.network;self._save_config()
                    elif key=="unit": self.network["unit_id"]=int(val,0);self.config["network"]=self.network;self._save_config()
                    elif key=="tcp-port": self.network["modbus_tcp_port"]=int(val,0);self.config["network"]=self.network;self._save_config()
                    else: raise ValueError("editable settings: baud, parity, frame-timeout, rs485, host, unit, tcp-port")
                    self.serial.settings=self.settings
                    self.config["serial"].update({"baudrate":self.settings.baudrate,"parity":self.settings.parity,"frame_timeout":self.settings.frame_timeout,"rs485_mode":self.settings.rs485_mode});self._save_config()
                elif args[0]=="preset":
                    self.settings=type(self.settings).preset(args[1],port=self.settings.port); self.serial.settings=self.settings
                    self.config["serial"].update({"baudrate":self.settings.baudrate,"parity":self.settings.parity});self._save_config()
                else: self.console.print("Usage: config show | config set baud 19200 | config preset 9600_8N1")
            elif cmd=="open":
                self._require_open()
                self.console.print(f"Opened Modbus TCP {self.network['host']}:{self.network['modbus_tcp_port']}" if self.transport_mode=="modbus-tcp" else f"Opened {self.settings.port} at {self.settings.baudrate} {self.settings.bytesize}{self.settings.parity}{self.settings.stopbits}")
            elif cmd=="close":
                if self.capture: self.capture.stop(); self.capture=None
                else: self.serial.close()
                if self.tcp_client:self.tcp_client.close();self.tcp_client=None
                self.console.print("Closed.")
            elif cmd=="status": self.console.print(f"transport={self.transport_mode} target={(str(self.network['host'])+':'+str(self.network['modbus_tcp_port'])) if self.transport_mode=='modbus-tcp' else (self.settings.port or '(unset)')} baud={self.settings.baudrate} parity={self.settings.parity} open={(self.tcp_client.is_open if self.transport_mode=='modbus-tcp' and self.tcp_client else self.serial.is_open)} capture={bool(self.capture and self.capture.running)} TX={'LOCKED' if self.gate.locked else 'UNLOCKED'}")
            elif cmd=="tx":
                if args[0]=="lock": self.gate.lock(); self.console.print("TX LOCKED")
                elif args[0]=="unlock": self.gate.unlock(); self.console.print("TX UNLOCKED — transmissions still require per-operation confirmation")
            elif cmd=="capture":
                if args[0]=="start":
                    if self.transport_mode=="modbus-tcp": raise RuntimeError("This TCP client records its own request/response exchanges; it cannot passively sniff other Ethernet traffic. Use Wireshark/Npcap with switch port mirroring for passive network capture.")
                    if self.capture and self.capture.running: raise RuntimeError("capture already running")
                    self.active_session=new_session_id(); self.capture=CaptureService(self.serial,self.store,self.settings,self.active_session,self._print_frame).start()
                    self.console.print(f"Passive RX capture started: {self.active_session}. No TX is performed. If no traffic appears, check port ownership, wiring/A-B polarity, baud/parity, polling master, interface configuration, and adapter driver.")
                elif args[0]=="stop":
                    if self.capture: self.capture.stop(); self.capture=None; self.console.print("Capture stopped and flushed to SQLite.")
                elif args[0]=="status": self.console.print(f"capture={'running' if self.capture and self.capture.running else 'stopped'} session={self.active_session}")
            elif cmd=="read":
                typ,slave,address,count=args[0],int(args[1],0),int(args[2],0),int(args[3],0)
                fn={"coils":1,"discrete":2,"holding":3,"input":4}.get(typ)
                if fn is None: raise ValueError("read supports coils, discrete, holding, or input")
                raw=build_request(slave,fn,address,count)
                self._transmit(raw,f"Slave/Unit ID {slave}; {FUNCTIONS[fn]}; protocol address {address} (zero-based); quantity {count}",tcp_pdu=(raw[1:-2] if self.transport_mode=="modbus-tcp" else None),tcp_unit_id=slave)
            elif cmd=="write":
                if args[0]=="coil":
                    if len(args)<4: raise ValueError("usage: write coil <unit-id> <zero-based-address> on|off [--force]")
                    slave,address=map(lambda x:int(x,0),args[1:3]);state=args[3].lower()
                    if state not in ("on","off"): raise ValueError("coil state must be 'on' or 'off'")
                    value=0xFF00 if state=="on" else 0
                    raw=build_request(slave,5,address,value)
                    self._transmit(raw,f"DANGEROUS WRITE; Unit ID {slave}; function 05 Write Single Coil; protocol address {address} (zero-based); state {state.upper()} (value 0x{value:04X})",force="--force" in args,dangerous=True,tcp_pdu=(raw[1:-2] if self.transport_mode=="modbus-tcp" else None),tcp_unit_id=slave)
                elif args[0]=="register":
                    slave,address,value=map(lambda x:int(x,0),args[1:4]); raw=build_request(slave,6,address,value)
                    self._transmit(raw,f"Slave/Unit ID {slave}; function 06; protocol address {address}; value {value}",force="--force" in args,dangerous=True,tcp_pdu=(raw[1:-2] if self.transport_mode=="modbus-tcp" else None),tcp_unit_id=slave)
                elif args[0]=="registers":
                    slave,address=map(lambda x:int(x,0),args[1:3]); vals=list(map(lambda x:int(x,0),[x for x in args[3:] if x!="--force"])); raw=build_write_registers(slave,address,vals)
                    self._transmit(raw,f"Slave/Unit ID {slave}; function 10; protocol address {address}; values {vals}",force="--force" in args,dangerous=True,tcp_pdu=(raw[1:-2] if self.transport_mode=="modbus-tcp" else None),tcp_unit_id=slave)
            elif cmd=="send-hex":
                force="--force" in args; dry="--dry-run" in args; hexpart=" ".join(a for a in args if not a.startswith("--")); raw=bytes.fromhex(hexpart)
                if dry: self.console.print(f"DRY RUN: {raw.hex(' ').upper()}")
                else: self._transmit(raw,"Exact raw bytes; CRC untouched",force=force)
            elif cmd=="modbus-build":
                raw=build_request(*(int(x,0) for x in args[:4])); self.console.print(raw.hex(" ").upper())
            elif cmd=="frames":
                rows=self.store.frames(args[0] if args else None); table=Table("ID","UTC","Dir","Bytes","Decode")
                for r in rows: table.add_row(str(r["id"]),r["timestamp_utc"],r["direction"],bytes(r["raw"]).hex(" ").upper(),r["parser_status"] or "")
                self.console.print(table)
            elif cmd in ("frame","decode"):
                row=self.store.db.execute("SELECT * FROM frames WHERE id=?",(int(args[0]),)).fetchone()
                if not row: raise ValueError("frame ID not found")
                f=Frame(raw=bytes(row["raw"]),timestamp_utc=row["timestamp_utc"],timestamp_local=row["timestamp_local"],direction=row["direction"]);f.decode=decode_frame(f.raw);self._print_frame(f,None)
            elif cmd=="sessions":
                for s in self.store.sessions(): self.console.print(f"{s['id']} {s['started_utc']} {s['port']}")
            elif cmd=="export": self.console.print(self.store.export(args[0],args[1]))
            elif cmd=="mark":
                if not self.active_session: raise ValueError("start capture before annotating")
                self.store.add_annotation(self.active_session," ".join(args)); self.console.print("Annotation saved.")
            elif cmd=="observe":
                if args[0]=="begin":
                    if not self.active_session: raise ValueError("start capture before observe begin")
                    self.observing=(self.active_session," ".join(args[1:]),self.store.db.execute("SELECT COALESCE(MAX(id),0) FROM frames WHERE session_id=?",(self.active_session,)).fetchone()[0]); self.console.print(f"Observation begun: {self.observing[1]}")
                elif args[0]=="end":
                    if not self.observing: raise ValueError("no observation is active")
                    sid,label,start_id=self.observing
                    before=[bytes(r["raw"]) for r in self.store.db.execute("SELECT raw FROM frames WHERE session_id=? AND id<=?",(sid,start_id))]
                    after=[bytes(r["raw"]) for r in self.store.db.execute("SELECT raw FROM frames WHERE session_id=? AND id>?",(sid,start_id))]
                    from collections import Counter
                    old,new=Counter(before),Counter(after)
                    self.console.print(f"Observation ended: {label}; before={len(before)} after={len(after)} identical={sum((old&new).values())} new={sum((new-old).values())} removed={sum((old-new).values())}")
                    for a in old-new:
                        for b in new-old:
                            if len(a)==len(b):
                                changes=[f"byte {i}: {x:02X}->{y:02X}" for i,(x,y) in enumerate(zip(a,b)) if x!=y]
                                if changes:
                                    self.console.print(f"Candidate: {a.hex(' ').upper()} => {b.hex(' ').upper()} ({', '.join(changes)})")
                                    da,db=decode_frame(a),decode_frame(b)
                                    if da.valid and db.valid and da.values!=db.values:self.console.print(f"  Decoded payload values changed: {da.values} -> {db.values}; register mapping remains unverified")
                    self.store.add_annotation(sid,label+" (observation boundary)")
                    self.observing=None
            elif cmd=="diff": self._diff(args[0],args[1])
            elif cmd=="replay": self._replay(args)
            elif cmd=="save-command":
                name=args[0]; raw=bytes.fromhex(" ".join(args[1:])); self.store.db.execute("INSERT OR REPLACE INTO commands(name,raw,created_utc) VALUES(?,?,datetime('now'))",(name,raw));self.store.db.commit();self.console.print(f"Saved {name}")
            elif cmd=="commands":
                for r in self.store.db.execute("SELECT name,raw FROM commands"): self.console.print(f"{r['name']} {bytes(r['raw']).hex(' ').upper()}")
            elif cmd=="run":
                row=self.store.db.execute("SELECT raw FROM commands WHERE name=?",(args[0],)).fetchone()
                if not row: raise ValueError("unknown command")
                raw=bytes(row[0]);is_rtu=decode_frame(raw).valid
                self._transmit(raw,f"Saved command {args[0]}",dangerous=True,tcp_pdu=(raw[1:-2] if self.transport_mode=="modbus-tcp" and is_rtu else None),tcp_unit_id=(raw[0] if self.transport_mode=="modbus-tcp" and is_rtu else None))
            elif cmd=="probe":
                if args and args[0]=="slave":
                    if len(args)<2: raise ValueError("usage: probe slave <id> [zero-based-address]")
                    slave=int(args[1],0);address=int(args[2],0) if len(args)>2 else 0
                    self._transmit(build_request(slave,3,address,1),f"READ-ONLY probe; slave {slave}; holding register protocol address {address}; quantity 1")
                elif args and args[0]=="baud": self._probe_baud()
                else: self.console.print("Usage: probe baud (passive only) | probe slave <id> [zero-based-address] (explicit read-only TX). No broad address scan is performed.")
            elif cmd=="discover": self.console.print("Passive inspection: start capture and operate the controller. Active discovery requires a known slave/address and must be explicitly issued with a read command.")
            elif cmd=="search": self._search(" ".join(args))
            else: self.console.print("Unknown command. Type help.")
        except Exception as exc: self.console.print(f"[red]Error:[/red] {exc}")
        return True

    def _search(self, needle):
        bits=needle.split()
        if len(bits)==2 and bits[0].lower() in ("slave","function"):
            key,value=bits[0].lower(),_number(bits[1])
            for r in self.store.frames():
                d=json.loads(r["decode_json"]) if r["decode_json"] else None
                if d and d.get(key)==value:self.console.print(f"{r['id']} {r['timestamp_utc']} {bytes(r['raw']).hex(' ').upper()}")
            return
        try: raw=bytes.fromhex(needle)
        except ValueError: raw=needle.encode()
        for r in self.store.frames():
            if raw in bytes(r["raw"]): self.console.print(f"{r['id']} {r['timestamp_utc']} {bytes(r['raw']).hex(' ').upper()}")
    def _diff(self,a,b):
        left=[bytes(r["raw"]) for r in self.store.frames(a)];right=[bytes(r["raw"]) for r in self.store.frames(b)]
        from collections import Counter
        l,r=Counter(left),Counter(right);self.console.print(f"identical={sum((l&r).values())} removed={sum((l-r).values())} new={sum((r-l).values())}")
        for old in l-r:
            for new in r-l:
                if len(old)==len(new):
                    diffs=[f"{i}:{x:02X}->{y:02X}" for i,(x,y) in enumerate(zip(old,new)) if x!=y]
                    self.console.print(f"candidate changed {old.hex(' ').upper()} => {new.hex(' ').upper()} bytes {', '.join(diffs)}")
    def _replay(self,args):
        force="--force" in args; tx="--tx" in args; speed=1.0
        if "--speed" in args: speed=float(args[args.index("--speed")+1])
        if args[0]=="frame": rows=list(self.store.db.execute("SELECT * FROM frames WHERE id=?",(int(args[1]),)))
        elif args[0]=="session": rows=self.store.frames(args[1])
        else: raise ValueError("replay frame <id> or replay session <id>")
        if not rows: raise ValueError("no stored frame(s)")
        include_tx="--include-tx" in args
        captured_tx=[row for row in rows if row["direction"]=="tx"]
        if args[0]=="session" and captured_tx and not include_tx:
            rows=[row for row in rows if row["direction"]!="tx"]
            self.console.print(f"Excluded {len(captured_tx)} captured tool-TX frame(s). Add --include-tx to review them explicitly.")
        if not rows: raise ValueError("no replayable received frames in this session")
        for row in rows: self.console.print("TX FRAME ("+("actual" if tx else "dry-run")+") "+bytes(row["raw"]).hex(" ").upper())
        if tx:
            if not force and input("Type REPLAY to transmit these exact bytes (including any explicitly included TX frames): ").strip()!="REPLAY": return
            self.gate.require_tx(); self._require_open()
            previous=None
            for row in rows:
                if previous:
                    from datetime import datetime
                    delay=max(0,(datetime.fromisoformat(row["timestamp_utc"])-datetime.fromisoformat(previous)).total_seconds())/speed;time.sleep(min(delay,10))
                self.serial.write(bytes(row["raw"]));previous=row["timestamp_utc"]
        else: self.console.print("Dry-run only; add --tx --force after tx unlock for actual transmission.")

    def _save_config(self):
        path=self.config_path or (Path.home()/".rs485-tool"/"config.yaml")
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(yaml.safe_dump(self.config,sort_keys=False),encoding="utf8")

    def _probe_baud(self):
        if not self.settings.port: raise ValueError("select adapter first")
        if self.capture and self.capture.running: raise ValueError("stop capture before probing baud rates")
        from collections import Counter
        from .framing import SilenceFramer
        from .models import new_session_id
        original=self.settings.baudrate;session=new_session_id();results={}
        self.store.create_session(session,self.settings,"passive baud-rate observation")
        for baud in (9600,19200,38400,57600,115200):
            self.serial.close();self.settings.baudrate=baud;self.serial.settings=self.settings;self.serial.open()
            framer=SilenceFramer(self.settings.frame_timeout);counts=Counter();deadline=time.monotonic()+.75
            while time.monotonic()<deadline:
                data=self.serial.read(256);now=time.monotonic()
                chunks=framer.feed(data,now) if data else framer.flush_if_idle(now)
                for raw,gap in chunks:
                    d=decode_frame(raw);counts[d.status]+=1
                    f=Frame(raw=raw,session_id=session,port=self.settings.port,baudrate=baud,bytesize=self.settings.bytesize,parity=self.settings.parity,stopbits=self.settings.stopbits,direction="rx",decode=d,parser_status=d.status,previous_byte_gap=gap);self.store.add_frame(f)
            for raw,gap in framer.flush():
                d=decode_frame(raw);counts[d.status]+=1
                f=Frame(raw=raw,session_id=session,port=self.settings.port,baudrate=baud,bytesize=self.settings.bytesize,parity=self.settings.parity,stopbits=self.settings.stopbits,direction="rx",decode=d,parser_status=d.status,previous_byte_gap=gap);self.store.add_frame(f)
            results[baud]=dict(counts);self.serial.close()
        self.settings.baudrate=original;self.serial.settings=self.settings
        self.console.print("Passive only; no test requests transmitted. Traffic absent at every rate is inconclusive (no master polling is a common cause).")
        for baud,result in results.items(): self.console.print(f"{baud}: {result}")

    def run(self):
        try:
            while True:
                try: line=input("rs485> ")
                except EOFError: break
                if not self.execute(line): break
        finally:
            if self.capture: self.capture.stop()
            else: self.serial.close()
            self.store.close()
