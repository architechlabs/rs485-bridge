"""YAML configuration with conservative defaults."""
from __future__ import annotations
from pathlib import Path
import yaml
from .models import SerialSettings

DEFAULTS={"serial":{"port":None,"baudrate":9600,"bytesize":8,"parity":"N","stopbits":1,"timeout":0.05,"frame_timeout":0.004,"transmit_delay":0.0,"receive_timeout":1.0,"rs485_mode":False},
          "network":{"host":None,"modbus_tcp_port":502,"unit_id":1,"timeout":2.0,"bacnet_port":47808,"bacnet_device_id":None,"bacnet_type":"C"},
          "capture_directory":"captures","database_path":"captures/rs485.sqlite3","tx_locked":True,"replay":{"default_mode":"dry-run"},"profile":"generic"}

def default_config_path() -> Path: return Path.home()/".rs485-tool"/"config.yaml"
def load_config(path: str|Path|None=None) -> dict:
    target=Path(path) if path else default_config_path()
    if not target.exists():
        target.parent.mkdir(parents=True,exist_ok=True); target.write_text(yaml.safe_dump(DEFAULTS,sort_keys=False),encoding="utf8")
    loaded=yaml.safe_load(target.read_text(encoding="utf8")) or {}
    result = {**DEFAULTS,**loaded,"serial":{**DEFAULTS["serial"],**loaded.get("serial",{})},"network":{**DEFAULTS["network"],**loaded.get("network",{})}}
    checkout_runtime = Path(__file__).resolve().parents[3] / "runtime"
    base = checkout_runtime if checkout_runtime.is_dir() else Path.cwd()
    for key in ("capture_directory", "database_path"):
        path_value = Path(result[key])
        if not path_value.is_absolute():
            result[key] = str(base / path_value)
    return result
def settings_from(config: dict) -> SerialSettings: return SerialSettings(**config["serial"])
