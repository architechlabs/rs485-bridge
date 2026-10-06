"""Single-owner, serialized wire transactions with raw capture and bounded retries."""
from __future__ import annotations
import time
from dataclasses import asdict
from rs485_tool.models import SerialSettings
from rs485_tool.serial_manager import SerialManager
from rs485_tool.modbus_tcp import ModbusTcpClient, decode_tcp_frame
from rs485_tool.modbus import build_request, decode_frame
from .schema import Gateway

class ModbusError(OSError):
    pass

class Wire:
    def __init__(self, gateway: Gateway, store):
        self.gateway, self.store = gateway, store
        self.last_tx = 0.0
        self.client = (ModbusTcpClient(gateway.host, gateway.port, gateway.timeout) if gateway.transport == "tcp" else
            SerialManager(SerialSettings(port=gateway.serial_port, baudrate=gateway.baudrate,
                bytesize=gateway.bytesize, parity=gateway.parity, stopbits=gateway.stopbits,
                timeout=min(0.01, gateway.frame_gap), receive_timeout=gateway.timeout,
                frame_timeout=gateway.frame_gap, rs485_mode=gateway.rs485_mode)))

    def close(self):
        self.client.close()

    def exchange(self, unit, function, address, value, source="poll"):
        is_write = function in (5, 6)
        # A read may be retried once on a fresh connection. Writes are never
        # retried when an acknowledgement was lost.
        attempts = 1 if is_write else 2
        for attempt in range(attempts):
            try:
                return self._once(unit, function, address, value, source)
            except ModbusError:
                self.close()
                raise
            except (OSError, TimeoutError):
                self.close()
                if attempt + 1 == attempts:
                    raise

    def _once(self, unit, function, address, value, source):
        gateway = self.gateway
        time.sleep(max(0, gateway.request_delay - (time.monotonic() - self.last_tx)))
        rtu = build_request(unit.slave_id, function, address, value)
        if not self.client.is_open:
            self.client.open()
        request = self.client.prepare(rtu[1:-2], unit.slave_id) if gateway.transport == "tcp" else rtu
        context = {"transport": gateway.transport, "settings": gateway.model_dump(), "source": source,
                   "slave_id": unit.slave_id, "function": function, "address": address, "value_or_count": value}
        request_id = self.store.event(gateway.id, unit.id, "tx", request, context)
        started = time.monotonic()
        self.last_tx = started
        try:
            if gateway.transport == "tcp":
                # Capture bytes even if a TCP response is partial or malformed.
                client = self.client
                client.sock.sendall(request)
                received = bytearray()
                wanted = 6
                try:
                    while len(received) < wanted:
                        chunk = client.sock.recv(wanted - len(received))
                        if not chunk:
                            raise ConnectionError("controller closed connection during response")
                        received.extend(chunk)
                        if len(received) == 6:
                            length = int.from_bytes(received[4:6], "big")
                            if not 2 <= length <= 254:
                                raise ModbusError(f"invalid MBAP length {length}")
                            wanted = 6 + length
                except OSError as exc:
                    if received:
                        self.store.event(gateway.id, unit.id, "rx", bytes(received), {**context, "request_id": request_id, "status":"partial", "error":str(exc)})
                    raise
                raw = bytes(received)
                decoded = decode_tcp_frame(raw)
            else:
                self.client.write(request)
                received = bytearray()
                deadline = time.monotonic() + gateway.timeout
                last = None
                try:
                    while time.monotonic() < deadline:
                        chunk = self.client.read(4096)
                        if chunk:
                            received.extend(chunk)
                            last = time.monotonic()
                        elif received and last and time.monotonic() - last >= gateway.frame_gap:
                            break
                except OSError as exc:
                    if received:
                        self.store.event(gateway.id, unit.id, "rx", bytes(received), {
                            **context, "request_id":request_id, "status":"partial", "error":str(exc)})
                    raise
                raw = bytes(received)
                decoded = decode_frame(raw)
            description = asdict(decoded)
            description["payload"] = decoded.payload.hex()
            self.store.event(gateway.id, unit.id, "rx", raw, {**context, "request_id":request_id,
                "round_trip_seconds": time.monotonic() - started, "decode": description})
            if not decoded.valid:
                raise ModbusError(decoded.status)
            if decoded.slave != unit.slave_id or decoded.function not in (function, function | 128):
                raise ModbusError("reply unit/function does not match request")
            if gateway.transport == "tcp" and raw[:2] != request[:2]:
                raise ModbusError("transaction ID does not match")
            if decoded.exception_code:
                raise ModbusError(f"Modbus exception {decoded.exception_code:02X}")
            if function in (5, 6):
                if decoded.address != address or decoded.values != [value]:
                    raise ModbusError("write acknowledgement differs from request")
            elif not decoded.values or (function in (3, 4) and len(decoded.values) != value):
                raise ModbusError("read reply has an unexpected length")
            return decoded.values
        finally:
            if gateway.transport == "tcp" and gateway.fresh_connection:
                self.close()

    def passive_read(self):
        if not self.client.is_open:
            self.client.open()
        return self.client.read(4096)
