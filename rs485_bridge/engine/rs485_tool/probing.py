"""Rate-limited read-only active probing primitives; caller must pass safety gate."""
from __future__ import annotations
import time
from dataclasses import dataclass
from .modbus import build_request, decode_frame

LIKELY_BAUD_RATES=(9600,19200,38400,57600,115200)

@dataclass(slots=True)
class ProbeResult:
    baudrate: int
    request_hex: str
    response_hex: str | None
    status: str

class ReadOnlyProber:
    def __init__(self, serial_manager, safety_gate, delay: float=.25):
        if delay < .1: raise ValueError("probe interval must be at least 100ms")
        self.serial=serial_manager;self.gate=safety_gate;self.delay=delay
    def read_registers(self, slave: int, address: int, count: int=1, function: int=3) -> ProbeResult:
        self.gate.require_tx(); request=build_request(slave,function,address,count)
        if not self.serial.is_open: self.serial.open()
        self.serial.write(request);time.sleep(self.delay)
        response=self.serial.read(256)
        if not response: return ProbeResult(self.serial.settings.baudrate,request.hex().upper(),None,"timeout")
        decoded=decode_frame(response)
        return ProbeResult(self.serial.settings.baudrate,request.hex().upper(),response.hex().upper(),decoded.status)
