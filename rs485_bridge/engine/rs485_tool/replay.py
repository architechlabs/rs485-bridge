"""Explicit replay operations; dry-run is the default."""
from __future__ import annotations
import time
from .safety import SafetyGate

class ReplayService:
    def __init__(self, serial_manager, gate: SafetyGate): self.serial=serial_manager; self.gate=gate
    def replay_frames(self, frames, *, mode="dry-run", speed=1.0, preserve_timing=True, confirmed=False):
        if mode not in ("dry-run","show-only","tx"): raise ValueError("mode must be dry-run, show-only, or tx")
        if speed <= 0: raise ValueError("speed must be positive")
        if mode == "tx":
            if not confirmed: raise PermissionError("Actual replay requires explicit confirmation")
            self.gate.require_tx()
            if not self.serial.is_open: self.serial.open()
        previous=None; result=[]
        for item in frames:
            if isinstance(item,(bytes,bytearray,memoryview)):
                raw=bytes(item);stamp=None
            else:
                raw=bytes(item["raw"] if isinstance(item,dict) else item.raw)
                stamp=item.get("timestamp_utc") if isinstance(item,dict) else item.timestamp_utc
            result.append(raw)
            if mode == "tx":
                if preserve_timing and previous and stamp:
                    from datetime import datetime
                    delay=max(0,(datetime.fromisoformat(stamp)-datetime.fromisoformat(previous)).total_seconds())/speed
                    time.sleep(min(delay,10.0))
                self.serial.write(raw)
            previous=stamp
        return result
