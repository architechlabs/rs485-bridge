"""Strict, versioned configuration. Imported mappings are data, never Python."""
from __future__ import annotations
import json
import math
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

class Point(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    name: str = Field(min_length=1, max_length=80)
    entity: Literal["sensor", "switch", "select", "number", "button"] = "sensor"
    role: Literal["power", "mode", "fan", "target", "current"] | None = None
    offset: int = Field(default=0, ge=0, le=65535)
    read_function: Literal[1, 2, 3, 4] = 3
    write_function: Literal[5, 6] | None = None
    commands: dict[str, int] = Field(default_factory=dict)
    states: dict[str, str] = Field(default_factory=dict)
    unit: str | None = None
    scale: float = Field(default=1, gt=0)
    value_offset: float = 0
    signed: bool = False
    minimum: float = 0
    maximum: float = 65535
    step: float = Field(default=1, gt=0)
    notes: str = ""
    optional: bool = False

    @model_validator(mode="after")
    def check_point(self):
        if self.minimum > self.maximum:
            raise ValueError("minimum exceeds maximum")
        if any(not 0 <= v <= 65535 for v in self.commands.values()):
            raise ValueError("command values must be 0..65535")
        if self.write_function == 5 and any(v not in (0, 65280) for v in self.commands.values()):
            raise ValueError("FC05 accepts only 0 or 65280")
        if self.entity in ("switch", "select", "button") and (not self.commands or not self.write_function):
            raise ValueError("controllable entities require commands and a write function")
        if self.entity == "number" and self.write_function != 6:
            raise ValueError("number entities require FC06")
        if self.entity == "switch" and set(self.commands) != {"ON", "OFF"}:
            raise ValueError("switch commands must be ON and OFF")
        if self.role == "power" and self.entity != "switch":
            raise ValueError("climate power role requires a switch")
        if self.role in ("mode", "fan") and self.entity != "select":
            raise ValueError("mode/fan roles require select entities")
        if self.role == "target" and (self.entity != "number" or self.unit != "°C"):
            raise ValueError("climate target requires a Celsius number")
        if self.role == "current" and (self.entity != "sensor" or self.unit != "°C"):
            raise ValueError("climate current requires a Celsius sensor")
        if self.role == "mode" and not set(self.commands).issubset({"cool", "heat", "dry", "fan_only", "auto"}):
            raise ValueError("use Home Assistant climate mode names")
        return self

    def decode(self, raw: int):
        if str(raw) in self.states:
            return self.states[str(raw)]
        if self.signed and raw >= 32768:
            raw -= 65536
        value = raw * self.scale + self.value_offset
        return int(value) if value.is_integer() else round(value, 6)

    def encode(self, value) -> int:
        if not self.write_function:
            raise PermissionError("point is read-only")
        if self.commands:
            if str(value) not in self.commands:
                raise ValueError(f"choose one of {list(self.commands)}")
            return self.commands[str(value)]
        number = float(value)
        if not math.isfinite(number) or not self.minimum <= number <= self.maximum:
            raise ValueError(f"value must be {self.minimum}..{self.maximum}")
        steps = (number - self.minimum) / self.step
        raw = (number - self.value_offset) / self.scale
        if abs(steps - round(steps)) > 1e-6 or abs(raw - round(raw)) > 1e-6:
            raise ValueError("value does not match the configured resolution")
        raw = round(raw)
        if self.signed:
            if not -32768 <= raw <= 32767:
                raise ValueError("signed value out of range")
            raw &= 65535
        if not 0 <= raw <= 65535:
            raise ValueError("raw value out of range")
        return raw

class Profile(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = "1.0.0"
    name: str = Field(min_length=1, max_length=100)
    manufacturer: str = "Custom"
    model: str = "Modbus device"
    source: str = "User supplied mapping"
    notes: str = ""
    base_address: int = Field(default=0, ge=0, le=65535)
    address_stride: int = Field(default=1, ge=0, le=4096)
    points: list[Point] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def unique_points(self):
        if len({p.id for p in self.points}) != len(self.points):
            raise ValueError("duplicate point IDs")
        roles = [p.role for p in self.points if p.role]
        if len(set(roles)) != len(roles):
            raise ValueError("duplicate climate roles")
        return self

class Gateway(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    name: str = Field(min_length=1, max_length=80)
    transport: Literal["tcp", "serial"] = "tcp"
    host: str = ""
    port: int = Field(default=502, ge=1, le=65535)
    serial_port: str = ""
    baudrate: int = Field(default=9600, ge=300, le=1000000)
    bytesize: Literal[7, 8] = 8
    parity: Literal["N", "E", "O"] = "N"
    stopbits: Literal[1, 2] = 1
    rs485_mode: bool = False
    frame_gap: float = Field(default=0.004, ge=0.001, le=1)
    timeout: float = Field(default=2, ge=0.2, le=15)
    request_delay: float = Field(default=0.25, ge=0.1, le=10)
    poll_interval: float = Field(default=15, ge=5, le=3600)
    polling_enabled: bool = False
    fresh_connection: bool = True
    passive: bool = False

    @model_validator(mode="after")
    def check_gateway(self):
        if self.transport == "tcp" and not self.host.strip():
            raise ValueError("TCP host is required")
        if self.transport == "serial" and not self.serial_port.strip():
            raise ValueError("serial port is required")
        if self.passive and (self.transport != "serial" or self.polling_enabled):
            raise ValueError("passive mode requires serial transport with polling disabled")
        return self

class Unit(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    name: str = Field(min_length=1, max_length=80)
    gateway: str
    slave_id: int = Field(default=10, ge=1, le=247)
    address: int = Field(default=0, ge=0, le=255)
    profile: str
    control_enabled: bool = False
    enabled: bool = True

class MQTT(StrictModel):
    enabled: bool = False
    host: str = ""
    port: int = Field(default=1883, ge=1, le=65535)
    username: str = ""
    password: str = ""
    tls: bool = False
    topic_prefix: str = Field(default="rs485_bridge", pattern=r"^[a-zA-Z0-9_/-]+$")
    discovery_prefix: str = Field(default="homeassistant", pattern=r"^[a-zA-Z0-9_/-]+$")

class BridgeConfig(StrictModel):
    schema_version: Literal[1] = 1
    instance_id: str = Field(default="site", pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    tx_enabled: bool = False
    allow_writes: bool = False
    mqtt: MQTT = Field(default_factory=MQTT)
    gateways: list[Gateway] = Field(default_factory=list, max_length=32)
    units: list[Unit] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def links(self):
        for name, items in (("gateway", self.gateways), ("unit", self.units)):
            if len({x.id for x in items}) != len(items):
                raise ValueError(f"duplicate {name} IDs")
        endpoints = []
        for gateway in self.gateways:
            serial_port = gateway.serial_port.strip()
            if serial_port.upper().startswith("COM"):
                serial_port = serial_port.upper()
            endpoints.append((gateway.transport, gateway.host.strip().lower(), gateway.port) if gateway.transport == "tcp" else (gateway.transport, serial_port))
        if len(set(endpoints)) != len(endpoints):
            raise ValueError("one gateway must own each TCP endpoint or serial port; add units to that gateway")
        if any(u.gateway not in {g.id for g in self.gateways} for u in self.units):
            raise ValueError("unit refers to an unknown gateway")
        identities = [(u.gateway, u.slave_id, u.address, u.profile) for u in self.units]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate equipment mapping; rename the existing unit instead")
        return self

def validate_links(config: BridgeConfig, profiles: dict[str, Profile]):
    for unit in config.units:
        if unit.profile not in profiles:
            raise ValueError(f"profile {unit.profile} is not installed")
        profile = profiles[unit.profile]
        if any(profile.base_address + unit.address * profile.address_stride + p.offset > 65535 for p in profile.points):
            raise ValueError("computed protocol address exceeds 65535")

def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)
