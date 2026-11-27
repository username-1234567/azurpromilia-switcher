"""按清单检查目标目录，只修有问题的文件。

修复有优先级：源区存在同内容的文件就重建硬链接（零下载），否则按清单里的地址重下那一个。
动其余正常文件，也不碰源区。
"""

from __future__ import annotations

import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import fetch, hashes, native, plan


def _url(manifest: dict, info: dict) -> str:
    raw = info.get("url") or ""
    if raw.startswith("http"):
        return raw
    host = manifest.get("cdn_host") or ""
    return f"{host}/{raw.lstrip('/')}"


def _ignored(path: str, suffixes: set[str]) -> bool:
    """清单键可能带 `AzurPromilia_Data/` 前缀，按后缀匹配补丁文件。"""
    return any(path.endswith(s) for s in suffixes if s)


def scan(target: Path, manifest: dict, sample: int = 0, full: bool = False,
         cache: Path | None = None, ignore: set[str] | None = None) -> dict:
    """返回出问题的路径：缺失、大小不符、校验值不符（抽样或全量）。

    `ignore` 是路径后缀集合，命中的文件视为有意保留的改动（如反虚化补丁），
    不参与检查和修复，避免复核把用户装好的补丁覆盖回官方版本。
    """
    files = manifest.get("files") or {}
    # repair 直接读 manifest.json，不经过 plan.build，必须自己再挡一次越界路径：
    # 否则一条 `../../x` 就会让后面的 unlink / os.link / 下载写到目标目录之外
    for path in files:
        plan.safe_rel(path)
    suffixes = set(ignore or ())
    items = [(p, i) for p, i in files.items() if not _ignored(p, suffixes)]
    kind = hashes.kind_of(manifest)
    if full:
        check_scope = dict(items)
    elif sample:
        check_scope = dict(random.sample(items, min(sample, len(items))))
    else:
        check_scope = {}

    # 日服的 CRC64 交给 7za 批量算，Python 逐字节跑 78 GiB 要几十分钟
    table: dict[str, int] = {}
    if check_scope and kind == hashes.CRC64:
        try:
            table = native.crc64_for(target, list(check_scope), cache)
        except native.NativeError as exc:
            print(f"  {exc}")

    missing: list[str] = []
    wrong: list[str] = []
    bad: list[str] = []
    blocked: list[str] = []
    for path, info in items:
        file_path = target / path
        try:
            stat = file_path.stat()
        except FileNotFoundError:
            missing.append(path)
            continue
        except OSError:
            # 被占用/没权限，不能当成缺失去重建，否则会误删好文件
            blocked.append(path)
            continue
        if stat.st_size != info["size"]:
            wrong.append(path)
            continue
        if path not in check_scope:
            continue
        if kind == hashes.CRC64 and table and path in table:
            want = int(info["md5"]) if str(info["md5"]).isdigit() else -1
            if table[path] != want:
                bad.append(path)
        elif hashes.digest(file_path, kind) != info["md5"]:
            # 7za 没算到的路径也走到这里：逐个补算，不能把「没核对」当成「文件坏了」
            bad.append(path)
    return {"missing": missing, "wrong": wrong, "bad": bad, "blocked": blocked,
            "checked": len(check_scope)}


def _base_path(base: Path, path: str, map_path) -> Path | None:
    """源区的目录名可能和这个区不一样，按清单映射找。"""
    for candidate in (path, map_path(path)):
        file_path = base / candidate
        if file_path.exists():
            return file_path
    return None


def _base_has(base: Path | None, path: str, info: dict, kind: str, map_path) -> bool:
    if base is None:
        return False
    file_path = _base_path(base, path, map_path)
    if file_path is None:
        return False
    try:
        if file_path.stat().st_size != info["size"]:
            return False
        if kind == hashes.CRC64:
            got = native.crc64_file(file_path)
            return got is not None and str(got) == str(info["md5"])
        return hashes.digest(file_path, kind) == info["md5"]
    except OSError:
        return False


def run(target: Path, manifest: dict, base: Path | None = None, workers: int = 16,
        sample: int = 20, full: bool = False, dry: bool = False,
        cache: Path | None = None, ignore: set[str] | None = None) -> dict:
    files = manifest.get("files") or {}
    kind = hashes.kind_of(manifest)
    map_path, _prefixes = plan.path_mapper(manifest)
    found = scan(target, manifest, sample=sample, full=full, cache=cache, ignore=ignore)
    broken = found["missing"] + found["wrong"] + found["bad"]
    skipped = [p for p in files if _ignored(p, set(ignore or ()))]
    print(f"  清单 {len(files):,} 个文件")
    if skipped:
        print(f"  跳过已装补丁的文件 {len(skipped)} 个")
    print(f"  缺失 {len(found['missing']):,} 个，大小不符 {len(found['wrong']):,} 个，"
          f"{hashes.label(kind)} 不符 {len(found['bad']):,} 个（核对 {found['checked']:,} 个）")
    for label, paths in (("缺失", found["missing"]), ("大小不符", found["wrong"]),
                         (f"{hashes.label(kind)} 不符", found["bad"])):
        for path in paths[:3]:
            print(f"    {label} {path}")
        if len(paths) > 3:
            print(f"    …共 {len(paths)} 个{label}")
    if found["blocked"]:
        print(f"  读不到 {len(found['blocked']):,} 个（占用或没权限），跳过不修")
        for path in found["blocked"][:3]:
            print(f"    读不到 {path}")
    if not broken:
        print("  没有需要修的")
        return {"linked": 0, "downloaded": 0, "failed": [], "bytes": 0}

    to_link: list[str] = []
    to_fetch: list[str] = []
    for path in broken:
        (to_link if _base_has(base, path, files[path], kind, map_path) else to_fetch).append(path)
    need = sum(files[p]["size"] for p in to_fetch)
    print(f"  可重建硬链接 {len(to_link):,} 个{'' if base else '（没给 --base，全部走下载）'}")
    print(f"  需要下载 {len(to_fetch):,} 个 / {need / 2**30:.2f} GiB")

    if dry:
        print("试运行结束，未改动任何文件。")
        return {"linked": 0, "downloaded": 0, "failed": [], "bytes": need}

    linked = 0
    link_failed: list[tuple[str, str]] = []
    for path in to_link:
        src = _base_path(base, path, map_path)  # type: ignore[arg-type]
        dst = target / path
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.unlink(missing_ok=True)
            os.link(src, dst)
            linked += 1
        except OSError as exc:
            link_failed.append((path, str(exc)))

    pool = fetch.Connections()
    done = 0
    counter = threading.Lock()
    failed: list[tuple[str, str]] = list(link_failed)

    def one(path: str) -> None:
        nonlocal done
        info = files[path]
        ok, msg = fetch.download_one(pool, _url(manifest, info), target / path,
                                     info["size"], info["md5"], kind)
        with counter:  # done += 1 不是原子操作，多线程下会丢计数
            done += 1
            current = done
        if not ok:
            failed.append((path, msg))
        elif current % 20 == 0 or current == len(to_fetch):
            print(f"  下载 {current}/{len(to_fetch)}", flush=True)

    if to_fetch:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            list(executor.map(one, to_fetch))

    fetch_set = set(to_fetch)
    downloaded = len(to_fetch) - len([f for f in failed if f[0] in fetch_set])
    print(f"  完成：重建链接 {linked:,} 个，下载 {downloaded:,} 个 / {need / 2**30:.2f} GiB，"
          f"失败 {len(failed)} 个")
    for path, msg in failed[:10]:
        print(f"    ! {path}: {msg}")
    return {"linked": linked, "downloaded": downloaded, "failed": failed, "bytes": need}
