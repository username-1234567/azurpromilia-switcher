"""删除一个区的客户端目录。

共用文件是硬链接，删目标目录只是减一层引用，源区文件还在；只有该区独有的文件才真正释放空间。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

CLIENT_MARKS = ("_index", "AzurPromilia_Data")


@dataclass
class Scan:
    files: int = 0
    total: int = 0
    base_files: int = 0
    base_bytes: int = 0
    other_files: int = 0
    other_bytes: int = 0
    own_files: int = 0
    own_bytes: int = 0


def _same_file(base: Path, rel: str, st: os.stat_result) -> bool:
    """目标文件与源区同路径文件是否同一份数据（同卷比对文件索引）。"""
    try:
        other = (base / rel).stat()
    except OSError:
        return False
    return (other.st_dev, other.st_ino) == (st.st_dev, st.st_ino)


def scan(root: Path, base: Path | None = None) -> Scan:
    """统计待删目录：与源区共用、与别处共用、本区独占。

    硬链接数大于 1 只说明「还有别的目录项指向这份数据」，未必是源区，
    所以给了 base 就按文件索引比对，避免把暂存/备份目录也算成源区。
    """
    stats = Scan()
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            stats.files += 1
            stats.total += st.st_size
            if st.st_nlink <= 1:
                stats.own_files += 1
                stats.own_bytes += st.st_size
                continue
            rel = path.relative_to(root).as_posix()
            if base is not None and _same_file(base, rel, st):
                stats.base_files += 1
                stats.base_bytes += st.st_size
            else:
                stats.other_files += 1
                stats.other_bytes += st.st_size
    return stats


def looks_like_client(root: Path) -> bool:
    return any((root / mark).exists() for mark in CLIENT_MARKS)


def check_target(target: Path, base: Path | None, force: bool) -> str:
    """返回拒绝理由，空串表示可以继续。"""
    if not target.exists():
        return f"目录不存在：{target}"
    if not target.is_dir():
        return f"不是目录：{target}"
    if len(target.parts) < 3:
        return f"路径太浅，拒绝删除：{target}"

    root = target.resolve()
    if base is not None:
        base_root = base.resolve()
        if root == base_root:
            return f"这是源区目录，不能删：{root}"
        if base_root.is_relative_to(root):
            return f"源区目录在待删目录里面，会一起删掉：{base_root}"
        if root.is_relative_to(base_root):
            return f"待删目录在源区目录里面，会破坏源区：{root}"

    if not looks_like_client(target) and not force:
        return f"不像客户端目录（没有 {' 或 '.join(CLIENT_MARKS)}），确认无误可加 --force"
    return ""


def _delete_tree(root: Path, errors: list[str]) -> None:
    for dirpath, dirnames, names in os.walk(root, topdown=False):
        for name in names:
            path = Path(dirpath) / name
            try:
                path.unlink()
            except OSError:
                try:
                    path.chmod(0o666)
                    path.unlink()
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
        for name in dirnames:
            path = Path(dirpath) / name
            try:
                path.rmdir()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError as exc:
        errors.append(f"{root}: {exc}")


def _confirm(target: Path) -> bool:
    if not sys.stdin.isatty():
        print("当前不是交互终端，确认无误请加 --yes 再执行。")
        return False
    try:
        answer = input(f"确认删除 {target} (y/N): ").strip().lower()
    except EOFError:
        return False
    return answer.startswith("y")


def run(target: Path, base: Path | None = None, yes: bool = False,
        dry: bool = False, force: bool = False) -> int:
    reason = check_target(target, base, force)
    if reason:
        print(reason)
        return 1

    stats = scan(target, base)
    print(f"目标 {target}")
    print(f"  文件 {stats.files:,} 个，合计 {stats.total / 2**30:.2f} GiB")
    if stats.base_files or base is not None:
        print(f"  与源区共用 {stats.base_files:,} 个 / {stats.base_bytes / 2**30:.2f} GiB，"
              f"删掉不影响源区，也不释放空间")
    if stats.other_files:
        why = "同一份数据还有别的目录项（暂存、备份、或源区里改了名的副本）"
        print(f"  与别处共用 {stats.other_files:,} 个 / {stats.other_bytes / 2**30:.2f} GiB，{why}")
    print(f"  本区独占 {stats.own_files:,} 个 / {stats.own_bytes / 2**30:.2f} GiB，删除即释放")
    if base is None and stats.other_files:
        print("  提示：加 --base 指定源区目录，可以把「与源区共用」和「链接在别处」分开算")
    if stats.other_bytes:
        print("  注意：别处还持有硬链接的文件，删完这里空间不会变，要连那些目录一起删才释放")
    if base is not None:
        print(f"  源区 {base} 不会被改动")

    if dry:
        print("试运行结束，未删除任何文件。")
        return 0
    if not yes and not _confirm(target):
        print("已取消。")
        return 1

    errors: list[str] = []
    _delete_tree(target, errors)
    done = stats.files - len(errors)
    if errors:
        print(f"删除时遇到 {len(errors)} 个问题，已删 {done:,} 个文件")
        for line in errors[:10]:
            print(f"  ! {line}")
        print("可能有文件正被启动器或游戏占用，关掉后重跑一次即可。")
        return 2

    print(f"已删除 {done:,} 个文件，空间释放约 {stats.own_bytes / 2**30:.2f} GiB")
    if stats.other_bytes:
        print(f"另有 {stats.other_bytes / 2**30:.2f} GiB 仍被别处的硬链接占着，"
              f"删掉那些目录后才会释放")
    if target.exists():
        print(f"目录没能完全删掉，可能还有文件被占用：{target}")
        return 2
    return 0
