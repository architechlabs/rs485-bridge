"""Cross-platform serial-device enumeration and chipset hints."""
from __future__ import annotations
from .models import DeviceInfo

CHIPS = {(0x1A86,0x7523):"CH340/CH341",(0x1A86,0x5523):"CH341",(0x0403,0x6001):"FTDI FT232",(0x10C4,0xEA60):"Silicon Labs CP210x",(0x067B,0x2303):"Prolific PL2303"}

def detect_devices() -> list[DeviceInfo]:
    try:
        from serial.tools import list_ports
    except ImportError as exc: raise RuntimeError("pyserial is required; install with pip install pyserial") from exc
    result=[]
    for p in list_ports.comports():
        chipset=CHIPS.get((p.vid,p.pid),"unknown")
        desc=(p.description or "").lower(); hwid=(p.hwid or "").lower()
        generic = "cdc" in desc or "usb" in desc or "usb" in hwid
        result.append(DeviceInfo(port=p.device,description=p.description or "",hwid=p.hwid or "",vid=p.vid,pid=p.pid,
            manufacturer=getattr(p,"manufacturer",None),product=getattr(p,"product",None),serial_number=getattr(p,"serial_number",None),
            interface=getattr(p,"interface",None),location=getattr(p,"location",None),driver=None,chipset=chipset,
            likely_rs485=chipset!="unknown" or generic))
    return result

def format_devices(devices: list[DeviceInfo]) -> str:
    if not devices: return "No serial devices found. Check adapter connection and Windows driver installation."
    lines=[]
    for d in devices:
        vid=f"{d.vid:04X}" if d.vid is not None else "?"; pid=f"{d.pid:04X}" if d.pid is not None else "?"
        mark="likely serial adapter" if d.likely_rs485 else "verify manually"
        lines.append(f"{d.port}: {mark}\n  Chipset: {d.chipset}; VID:PID {vid}:{pid}\n  Description: {d.description}\n  Manufacturer/product: {d.manufacturer or '?'} / {d.product or '?'}\n  Serial: {d.serial_number or '?'}; Location: {d.location or '?'}\n  Driver: {d.driver or '?'}; HWID: {d.hwid or '?'}")
    return "\n".join(lines)
