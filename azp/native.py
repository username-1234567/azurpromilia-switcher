"""CRC64 加速：借启动器自带的 7za 批量算。

日服清单用 CRC-64/XZ。Python 逐字节算 46 GiB 要几十分钟，7za 是原生实现
（本机实测约 600 MB/s），而且两个区的启动器目录里都带着它，不算额外依赖。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

LINE = re.compile(r"^([0-9A-Fa-f]{16}) +(\d+) +(.+)$")
NAMES = ("7za.exe", "7z.exe", "7za", "7z")


class NativeError(RuntimeError):
    pass


_CACHED: Path | None = None
_LOOKED = False


def find() -> Path | None:
    """环境变量 → 内置 7za → PATH → 启动器目录，按这个顺序找。找到一次就记住。"""
    global _CACHED, _LOOKED
    if _LOOKED:
        return _CACHED
    _LOOKED = True
    _CACHED = _find()
    return _CACHED


def bundled_7za() -> Path:
    """随程序附带的 7za 路径；源码模式与 PyInstaller 解压目录都适用。"""
    return Path(__file__).resolve().parent / "tools" / "7za.exe"


def _find() -> Path | None:
    env = os.environ.get("AZP_7ZA")
    if env:
        candidate = Path(env)
        if candidate.exists():
            return candidate
    # 随程序附带的 7za：源码模式是 azp/tools/7za.exe，
    # PyInstaller 打包后是 _MEIPASS/azp/tools/7za.exe
    bundled = bundled_7za()
    if bundled.is_file():
        return bundled
    for name in NAMES:
        hit = shutil.which(name)
        if hit:
            return Path(hit)
    from . import detect

    try:
        installs = detect.find_installs()
    except Exception:  # noqa: BLE001
        return None
    for inst in installs:
        for rel in (Path("tools") / "7za.exe", Path("7z.exe")):
            candidate = inst.root / inst.version / rel
            if candidate.exists():
                return candidate
    return None


def crc64_dir(exe: Path, folder: Path, names: list[str] | None = None) -> dict[str, int]:
    """算一个目录下的 CRC64。

    给了 names 就只算这些文件（在目录里按名字调用，不递归），否则整个目录递归扫。
    小分组必须走前者：目录根上几个文件会让 7za 把整棵 79 GiB 的树读一遍。
    """
    if not folder.is_dir():
        return {}
    if names is None:
        cmd = [str(exe), "h", "-scrcCRC64", "-sccUTF-8", "-r", f"{folder}{os.sep}*"]
        cwd = None
    else:
        cmd = [str(exe), "h", "-scrcCRC64", "-sccUTF-8", *names]
        cwd = str(folder)
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NativeError(f"7za 调用失败：{exc}") from None
    text = proc.stdout.decode("utf-8", "replace")
    table: dict[str, int] = {}
    for line in text.splitlines():
        hit = LINE.match(line.rstrip())
        if hit:
            table[hit.group(3).replace("\\", "/").lower()] = int(hit.group(1), 16)
    if not table and proc.returncode != 0:
        tail = text.strip().splitlines()[-1:] or ["（无输出）"]
        raise NativeError(f"7za 返回 {proc.returncode}：{tail[0]}")
    return table


def crc64_file(path: Path, exe: Path | None = None) -> int | None:
    """单个文件的 CRC64，找不到 7za 就返回 None 让上层退到纯 Python。"""
    exe = exe or find()
    if exe is None:
        return None
    table = crc64_dir(exe, path.parent, [path.name])
    if not table:
        return None
    # 不能用 `a or b`：CRC 值合法地等于 0 时会被当成「没算到」
    got = table.get(path.name.lower())
    return table.get(path.name) if got is None else got


def _load(cache: Path | None) -> dict[str, list]:
    if cache is None or not cache.exists():
        return {}
    try:
        data = json.loads(cache.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cache_key(base_dir: Path, rel: str) -> str:
    root = os.path.normcase(str(base_dir.resolve()))
    return f"{root}|{rel.lower()}"


def crc64_for(base_dir: Path, rels: list[str], cache: Path | None = None,
              exe: Path | None = None) -> dict[str, int]:
    """按需算 CRC64，命中缓存就不读盘。"""
    if not rels:
        return {}
    exe = exe or find()
    if exe is None:
        raise NativeError(
            "没有找到 7za（7-Zip）。装一个，或用 AZP_7ZA 指向 7za.exe，"
            "也可以加 --trust-names 跳过内容核对"
        )

    stored = _load(cache)
    result: dict[str, int] = {}
    todo: list[str] = []
    for rel in rels:
        key = _cache_key(base_dir, rel)
        try:
            stat = (base_dir / rel).stat()
        except OSError:
            continue
        hit = stored.get(key)
        if hit and len(hit) == 3 and hit[0] == stat.st_size and hit[1] == stat.st_mtime_ns:
            result[rel] = int(hit[2])
        else:
            todo.append(rel)

    if todo:
        groups: dict[str, list[str]] = {}
        for rel in todo:
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            groups.setdefault(parent, []).append(rel)
        print(f"  用 7za 核对 {len(todo):,} 个文件（{len(groups)} 个目录）")
        for parent, items in sorted(groups.items()):
            folder = base_dir / parent if parent else base_dir
            prefix = f"{parent}/".lower() if parent else ""
            names = [rel[len(prefix):] for rel in items] if prefix else list(items)
            got = 0
            # 命令行长度有限，分批点名；不要退化成整目录递归——那会把整棵几十 GiB 的
            # 树读一遍，而且递归输出的是带目录的全路径，和这里的文件名对不上。
            for start in range(0, len(names), 200):
                batch = names[start:start + 200]
                table = crc64_dir(exe, folder, batch)
                table = {k.lower(): v for k, v in table.items()}
                got += _collect(base_dir, items, prefix, table, result, stored)
            print(f"    {parent or '.'}：{got}/{len(items)}")
        if cache is not None:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(stored, separators=(",", ":")), "utf-8")
            except OSError:
                pass
    return result


def _collect(base_dir: Path, items: list[str], prefix: str, table: dict[str, int],
             result: dict[str, int], stored: dict[str, list]) -> int:
    got = 0
    for rel in items:
        name = rel[len(prefix):].lower() if prefix else rel.lower()
        crc = table.get(name)
        if crc is None:
            continue
        result[rel] = crc
        got += 1
        try:
            stat = (base_dir / rel).stat()
        except OSError:
            continue
        stored[_cache_key(base_dir, rel)] = [stat.st_size, stat.st_mtime_ns, crc]
    return got

