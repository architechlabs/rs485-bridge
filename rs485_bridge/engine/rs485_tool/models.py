"""Shared, application-neutral data models."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
import uuid


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class SerialSettings:
    port: str | None = None
    baudrate: int | None = 9600
    bytesize: int | None = 8
    parity: str | None = "N"
    stopbits: float | None = 1
    timeout: float = 0.05
    frame_timeout: float = 0.004
    transmit_delay: float = 0.0
    receive_timeout: float = 1.0
    rs485_mode: bool = False

    @classmethod
    def preset(cls, name: str, **kwargs: Any) -> "SerialSettings":
        presets = {"9600_8N1": (9600, "N"), "9600_8E1": (9600, "E"), "9600_8O1": (9600, "O"),
                   "19200_8N1": (19200, "N"), "19200_8E1": (19200, "E"), "38400_8N1": (38400, "N"),
                   "57600_8N1": (57600, "N"), "115200_8N1": (115200, "N")}
        if name not in presets:
            raise ValueError(f"Unknown serial preset {name!r}; available: {', '.join(presets)}")
        baud, parity = presets[name]
        return cls(baudrate=baud, parity=parity, **kwargs)


@dataclass(slots=True)
class DeviceInfo:
    port: str
    description: str = ""
    hwid: str = ""
    vid: int | None = None
    pid: int | None = None
    manufacturer: str | None = None
    product: str | None = None
    serial_number: str | None = None
    interface: str | None = None
    location: str | None = None
    driver: str | None = None
    chipset: str = "unknown"
    likely_rs485: bool = False


@dataclass(slots=True)
class ModbusDecode:
    valid: bool
    status: str
    slave: int | None = None
    function: int | None = None
    function_name: str | None = None
    payload: bytes = b""
    crc_received: int | None = None
    crc_calculated: int | None = None
    crc_valid: bool | None = None
    exception_code: int | None = None
    address: int | None = None
    quantity: int | None = None
    values: list[int] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Frame:
    raw: bytes
    timestamp_utc: str = field(default_factory=lambda: utc_now().isoformat())
    timestamp_local: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    session_id: str = ""
    port: str | None = None
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    direction: str = "unknown"
    previous_byte_gap: float | None = None
    previous_frame_gap: float | None = None
    parser_status: str = "unparsed"
    notes: str = ""
    decode: ModbusDecode | None = None
    frame_id: int | None = None

    @property
    def hex(self) -> str:
        return self.raw.hex(" ").upper()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["raw"] = self.raw.hex()
        if self.decode:
            data["decode"]["payload"] = self.decode.payload.hex()
        return data


def new_session_id() -> str:
    return "session_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
