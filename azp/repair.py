"""按清单检查目标目录，只修有问题的文件。

修复有优先级：源区存在同 MD5 的文件就重建硬链接（零下载），否则按清单里的地址重下那一个。
不动其余正常文件，也不碰源区。
"""

from __future__ import annotations

import os
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import fetch, plan


def _url(manifest: dict, info: dict) -> str:
    raw = info.get("url") or ""
    if raw.startswith("http"):
        return raw
    host = manifest.get("cdn_host") or ""
    return f"{host}/{raw.lstrip('/')}"


def scan(target: Path, manifest: dict, sample: int = 0, full: bool = False) -> dict:
    """返回出问题的路径：缺失、大小不符、MD5 不符（抽样或全量）。"""
    files = manifest.get("files") or {}
    items = list(files.items())
    if full:
        md5_scope = dict(items)
    elif sample:
        md5_scope = dict(random.sample(items, min(sample, len(items))))
    else:
        md5_scope = {}

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
        if path in md5_scope and plan.md5_of(file_path) != info["md5"]:
            bad.append(path)
    return {"missing": missing, "wrong": wrong, "bad": bad, "blocked": blocked,
            "checked_md5": len(md5_scope)}


def _base_has(base: Path | None, path: str, info: dict) -> bool:
    if base is None:
        return False
    file_path = base / path
    try:
        if file_path.stat().st_size != info["size"]:
            return False
        return plan.md5_of(file_path) == info["md5"]
    except OSError:
        return False


def run(target: Path, manifest: dict, base: Path | None = None, workers: int = 16,
        sample: int = 20, full: bool = False, dry: bool = False) -> dict:
    files = manifest.get("files") or {}
    found = scan(target, manifest, sample=sample, full=full)
    broken = found["missing"] + found["wrong"] + found["bad"]
    print(f"  清单 {len(files):,} 个文件")
    print(f"  缺失 {len(found['missing']):,} 个，大小不符 {len(found['wrong']):,} 个，"
          f"MD5 不符 {len(found['bad']):,} 个（核对 {found['checked_md5']:,} 个）")
    for label, paths in (("缺失", found["missing"]), ("大小不符", found["wrong"]), ("MD5 不符", found["bad"])):
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
        (to_link if _base_has(base, path, files[path]) else to_fetch).append(path)
    need = sum(files[p]["size"] for p in to_fetch)
    print(f"  可重建硬链接 {len(to_link):,} 个{'' if base else '（没给 --base，全部走下载）'}")
    print(f"  需要下载 {len(to_fetch):,} 个 / {need / 2**30:.2f} GiB")

    if dry:
        print("试运行结束，未改动任何文件。")
        return {"linked": 0, "downloaded": 0, "failed": [], "bytes": need}

    linked = 0
    link_failed: list[tuple[str, str]] = []
    for path in to_link:
        src = base / path  # type: ignore[operator]
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
    failed: list[tuple[str, str]] = list(link_failed)

    def one(path: str) -> None:
        nonlocal done
        info = files[path]
        ok, msg = fetch.download_one(pool, _url(manifest, info), target / path,
                                     info["size"], info["md5"])
        done += 1
        if not ok:
            failed.append((path, msg))
        elif done % 20 == 0 or done == len(to_fetch):
            print(f"  下载 {done}/{len(to_fetch)}", flush=True)

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
