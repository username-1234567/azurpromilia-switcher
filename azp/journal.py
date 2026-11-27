"""操作账本与回退。

每次往目标目录写东西之前记一笔：新建了哪些文件、覆盖了哪些文件（覆盖前先备份到
out/backup/）。出问题就用 `python -m azp rollback` 按账本撤销。

能退的：本次新建的文件、本次覆盖掉的 `_index`。
退不了的：被删掉的文件、被别的程序（比如启动器更新）改写的文件——那种情况只能靠
启动器自带的「校验修复」重新下回来。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path("out")
JOURNAL_DIR = ROOT / "journal"
BACKUP_DIR = ROOT / "backup"


@dataclass
class Entry:
    cmd: str
    target: Path
    created: list[dict] = field(default_factory=list)
    backups: list[dict] = field(default_factory=list)
    _path: Path | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_created(self, path: Path, size: int) -> None:
        with self._lock:
            self.created.append({"path": str(path.absolute()), "size": size})

    def backup(self, path: Path) -> None:
        """覆盖前留一份。文件不存在就不用备。"""
        if not path.exists():
            return
        with self._lock:
            self._backup_locked(path)

    def _backup_locked(self, path: Path) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest_dir = BACKUP_DIR / stamp
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{path.parent.name}_{path.name}"
        n = 1
        while dest.exists():
            dest = dest_dir / f"{path.parent.name}_{path.name}.{n}"
            n += 1
        shutil.copy2(path, dest)
        self.backups.append({"orig": str(path.absolute()), "saved": str(dest.resolve())})

    def save(self) -> Path | None:
        """写账本。同一个 Entry 多次调用会覆盖同一个文件，方便中断后保住记录。"""
        with self._lock:
            if not self.created and not self.backups:
                return None
            if self._path is None:
                JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                path = JOURNAL_DIR / f"{stamp}-{self.cmd}.json"
                n = 1
                while path.exists():
                    # 同一秒里跑两笔要能分开，否则后一笔的第一次保存会把前一笔的账本覆盖掉
                    path = JOURNAL_DIR / f"{stamp}-{self.cmd}.{n}.json"
                    n += 1
                self._path = path
            payload = {
                "cmd": self.cmd,
                "target": str(self.target.absolute()),
                "time": time.time(),
                "created": list(self.created),
                "backups": list(self.backups),
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
            os.replace(tmp, self._path)
            return self._path


def load(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))


def available(target: Path | None = None) -> list[Path]:
    if not JOURNAL_DIR.exists():
        return []
    files = sorted(JOURNAL_DIR.glob("*.json"))
    if target is None:
        return files
    keep = []
    for path in files:
        try:
            data = load(path)
        except (OSError, ValueError):
            continue
        try:
            # 账本里写的是 absolute()，这里也要按同一基准比，否则相对路径永远对不上
            same = (Path(data["target"]).resolve()
                    == target.absolute().resolve())
        except (KeyError, OSError):
            same = False
        if same:
            keep.append(path)
    return keep


def find(target: Path | None, ident: str | None) -> Path | None:
    if ident:
        name = str(ident)
        # --id 只认账本文件名。直接拼路径的话，`--id ../../x.json` 能读到任意 JSON，
        # 里面的 created/backups 又会被 rollback 当成删除与覆盖的依据
        if name != Path(name).name or name in (".", ".."):
            return None
        path = JOURNAL_DIR / name
        return path if path.exists() else None
    files = available(target)
    return files[-1] if files else None


def rollback(path: Path, dry: bool = False) -> tuple[int, int, int, list[str]]:
    """按账本撤销。返回 (删掉的文件数, 还原的备份数, 保留未动数, 说明)。"""
    data = load(path)
    root = Path(data["target"])
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    cutoff = float(data["time"]) + 120  # 给时钟误差留点余量

    removed = skipped = 0
    notes: list[str] = []
    parents: set[Path] = set()

    for item in reversed(data["created"]):
        file_path = Path(item["path"])
        try:
            resolved = file_path.resolve()
        except OSError:
            continue
        if root_resolved not in resolved.parents:
            notes.append(f"不在目标目录内，跳过 {file_path}")
            skipped += 1
            continue
        if not file_path.exists():
            continue
        try:
            stat = file_path.stat()
        except OSError:
            continue
        if item.get("size") and stat.st_size != item["size"]:
            notes.append(f"大小已变，保留 {file_path.name}")
            skipped += 1
            continue
        if stat.st_mtime > cutoff:
            notes.append(f"操作之后被改过，保留 {file_path.name}")
            skipped += 1
            continue
        if not dry:
            try:
                file_path.unlink()
            except OSError as exc:
                notes.append(f"删不掉 {file_path}: {exc}")
                skipped += 1
                continue
        removed += 1
        parents.add(file_path.parent)

    restored = 0
    for item in data["backups"]:
        saved, orig = Path(item["saved"]), Path(item["orig"])
        # 账本是磁盘上可编辑的 JSON：还原目标和 created 一样必须限制在目标目录内，
        # 否则一条 `"orig": "C:/Windows/..."` 就会变成任意文件覆盖
        try:
            orig_resolved = orig.resolve()
        except OSError:
            notes.append(f"备份目标解析不了，跳过 {orig}")
            skipped += 1
            continue
        if orig_resolved != root_resolved and root_resolved not in orig_resolved.parents:
            notes.append(f"备份目标不在目标目录内，跳过 {orig}")
            skipped += 1
            continue
        if not saved.exists():
            notes.append(f"备份缺失 {saved}")
            continue
        if not dry:
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, orig)
        restored += 1

    if not dry:
        for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
            cur = parent
            while cur != root_resolved and cur.exists():
                try:
                    cur.rmdir()
                except OSError:
                    break
                cur = cur.parent

    return removed, restored, skipped, notes
