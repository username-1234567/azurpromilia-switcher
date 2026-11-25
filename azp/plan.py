"""比对两份清单，生成硬链接与增量下载计划。"""

from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class PlanStats:
    link_files: int = 0
    link_bytes: int = 0
    reuse_files: int = 0
    reuse_bytes: int = 0
    download_files: int = 0
    download_bytes: int = 0

    def render(self, target_total: int) -> str:
        lines = [
            f"  硬链接 {self.link_files:,} 个，{self.link_bytes / 2**30:.2f} GiB",
        ]
        if self.reuse_files:
            lines.append(f"  复用 {self.reuse_files:,} 个，{self.reuse_bytes / 2**30:.2f} GiB")
        lines.append(f"  下载 {self.download_files:,} 个，{self.download_bytes / 2**30:.2f} GiB")
        lines.append(
            f"  新增占用 {self.download_bytes / 2**30:.2f} GiB"
            f"（完整安装 {target_total / 2**30:.2f} GiB）"
        )
        return "\n".join(lines)


def _scan_extra(local_dir: Path, base_paths: set[str]) -> dict[tuple[str, int], Path]:
    index: dict[tuple[str, int], Path] = {}
    for dirpath, _dirs, names in os.walk(local_dir):
        for name in names:
            full = Path(dirpath) / name
            rel = full.relative_to(local_dir).as_posix()
            if rel in base_paths:
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            index.setdefault((name, size), full)
    return index


def build(
    base: dict,
    target: dict,
    local_dir: Path | None = None,
) -> tuple[list[dict], PlanStats]:
    a_files = base["files"]
    b_files = target["files"]
    host = target.get("cdn_host") or base.get("cdn_host") or ""
    a_lower = {k.lower(): k for k in a_files}

    extra: dict[tuple[str, int], Path] = {}
    if local_dir and local_dir.is_dir():
        print(f"  扫描本地额外文件 {local_dir}")
        extra = _scan_extra(local_dir, set(a_files))
        print(f"    清单外 {len(extra):,} 项")

    stats = PlanStats()
    rows: list[dict] = []
    for path, info in b_files.items():
        src = a_lower.get(path.lower())
        if src and a_files[src]["md5"] == info["md5"]:
            rows.append({"action": "link", "size": info["size"], "md5": info["md5"],
                         "path": path, "src_or_url": src})
            stats.link_files += 1
            stats.link_bytes += info["size"]
            continue

        cand = extra.get((path.rsplit("/", 1)[-1], info["size"]))
        if cand is not None:
            try:
                if md5_of(cand) == info["md5"]:
                    rows.append({"action": "link_local", "size": info["size"], "md5": info["md5"],
                                 "path": path, "src_or_url": cand.relative_to(local_dir).as_posix()})
                    stats.reuse_files += 1
                    stats.reuse_bytes += info["size"]
                    continue
            except OSError:
                pass

        rel = info.get("url") or ""
        url = rel if rel.startswith("http") else f"{host}/{rel.lstrip('/')}"
        rows.append({"action": "download", "size": info["size"], "md5": info["md5"],
                     "path": path, "src_or_url": url})
        stats.download_files += 1
        stats.download_bytes += info["size"]

    rows.sort(key=lambda r: (r["action"] != "download", -r["size"]))
    return rows, stats


def write_csv(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["action", "size", "md5", "path", "src_or_url"])
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [
            {"action": r["action"], "size": int(r["size"]), "md5": r["md5"],
             "path": r["path"], "src_or_url": r["src_or_url"]}
            for r in csv.DictReader(fh)
        ]
