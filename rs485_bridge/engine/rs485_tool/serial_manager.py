"""PySerial wrapper with clear errors and application-marked TX/RX."""
from __future__ import annotations
import time
from .models import SerialSettings

class SerialManager:
    def __init__(self, settings: SerialSettings): self.settings=settings; self.serial=None; self._mock=False
    @property
    def is_open(self):
        if not self.serial: return False
        if self._mock: return self.serial.fileno()!=-1
        return bool(self.serial.is_open)
    def open(self):
        if not self.settings.port: raise ValueError("Select a serial device first (detect, then use <port>)")
        if self.settings.port.startswith("mock://"):
            import socket
            target=self.settings.port[len("mock://"):]
            host,sep,port=target.rpartition(":")
            if not sep: host,port="127.0.0.1",target
            try:
                self.serial=socket.create_connection((host,int(port)),timeout=self.settings.receive_timeout)
                self.serial.settimeout(self.settings.timeout)
                self._mock=True
                return self
            except Exception as exc: raise OSError(f"Cannot connect to mock server {self.settings.port}: {exc}") from exc
        try:
            import serial
            self.serial=serial.Serial(port=self.settings.port,baudrate=self.settings.baudrate,bytesize=self.settings.bytesize,
                parity=self.settings.parity,stopbits=self.settings.stopbits,timeout=self.settings.timeout,write_timeout=self.settings.receive_timeout)
            if self.settings.rs485_mode:
                import serial.rs485
                try: self.serial.rs485_mode=serial.rs485.RS485Settings()
                except Exception as exc:
                    self.serial.close();self.serial=None
                    raise OSError(f"RS485 mode was requested but this OS/adapter driver does not support it: {exc}") from exc
        except Exception as exc:
            if "access" in str(exc).lower() or "permission" in str(exc).lower() or "busy" in str(exc).lower():
                raise OSError(f"Cannot open {self.settings.port}; another application may own it. Close terminal/Modbus tools and retry. ({exc})") from exc
            raise OSError(f"Cannot open {self.settings.port}: {exc}") from exc
        return self
    def close(self):
        if self.serial:
            try: self.serial.close()
            finally: self.serial=None; self._mock=False
    def read(self, size: int=256) -> bytes:
        if not self.is_open: raise OSError("serial port is not open")
        try: return self.serial.recv(size) if self.settings.port and self.settings.port.startswith("mock://") else self.serial.read(size)
        except TimeoutError: return b""
    def write(self, data: bytes) -> None:
        if not self.is_open: raise OSError("serial port is not open")
        if self.settings.transmit_delay: time.sleep(self.settings.transmit_delay)
        self.serial.sendall(data) if self.settings.port and self.settings.port.startswith("mock://") else self.serial.write(data)
        if not (self.settings.port and self.settings.port.startswith("mock://")): self.serial.flush()
    def __enter__(self): return self.open()
    def __exit__(self,*_): self.close()
