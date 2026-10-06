"""Modbus RTU CRC-16 implementation."""
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc & 0xFFFF


def append_crc(data: bytes) -> bytes:
    value = crc16(data)
    return data + bytes((value & 0xFF, value >> 8))


def check_crc(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")
