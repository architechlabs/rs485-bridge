"""Passive inspection helpers; does not transmit or infer undocumented mappings."""
from __future__ import annotations
from collections import Counter
from .models import Frame

def summarize(frames: list[Frame]) -> dict:
    statuses=Counter(f.decode.status if f.decode else f.parser_status for f in frames)
    slaves=Counter(f.decode.slave for f in frames if f.decode and f.decode.valid and f.decode.slave is not None)
    functions=Counter(f.decode.function for f in frames if f.decode and f.decode.valid and f.decode.function is not None)
    return {"frames":len(frames),"statuses":dict(statuses),"slaves":dict(slaves),"functions":dict(functions)}
