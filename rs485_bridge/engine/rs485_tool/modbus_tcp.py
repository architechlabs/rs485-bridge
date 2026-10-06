"""Modbus TCP framing and client. Raw MBAP bytes are preserved for storage."""
from __future__ import annotations
import socket
from .crc import append_crc
from .models import ModbusDecode
from .modbus import decode_frame


def build_mbap(pdu: bytes, unit_id: int, transaction_id: int) -> bytes:
    if not 0 <= unit_id <= 255: raise ValueError("Modbus TCP Unit ID must be 0..255")
    if not 0 <= transaction_id <= 0xFFFF: raise ValueError("transaction id must be 0..65535")
    if not 1 <= len(pdu) <= 253: raise ValueError("Modbus PDU must contain 1..253 bytes")
    return transaction_id.to_bytes(2,"big") + b"\x00\x00" + (len(pdu)+1).to_bytes(2,"big") + bytes((unit_id,)) + pdu


def decode_tcp_frame(raw: bytes) -> ModbusDecode:
    if len(raw) < 8: return ModbusDecode(False,"malformed Modbus TCP: short MBAP/PDU",payload=raw)
    transaction=int.from_bytes(raw[:2],"big"); protocol=int.from_bytes(raw[2:4],"big"); length=int.from_bytes(raw[4:6],"big");unit=raw[6]
    if protocol != 0: return ModbusDecode(False,"malformed Modbus TCP: protocol identifier is not zero",slave=unit,payload=raw[7:])
    if length != len(raw)-6: return ModbusDecode(False,"malformed Modbus TCP: MBAP length mismatch",slave=unit,payload=raw[7:],details={"transaction_id":transaction,"mbap_length":length})
    pdu=raw[7:]
    # Reuse the well-tested function/payload decoder without pretending TCP has an RTU CRC.
    parsed=decode_frame(append_crc(bytes((unit,))+pdu))
    parsed.crc_received=None;parsed.crc_calculated=None;parsed.crc_valid=None
    parsed.details={**parsed.details,"transaction_id":transaction,"transport":"Modbus TCP"}
    if parsed.valid: parsed.status="valid Modbus TCP"
    elif parsed.status.startswith("malformed: CRC mismatch"): parsed.status="malformed Modbus TCP function/payload"
    return parsed


class ModbusTcpClient:
    def __init__(self, host: str, port: int=502, timeout: float=1.0):
        self.host,self.port,self.timeout=host,port,timeout;self.sock:socket.socket|None=None;self.transaction_id=0
    @property
    def is_open(self): return self.sock is not None
    def open(self):
        if not self.sock:
            try:
                self.sock=socket.create_connection((self.host,self.port),timeout=self.timeout);self.sock.settimeout(self.timeout)
            except OSError as exc: raise OSError(f"Cannot connect to Modbus TCP {self.host}:{self.port}: {exc}") from exc
        return self
    def close(self):
        if self.sock:
            try:self.sock.close()
            finally:self.sock=None
    def prepare(self,pdu:bytes,unit_id:int)->bytes:
        self.transaction_id=(self.transaction_id+1)&0xFFFF
        if self.transaction_id==0:self.transaction_id=1
        return build_mbap(pdu,unit_id,self.transaction_id)
    def exchange(self,request:bytes)->bytes:
        if not self.sock:self.open()
        assert self.sock is not None
        try:
            self.sock.sendall(request)
            header=self._recv_exact(6)
            length=int.from_bytes(header[4:6],"big")
            if length<2 or length>254: raise OSError(f"Invalid Modbus TCP MBAP length {length}")
            tail=self._recv_exact(length)
            response=header+tail
            if response[:2]!=request[:2]: raise OSError("Modbus TCP transaction ID mismatch")
            return response
        except OSError:
            # Some controllers close or reset the TCP session after a command.
            # Drop the dead socket so the next user operation creates a fresh
            # connection. Do not retry this request here: after a lost reply,
            # a write's execution may be uncertain and must never be repeated.
            self.close()
            raise
    def _recv_exact(self,count):
        assert self.sock is not None
        result=bytearray()
        while len(result)<count:
            chunk=self.sock.recv(count-len(result))
            if not chunk: raise OSError("Modbus TCP peer closed connection during response")
            result.extend(chunk)
        return bytes(result)
