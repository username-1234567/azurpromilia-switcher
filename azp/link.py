"""按计划建立硬链接树（不复制字节）。"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .journal import Entry


class LinkStats:
    def __init__(self, on_change=None) -> None:
        self.created = 0
        self.skipped = 0
        self.failed: list[tuple[str, str]] = []
        self.bytes = 0
        self._lock = threading.Lock()
        self.on_change = on_change

    def _notify(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self)
            except Exception:  # noqa: BLE001
                pass

    def add_created(self, size: int) -> None:
        with self._lock:
            self.created += 1
            self.bytes += size
        self._notify()

    def add_skipped(self) -> None:
        with self._lock:
            self.skipped += 1
        self._notify()

    def add_failed(self, path: str, msg: str) -> None:
        with self._lock:
            self.failed.append((path, msg))
        self._notify()


def _link_one(src: Path, dst: Path, size: int, stats: LinkStats, dry: bool,
              journal: Entry | None = None) -> None:
    try:
        if dst.exists():
            if dst.stat().st_size == size:
                stats.add_skipped()
                return
            stats.add_failed(str(dst), "目标已存在且大小不同")
            return
        if not src.exists():
            stats.add_failed(str(src), "源文件不存在")
            return
        if src.stat().st_size != size:
            stats.add_failed(str(src), "源文件大小与清单不符")
            return
        if dry:
            stats.add_created(size)
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(src, dst)
        stats.add_created(size)
        if journal is not None:
            journal.add_created(dst, size)
    except OSError as exc:
        hint = ""
        if getattr(exc, "winerror", None) == 17:
            hint = "（硬链接不能跨卷，请把目标放在同一分区）"
        stats.add_failed(str(dst), f"{exc}{hint}")


def run(rows: list[dict], base_dir: Path, target_dir: Path,
        local_dir: Path | None = None, workers: int = 8, dry: bool = False,
        journal: Entry | None = None, on_change=None) -> LinkStats:
    jobs: list[tuple[Path, Path, int]] = []
    for row in rows:
        if row["action"] == "link":
            src = base_dir / row["src_or_url"]
        elif row["action"] == "link_local" and local_dir is not None:
            src = local_dir / row["src_or_url"]
        else:
            continue
        jobs.append((src, target_dir / row["path"], row["size"]))

    stats = LinkStats(on_change)
    if not jobs:
        print("  没有需要建立链接的文件")
        return stats

    print(f"  {'[试运行] ' if dry else ''}建立 {len(jobs)} 个硬链接")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda j: _link_one(j[0], j[1], j[2], stats, dry, journal), jobs))

    print(f"  完成：新建 {stats.created} 个 / {stats.bytes / 2**30:.2f} GiB，"
          f"已存在跳过 {stats.skipped} 个，失败 {len(stats.failed)} 个")
    for path, msg in stats.failed[:10]:
        print(f"    ! {path}: {msg}")
    return stats
