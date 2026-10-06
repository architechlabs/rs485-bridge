"""Passive capture service; it never calls SerialManager.write."""
from __future__ import annotations
import threading, time
from .framing import SilenceFramer
from .models import Frame, SerialSettings
from .modbus import decode_frame
from .storage import CaptureStore

class CaptureService:
    def __init__(self, serial_manager, store: CaptureStore, settings: SerialSettings, session_id: str, on_frame=None):
        self.serial=serial_manager; self.store=store; self.settings=settings; self.session_id=session_id; self.on_frame=on_frame
        self.stop_event=threading.Event(); self.thread=None; self.previous_frame_monotonic=None; self.last_byte_gap=None
    def start(self):
        if not self.serial.is_open: self.serial.open()
        self.store.create_session(self.session_id,self.settings)
        self.thread=threading.Thread(target=self._run,name="rs485-capture",daemon=True); self.thread.start(); return self
    def _run(self):
        framer=SilenceFramer(self.settings.frame_timeout)
        while not self.stop_event.is_set():
            try: data=self.serial.read(256)
            except Exception as exc:
                if self.on_frame: self.on_frame(None, f"Capture stopped: {exc}")
                self.stop_event.set(); break
            now=time.monotonic()
            chunks=framer.feed(data,now) if data else framer.flush_if_idle(now)
            for raw,gap in chunks: self._save(raw,gap)
        for raw,gap in framer.flush(): self._save(raw,gap)
    def _save(self, raw: bytes, gap: float | None):
        frame=Frame(raw=raw,session_id=self.session_id,port=self.settings.port,baudrate=self.settings.baudrate,bytesize=self.settings.bytesize,
                    parity=self.settings.parity,stopbits=self.settings.stopbits,direction="rx",previous_byte_gap=gap,
                    previous_frame_gap=(max(0.0,time.monotonic()-self.previous_frame_monotonic) if self.previous_frame_monotonic else None))
        frame.decode=decode_frame(raw); frame.parser_status=frame.decode.status
        self.store.add_frame(frame); self.previous_frame_monotonic=time.monotonic()
        if self.on_frame: self.on_frame(frame,None)
    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=2)
        self.serial.close()
    @property
    def running(self): return bool(self.thread and self.thread.is_alive())
