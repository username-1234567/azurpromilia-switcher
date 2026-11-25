"""把需要下载的那部分导出成独立目录，便于离线拷给另一台机器。

只导 download 动作的文件。接收方把它们放进目标目录后再跑 `azp fetch`，
会因大小和 MD5 一致直接跳过。
"""

from __future__ import annotations

import csv
import os
import shutil
from pathlib import Path


def run(rows: list[dict], source: Path, dest: Path,
        include_links: bool = False, dry: bool = False,
        hardlink: bool = False) -> dict:
    actions = {"download", "link", "link_local"} if include_links else {"download"}
    picks = [r for r in rows if r["action"] in actions]
    total = sum(r["size"] for r in picks)

    if dry:
        mode = "硬链接" if hardlink else "导出"
        print(f"  [试运行] 将{mode} {len(picks)} 个文件 / {total / 2**30:.2f} GiB → {dest}")
        if hardlink:
            print("         硬链接方式：同一分区内几乎不占空间，适合先做个暂存目录再往外发")
        return {"files": len(picks), "bytes": total, "copied": 0, "missing": []}

    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    done_bytes = 0
    missing: list[str] = []
    fell_back = 0

    for i, row in enumerate(picks, 1):
        src = source / row["path"]
        dst = dest / row["path"]
        if not src.exists():
            missing.append(row["path"])
            continue
        if dst.exists() and dst.stat().st_size == row["size"]:
            copied += 1
            done_bytes += row["size"]
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if hardlink:
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
                fell_back += 1
        else:
            shutil.copy2(src, dst)
        copied += 1
        done_bytes += row["size"]
        if i % 50 == 0 or i == len(picks):
            print(f"  {i}/{len(picks)}  {done_bytes / 2**30:.2f}/{total / 2**30:.2f} GiB")

    with (dest / "_handoff.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["path", "size", "md5"])
        for row in picks:
            writer.writerow([row["path"], row["size"], row["md5"]])

    print(f"  完成：{copied} 个 / {done_bytes / 2**30:.2f} GiB，缺失 {len(missing)} 个")
    if hardlink:
        print("  提示：这是硬链接暂存目录，删除它不会影响原文件；原文件删除后数据同样还在")
        if fell_back:
            print(f"  注意：有 {fell_back} 个文件因跨分区退化为真实复制")
    for path in missing[:10]:
        print(f"    ! 源文件缺失 {path}")
    return {"files": len(picks), "bytes": total, "copied": copied, "missing": missing}
