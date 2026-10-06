"""Conservative Modbus RTU decoding and request construction."""
from __future__ import annotations
from .crc import append_crc, crc16
from .models import ModbusDecode

FUNCTIONS = {1:"Read Coils", 2:"Read Discrete Inputs", 3:"Read Holding Registers", 4:"Read Input Registers",
             5:"Write Single Coil", 6:"Write Single Register", 15:"Write Multiple Coils", 16:"Write Multiple Registers"}
_FIXED = {5:8, 6:8}


def decode_frame(raw: bytes) -> ModbusDecode:
    if len(raw) < 4:
        return ModbusDecode(False, "malformed: shorter than address/function/CRC", payload=raw)
    slave, function = raw[0], raw[1]
    received, calculated = int.from_bytes(raw[-2:], "little"), crc16(raw[:-2])
    if not 1 <= slave <= 247:
        return ModbusDecode(False, "malformed: slave address outside 1..247", slave=slave, function=function, payload=raw[2:-2], crc_received=received, crc_calculated=calculated, crc_valid=received == calculated)
    if received != calculated:
        return ModbusDecode(False, "malformed: CRC mismatch", slave=slave, function=function, function_name=FUNCTIONS.get(function), payload=raw[2:-2], crc_received=received, crc_calculated=calculated, crc_valid=False)
    if function not in FUNCTIONS and function not in {f | 0x80 for f in FUNCTIONS}:
        return ModbusDecode(False, "unknown function with valid CRC", slave=slave, function=function, payload=raw[2:-2], crc_received=received, crc_calculated=calculated, crc_valid=True)
    exception = bool(function & 0x80)
    base_function = function & 0x7F if exception else function
    payload = raw[2:-2]
    dec = ModbusDecode(True, "valid exception response" if exception else "valid Modbus RTU", slave=slave, function=function,
                       function_name=FUNCTIONS.get(base_function), payload=payload, crc_received=received, crc_calculated=calculated, crc_valid=True)
    if exception:
        if len(payload) != 1: dec.valid, dec.status = False, "malformed exception response length"
        else: dec.exception_code = payload[0]
        return dec
    if base_function in (1,2,3,4):
        # Read response: byte-count prefix matches remaining payload. This check
        # takes priority so e.g. a two-register response is not mistaken for a request.
        if len(payload) >= 2 and payload[0] == len(payload) - 1:
            dec.details = {"role": "response", "byte_count": payload[0]}
            if base_function in (3,4):
                if payload[0] % 2: dec.valid, dec.status = False, "malformed register response has odd byte count"
                else: dec.values = [int.from_bytes(payload[i:i+2], "big") for i in range(1, len(payload)-1, 2)]
            else:
                dec.values = [((payload[1+i//8] >> (i%8)) & 1) for i in range(min(payload[0]*8, 2000))]
        elif len(payload) == 4:
            dec.address, dec.quantity = int.from_bytes(payload[:2], "big"), int.from_bytes(payload[2:4], "big")
            dec.details = {"role": "request", "protocol_address_zero_based": dec.address}
            if not 1 <= dec.quantity <= (2000 if base_function in (1,2) else 125): dec.valid, dec.status = False, "malformed read quantity"
        else:
            dec.valid, dec.status = False, "malformed read request/response payload"
    elif base_function in (5,6):
        if len(payload) != 4: dec.valid, dec.status = False, "malformed single-write payload"
        else:
            dec.address, value = int.from_bytes(payload[:2], "big"), int.from_bytes(payload[2:], "big")
            dec.values = [value]
            dec.details = {"role": "request_or_echo_response", "protocol_address_zero_based": dec.address, "value": value}
    elif base_function in (15,16):
        if len(payload)==4:
            dec.address=int.from_bytes(payload[:2],"big");dec.quantity=int.from_bytes(payload[2:4],"big")
            dec.details={"role":"response","protocol_address_zero_based":dec.address}
        elif len(payload)>=5:
            dec.address = int.from_bytes(payload[:2], "big")
            dec.quantity = int.from_bytes(payload[2:4], "big")
            count = payload[4]
            dec.details = {"role": "request", "protocol_address_zero_based": dec.address, "byte_count":count}
            if len(payload) != 5+count: dec.valid, dec.status = False, "malformed multiple-write byte count"
        else: dec.valid, dec.status = False, "malformed multiple-write payload"
    return dec


def build_request(slave: int, function: int, address: int, quantity_or_value: int) -> bytes:
    if not 1 <= slave <= 247: raise ValueError("slave must be 1..247")
    if function not in (1,2,3,4,5,6): raise ValueError("supported builder functions: 1,2,3,4,5,6")
    if not 0 <= address <= 65535 or not 0 <= quantity_or_value <= 65535: raise ValueError("address/value out of range")
    if function in (1,2) and not 1 <= quantity_or_value <= 2000: raise ValueError("coil/input quantity must be 1..2000")
    if function in (3,4) and not 1 <= quantity_or_value <= 125: raise ValueError("register quantity must be 1..125")
    if function==5 and quantity_or_value not in (0,0xFF00): raise ValueError("single-coil value must be 0x0000 (off) or 0xFF00 (on)")
    return append_crc(bytes((slave,function)) + address.to_bytes(2,"big") + quantity_or_value.to_bytes(2,"big"))


def build_write_registers(slave: int, address: int, values: list[int]) -> bytes:
    if not 1 <= slave <= 247: raise ValueError("slave must be 1..247")
    if not 0 <= address <= 65535: raise ValueError("address out of range")
    if not values or len(values) > 123: raise ValueError("provide 1..123 register values")
    if any(not 0 <= v <= 65535 for v in values): raise ValueError("register values must be 0..65535")
    data = b"".join(v.to_bytes(2,"big") for v in values)
    return append_crc(bytes((slave,16)) + address.to_bytes(2,"big") + len(values).to_bytes(2,"big") + bytes((len(data),)) + data)
