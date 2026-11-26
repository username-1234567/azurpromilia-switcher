"""为目标客户端补齐启动器需要的 _index 缓存。

启动器靠 _index/<versionCode> 判断这个版本是否装过。没有它，文件齐全也会被当成
未安装，从头再下一遍。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .journal import Entry
from .manifest import encrypt_index


def write(target_game_dir: Path, version_code: str, version_name: str,
          index_csv: Path, paths_from: Path | None = None,
          journal: Entry | None = None) -> list[Path]:
    idx = target_game_dir / "_index"
    idx.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    version_file = idx / "version"
    if journal is not None:
        journal.backup(version_file)
    version_file.write_text(
        json.dumps({"version": version_code, "versionName": version_name}, separators=(",", ":")),
        encoding="utf-8",
    )
    written.append(version_file)

    text = index_csv.read_text(encoding="utf-8")
    blob = encrypt_index(text)
    index_file = idx / version_code
    if journal is not None:
        journal.backup(index_file)
    index_file.write_bytes(blob)
    written.append(index_file)

    paths_file = idx / "paths"
    source = None
    if paths_from is not None:
        candidate = paths_from / "_index" / "paths"
        if candidate.exists():
            source = candidate
    if source is not None:
        if journal is not None:
            journal.backup(paths_file)
        shutil.copyfile(source, paths_file)
        written.append(paths_file)
    elif not paths_file.exists():
        paths_file.write_text("{}", encoding="utf-8")
        written.append(paths_file)

    return written
