"""环境自检：Python、AES 后端、文件系统、硬链接能力、可用空间、长路径。

硬链接是本工具的前提，所以这里不是查操作系统版本，而是真的在目标卷上建一对链接试一下。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import crypto

OK = "ok"
WARN = "warn"
FAIL = "fail"

# 支持硬链接的文件系统。ReFS 也支持，FAT32/exFAT 不支持。
LINKABLE_FS = {"NTFS", "REFS"}


@dataclass
class Item:
    name: str
    status: str
    detail: str = ""


def _probe_root(path: Path) -> Path:
    """返回最近一个已存在的祖先目录，用来做卷查询和探测。"""
    root = path
    while not root.exists() and root != root.parent:
        root = root.parent
    return root


def volume_info(path: Path) -> tuple[str, str]:
    """返回 (文件系统名, 卷序列号)。读不到时返回空串。"""
    if os.name != "nt":
        return "", ""
    drive = _probe_root(path).drive
    if not drive or drive.startswith("\\\\"):
        return "", ""
    fs_buf = ctypes.create_unicode_buffer(64)
    serial = ctypes.c_ulong(0)
    max_len = ctypes.c_ulong(0)
    flags = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(drive + "\\"),
        None,
        0,
        ctypes.byref(serial),
        ctypes.byref(max_len),
        ctypes.byref(flags),
        fs_buf,
        64,
    )
    if not ok:
        return "", ""
    return fs_buf.value, f"{serial.value:08X}"


def hardlink_works(directory: Path) -> tuple[bool, str]:
    """在目录所在卷上真建一对硬链接，成功才算过。"""
    root = _probe_root(directory)
    try:
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            src = Path(tmp) / "probe"
            dst = Path(tmp) / "probe.link"
            src.write_bytes(b"azp")
            os.link(src, dst)
            if dst.stat().st_nlink < 2:
                return False, "链接数没有变成 2"
            return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def long_paths_enabled() -> bool | None:
    """注册表里的 LongPathsEnabled，读不到返回 None。"""
    if os.name != "nt":
        return None
    try:
        import winreg

        key_path = r"SYSTEM\CurrentControlSet\Control\FileSystem"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
        return bool(value)
    except OSError:
        return None


def _aes_item() -> Item:
    try:
        import Crypto  # noqa: F401

        backend = "pycryptodome"
    except ImportError:
        backend = "openssl" if shutil.which("openssl") else ""
    if not backend:
        return Item("AES 后端", FAIL, "pycryptodome 和 openssl 都没有")
    try:
        key, iv = b"0123456789abcdef", b"0123456789abcdef"
        if crypto.decrypt(key, iv, crypto.encrypt(key, iv, b"azp-check")) != b"azp-check":
            return Item("AES 后端", FAIL, f"{backend} 加解密结果不一致")
    except Exception as exc:  # noqa: BLE001
        return Item("AES 后端", FAIL, f"{backend} 不可用：{exc}")
    return Item("AES 后端", OK, backend)


def _path_items(label: str, path: Path, need_bytes: int = 0,
                probe_links: bool = True) -> list[Item]:
    items: list[Item] = []
    root = _probe_root(path)
    if not root.exists():
        return [Item(f"{label}所在盘", FAIL, f"盘不存在：{root}")]
    exists = path.exists()
    fs, serial = volume_info(path)
    where = "" if exists else "（目录还不存在，按所在卷检查）"

    if fs:
        status = OK if fs.upper() in LINKABLE_FS else FAIL
        items.append(Item(f"{label}文件系统", status, f"{fs}  卷 {serial}{where}"))
    else:
        items.append(Item(f"{label}文件系统", WARN, "读不到卷信息，跳过"))

    if probe_links:
        ok, why = hardlink_works(path)
        items.append(Item(f"{label}硬链接", OK if ok else FAIL, why or "可创建"))
    else:
        status = OK if label == "源目录" else WARN
        detail = "不在源区实测，由目标卷探测判断" if label == "源目录" else "未实测（避免写入该目录）"
        items.append(Item(f"{label}硬链接", status, detail))

    if exists:
        probe = path
    else:
        probe = _probe_root(path)
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return items
    if need_bytes:
        need = need_bytes / 2**30
        free = usage.free / 2**30
        if usage.free < need_bytes:
            items.append(Item(f"{label}可用空间", FAIL, f"{free:.2f} GiB，需要 {need:.2f} GiB"))
        elif usage.free < need_bytes * 1.1:
            items.append(Item(f"{label}可用空间", WARN, f"{free:.2f} GiB，需要 {need:.2f} GiB，余量很紧"))
        else:
            items.append(Item(f"{label}可用空间", OK, f"{free:.2f} GiB，需要 {need:.2f} GiB"))
    else:
        items.append(Item(f"{label}可用空间", OK, f"{usage.free / 2**30:.2f} GiB"))
    return items


def _long_path_item(target: Path, manifest: dict | None) -> Item | None:
    if not manifest:
        return None
    files = manifest.get("files") or {}
    if not files:
        return None
    longest = max(len(p) for p in files)
    total = len(str(target)) + 1 + longest
    enabled = long_paths_enabled()
    if total <= 255:
        return Item("长路径", OK, f"最长约 {total} 字符")
    if enabled:
        return Item("长路径", OK, f"最长约 {total} 字符，系统已开启长路径支持")
    detail = f"最长约 {total} 字符，超过 255 且系统未开启长路径"
    return Item("长路径", WARN, detail)


def _running_games() -> list[str]:
    """正在运行的游戏进程。读不到就返回空，宁可漏报也不误报。"""
    if os.name != "nt":
        return []
    import subprocess

    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names: set[str] = set()
    for line in out.splitlines():
        name = line.split(",", 1)[0].strip('"').lower()
        if name.endswith(".exe") and ("azurpromilia" in name or "azupro" in name):
            names.add(name)
    return sorted(names)


def running_games() -> list[str]:
    """正在运行的游戏进程。"""
    return _running_games()


def pending_bytes(rows: list[dict], target: Path) -> int:
    """计划里还真正需要落盘的量：目标已存在且大小一致的文件不计。"""
    total = 0
    for row in rows:
        if row["action"] != "download":
            continue
        try:
            if (target / row["path"]).stat().st_size == row["size"]:
                continue
        except OSError:
            pass
        total += row["size"]
    return total


def run(
    base: Path | None = None,
    target: Path | None = None,
    need_bytes: int = 0,
    manifest: dict | None = None,
    probe_links: bool = True,
) -> tuple[bool, list[Item]]:
    """跑一遍自检。返回 (是否有致命项, 明细)。"""
    items: list[Item] = []
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 10):
        items.append(Item("Python", FAIL, f"{py}，需要 3.10+"))
    else:
        items.append(Item("Python", OK, py))
    items.append(_aes_item())
    running = running_games()
    if running:
        items.append(Item("游戏进程", FAIL, f"正在运行：{'、'.join(running)}，先退出游戏再跑"))
    else:
        items.append(Item("游戏进程", OK, "没有运行"))

    if base is not None:
        items.extend(_path_items("源目录", base, probe_links=False))
    if target is not None:
        items.extend(_path_items("目标目录", target, need_bytes, probe_links))

    if base is not None and target is not None:
        base_fs, base_serial = volume_info(base)
        target_fs, target_serial = volume_info(target)
        if base_serial and target_serial:
            same = base_serial == target_serial
            items.append(
                Item(
                    "同卷",
                    OK if same else FAIL,
                    "源与目标在同一卷" if same else f"源 {base_serial} / 目标 {target_serial}，硬链接不能跨卷",
                )
            )

    long_item = _long_path_item(target, manifest) if target is not None else None
    if long_item is not None:
        items.append(long_item)

    failed = any(i.status == FAIL for i in items)
    return not failed, items


def render(items: list[Item]) -> str:
    marks = {OK: "[ok]  ", WARN: "[!]   ", FAIL: "[x]   "}
    lines = []
    for item in items:
        detail = f"  {item.detail}" if item.detail else ""
        lines.append(f"{marks.get(item.status, '[?]')} {item.name}{detail}")
    return "\n".join(lines)


def report(items: list[Item]) -> str:
    return f"{sum(1 for i in items if i.status == FAIL)} 项致命，{sum(1 for i in items if i.status == WARN)} 项警告"
