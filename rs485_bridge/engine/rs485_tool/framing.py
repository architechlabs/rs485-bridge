"""Silence-gap RTU framer. Reads may be fragmented or coalesced by the OS."""
from __future__ import annotations
import time


class SilenceFramer:
    def __init__(self, silence: float = 0.004):
        self.silence = silence
        self.buffer = bytearray()
        self.last_data_at: float | None = None
        self.last_frame_at: float | None = None
        self.last_gap: float | None = None

    def feed(self, data: bytes, now: float | None = None) -> list[tuple[bytes, float | None]]:
        now = time.monotonic() if now is None else now
        out: list[tuple[bytes, float | None]] = []
        if self.buffer and self.last_data_at is not None and now - self.last_data_at >= self.silence:
            out.append((bytes(self.buffer), self.last_gap)); self.buffer.clear()
            self.last_frame_at = self.last_data_at
        if data:
            if self.buffer and self.last_data_at is not None:
                self.last_gap = max(0.0, now - self.last_data_at)
            self.buffer.extend(data)
            self.last_data_at = now
        return out

    def flush_if_idle(self, now: float | None = None) -> list[tuple[bytes, float | None]]:
        now = time.monotonic() if now is None else now
        if self.buffer and self.last_data_at is not None and now - self.last_data_at >= self.silence:
            frame = bytes(self.buffer); gap = self.last_gap; self.buffer.clear()
            self.last_frame_at = self.last_data_at
            return [(frame, gap)]
        return []

    def flush(self) -> list[tuple[bytes, float | None]]:
        if not self.buffer: return []
        frame = bytes(self.buffer); self.buffer.clear(); self.last_frame_at = self.last_data_at
        return [(frame, self.last_gap)]
