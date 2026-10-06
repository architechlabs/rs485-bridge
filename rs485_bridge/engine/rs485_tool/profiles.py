"""Vendor profile boundary. Unknown maps intentionally remain empty."""
from dataclasses import dataclass, field
from .models import SerialSettings

@dataclass(slots=True)
class RegisterDefinition:
    address: int
    name: str
    access: str = "unknown"
    scale: float | None = None
    unit: str | None = None
    documentation: str | None = None

@dataclass(slots=True)
class DeviceProfile:
    name: str
    serial: SerialSettings = field(default_factory=SerialSettings)
    default_slave: int | None = None
    registers: dict[int, RegisterDefinition] = field(default_factory=dict)

LG_GENERIC = DeviceProfile(name="LG HVAC (unverified generic profile)", serial=SerialSettings(baudrate=9600,bytesize=8,parity="N",stopbits=1))
