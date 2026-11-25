"""按计划建立硬链接树（不复制字节）。"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class LinkStats:
    def __init__(self) -> None:
        self.created = 0
        self.skipped = 0
        self.failed: list[tuple[str, str]] = []
        self.bytes = 0


def _link_one(src: Path, dst: Path, size: int, stats: LinkStats, dry: bool) -> None:
    try:
        if dst.exists():
            if dst.stat().st_size == size:
                stats.skipped += 1
                return
            stats.failed.append((str(dst), "目标已存在且大小不同"))
            return
        if not src.exists():
            stats.failed.append((str(src), "源文件不存在"))
            return
        if src.stat().st_size != size:
            stats.failed.append((str(src), "源文件大小与清单不符"))
            return
        if dry:
            stats.created += 1
            stats.bytes += size
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(src, dst)
        stats.created += 1
        stats.bytes += size
    except OSError as exc:
        hint = ""
        if getattr(exc, "winerror", None) == 17:
            hint = "（硬链接不能跨卷，请把目标放在同一分区）"
        stats.failed.append((str(dst), f"{exc}{hint}"))


def run(rows: list[dict], base_dir: Path, target_dir: Path,
        local_dir: Path | None = None, workers: int = 8, dry: bool = False) -> LinkStats:
    jobs: list[tuple[Path, Path, int]] = []
    for row in rows:
        if row["action"] == "link":
            src = base_dir / row["src_or_url"]
        elif row["action"] == "link_local" and local_dir is not None:
            src = local_dir / row["src_or_url"]
        else:
            continue
        jobs.append((src, target_dir / row["path"], row["size"]))

    stats = LinkStats()
    if not jobs:
        print("  没有需要建立链接的文件")
        return stats

    print(f"  {'[试运行] ' if dry else ''}建立 {len(jobs)} 个硬链接")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda j: _link_one(j[0], j[1], j[2], stats, dry), jobs))

    print(f"  完成：新建 {stats.created} 个 / {stats.bytes / 2**30:.2f} GiB，"
          f"已存在跳过 {stats.skipped} 个，失败 {len(stats.failed)} 个")
    for path, msg in stats.failed[:10]:
        print(f"    ! {path}: {msg}")
    return stats
