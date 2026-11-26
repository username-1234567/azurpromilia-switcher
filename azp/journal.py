"""操作账本与回退。

每次往目标目录写东西之前记一笔：新建了哪些文件、覆盖了哪些文件（覆盖前先备份到
out/backup/）。出问题就用 `azp rollback` 按账本撤销。

能退的：本次新建的文件、本次覆盖掉的 `_index`。
退不了的：被删掉的文件、被别的程序（比如启动器更新）改写的文件——那种情况只能靠
启动器自带的「校验修复」重新下回来。
"""

from __future__ import annotations

import json
import shutil
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

    def add_created(self, path: Path, size: int) -> None:
        self.created.append({"path": str(path), "size": size})

    def backup(self, path: Path) -> None:
        """覆盖前留一份。文件不存在就不用备。"""
        if not path.exists():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest_dir = BACKUP_DIR / stamp
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{path.parent.name}_{path.name}"
        n = 1
        while dest.exists():
            dest = dest_dir / f"{path.parent.name}_{path.name}.{n}"
            n += 1
        shutil.copy2(path, dest)
        self.backups.append({"orig": str(path), "saved": str(dest)})

    def save(self) -> Path | None:
        if not self.created and not self.backups:
            return None
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = JOURNAL_DIR / f"{stamp}-{self.cmd}.json"
        n = 1
        while path.exists():
            path = JOURNAL_DIR / f"{stamp}-{self.cmd}.{n}.json"
            n += 1
        payload = {
            "cmd": self.cmd,
            "target": str(self.target),
            "time": time.time(),
            "created": self.created,
            "backups": self.backups,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        return path


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
            same = Path(data["target"]).resolve() == target.resolve()
        except (KeyError, OSError):
            same = False
        if same:
            keep.append(path)
    return keep


def find(target: Path | None, ident: str | None) -> Path | None:
    if ident:
        path = JOURNAL_DIR / ident
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
