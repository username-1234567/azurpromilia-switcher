"""把需要下载的那部分导出成独立目录，便于离线拷给另一台机器。

两种用法：
- 按计划只导 download 动作的文件，接收方放进目标目录后 `azp fetch` 会跳过；
- `--full` 整客户端搬家（含启动器索引：国服/国际服 `_index`，日服 `manifest.json`），
  给一台什么都没有的机器用，省掉 95 GiB 下载。
"""

from __future__ import annotations

import csv
import os
import shutil
from pathlib import Path


def run_full(source: Path, dest: Path, hardlink: bool = False, dry: bool = False) -> dict:
    """整目录导出。同分区且 --hardlink 时零占用，否则按文件复制。"""
    if not source.is_dir():
        raise FileNotFoundError(f"源目录不存在：{source}")
    src = source.resolve()
    dst_root = dest.resolve()
    if dst_root == src or src in dst_root.parents or dst_root in src.parents:
        raise ValueError("导出目录不能和源目录互相包含")

    files = [p for p in src.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    if dry:
        mode = "硬链接" if hardlink else "复制"
        print(f"  [试运行] 将{mode} {len(files):,} 个文件 / {total / 2**30:.2f} GiB → {dest}")
        print("         接收方装好启动器后把这棵目录放进安装根，目录名按 game_folder_name")
        return {"files": len(files), "bytes": total, "copied": 0, "missing": [], "fell_back": 0}

    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    done = 0
    missing: list[str] = []
    fell_back = 0
    for i, path in enumerate(files, 1):
        rel = path.relative_to(src)
        target = dest / rel
        try:
            size = path.stat().st_size
        except OSError:
            missing.append(str(rel))
            continue
        if target.exists():
            try:
                st = target.stat()
            except OSError:
                st = None
            if st is not None and st.st_size == size and abs(st.st_mtime - path.stat().st_mtime) < 2:
                copied += 1
                done += size
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if hardlink:
                try:
                    os.link(path, target)
                except OSError:
                    shutil.copy2(path, target)
                    fell_back += 1
            else:
                shutil.copy2(path, target)
        except OSError as exc:
            missing.append(f"{rel} ({exc.strerror or exc})")
            continue
        copied += 1
        done += size
        if i % 500 == 0 or i == len(files):
            print(f"  {i:,}/{len(files):,}  {done / 2**30:.2f}/{total / 2**30:.2f} GiB")

    print(f"  完成：{copied:,} 个 / {done / 2**30:.2f} GiB，缺失 {len(missing)} 个")
    if hardlink:
        print("  提示：同分区是硬链接，删这份不影响原客户端")
        if fell_back:
            print(f"  注意：{fell_back} 个文件跨分区退化为真实复制")
    for line in missing[:10]:
        print(f"    ! {line}")
    return {"files": len(files), "bytes": total, "copied": copied, "missing": missing,
            "fell_back": fell_back}


def run(rows: list[dict], source: Path, dest: Path,
        include_links: bool = False, dry: bool = False,
        hardlink: bool = False) -> dict:
    src = source.resolve()
    dst_root = dest.resolve()
    if dst_root == src or src in dst_root.parents or dst_root in src.parents:
        raise ValueError("导出目录不能和源目录互相包含")
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
    return {"files": len(picks), "bytes": total, "copied": copied, "missing": missing,
            "fell_back": fell_back}
