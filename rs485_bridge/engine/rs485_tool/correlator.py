"""Best-effort RTU request/response matching; ambiguous matches stay unpaired."""
from __future__ import annotations
from dataclasses import dataclass
from .models import Frame

@dataclass(slots=True)
class Match:
    request_id: int | None
    response_id: int | None
    round_trip_seconds: float | None
    confidence: str

class Correlator:
    def __init__(self, timeout: float = 2.0):
        self.timeout = timeout
        self.pending: list[Frame] = []

    def add(self, frame: Frame) -> Match | None:
        d = frame.decode
        if not d or not d.valid or d.slave is None or d.function is None: return None
        if frame.direction == "tx":
            self.pending.append(frame); return None
        if frame.direction != "rx": return None
        from datetime import datetime
        try: received_at=datetime.fromisoformat(frame.timestamp_utc)
        except Exception: received_at=None
        if received_at:
            self.pending=[req for req in self.pending if (received_at-datetime.fromisoformat(req.timestamp_utc)).total_seconds() <= self.timeout]
        candidates = []
        for req in self.pending:
            q = req.decode
            if not q or q.slave != d.slave or (q.function or 0) != (d.function & 0x7f): continue
            if q.address is not None and d.address is not None and q.address != d.address: continue
            is_exception=bool(d.function & 0x80)
            if q.quantity is not None and (d.function & 0x7f) in (1,2,3,4) and not is_exception:
                expected=q.quantity*2 if (d.function & 0x7f) in (3,4) else (q.quantity+7)//8
                if d.details.get("role")!="response" or d.details.get("byte_count")!=expected: continue
            candidates.append(req)
        if len(candidates) != 1: return Match(None, frame.frame_id, None, "ambiguous" if candidates else "unmatched")
        req = candidates[0]; self.pending.remove(req)
        try: rtt = (received_at - datetime.fromisoformat(req.timestamp_utc)).total_seconds() if received_at else None
        except Exception: rtt = None
        return Match(req.frame_id, frame.frame_id, rtt, "best_effort")
