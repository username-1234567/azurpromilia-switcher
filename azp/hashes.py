"""清单里的校验值。

国服/台服用 MD5（32 位十六进制），日服用 CRC-64/XZ（十进制字符串）。
两种都要能算、能比，否则跨区建链接时无法确认「同名同大小」到底是不是同一份内容。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 1 << 20

MD5 = "md5"
CRC64 = "crc64"

_POLY = 0x42F0E1EBA9EA3693
_MASK = (1 << 64) - 1
_TABLE: list[int] | None = None


def _table() -> list[int]:
    global _TABLE
    if _TABLE is None:
        rpoly = int(bin(_POLY)[2:].zfill(64)[::-1], 2)
        rows: list[int] = []
        for i in range(256):
            crc = i
            for _ in range(8):
                crc = (crc >> 1) ^ (rpoly if crc & 1 else 0)
            rows.append(crc)
        _TABLE = rows
    return _TABLE


def crc64(data: bytes) -> int:
    """CRC-64/XZ：反射、初值与异或输出都是全 1，和日服清单对得上。"""
    table = _table()
    crc = _MASK
    for byte in data:
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ _MASK


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def crc64_of(path: Path) -> str:
    table = _table()
    crc = _MASK
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            for byte in block:
                crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return str(crc ^ _MASK)


def digest(path: Path, kind: str = MD5) -> str:
    return crc64_of(path) if kind == CRC64 else md5_of(path)


def digest_bytes(data: bytes, kind: str = MD5) -> str:
    """不落盘算校验值，给零字节文件用。"""
    return str(crc64(data)) if kind == CRC64 else hashlib.md5(data).hexdigest()


def kind_of(manifest: dict) -> str:
    """清单声明的校验方式，老清单没写就是 md5。"""
    return str(manifest.get("hash_kind") or MD5)


def label(kind: str) -> str:
    return "CRC64" if kind == CRC64 else "MD5"
