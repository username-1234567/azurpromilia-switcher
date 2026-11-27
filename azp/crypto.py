"""AES-CBC 封装：优先用 pycryptodome，缺失时回退到系统 openssl。"""

from __future__ import annotations

import shutil
import subprocess

_BITS = {16: 128, 24: 192, 32: 256}


class BackendMissing(RuntimeError):
    """pycryptodome 和系统 openssl 都没有。"""


def _openssl(encrypt: bool, key: bytes, iv: bytes, data: bytes) -> bytes:
    exe = shutil.which("openssl")
    if not exe:
        raise BackendMissing(
            "缺少 AES 实现：请 pip install pycryptodome，或安装 openssl 并加入 PATH"
        )
    alg = f"-aes-{_BITS[len(key)]}-cbc"
    proc = subprocess.run(
        [exe, "enc", alg, "-e" if encrypt else "-d", "-K", key.hex(), "-iv", iv.hex()],
        input=data,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"openssl 失败: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    return proc.stdout


def _cipher(key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
    if len(key) not in _BITS:
        raise ValueError(f"key 长度必须是 16/24/32 字节，实际 {len(key)}")
    if len(iv) != 16:
        raise ValueError("iv 必须 16 字节")
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad, unpad
    except ImportError:
        return _openssl(encrypt, key, iv, data)
    engine = AES.new(key, AES.MODE_CBC, iv)
    if encrypt:
        return engine.encrypt(pad(data, 16))
    return unpad(engine.decrypt(data), 16)


def decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return _cipher(key, iv, data, False)


def encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return _cipher(key, iv, data, True)
