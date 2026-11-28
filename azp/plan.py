"""比对两份清单，生成硬链接与增量下载计划。"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from . import hashes, native
from .hashes import md5_of

FIELDS = ["action", "size", "md5", "path", "src_or_url", "hash_kind"]


def safe_rel(path: str) -> str:
    """清单和计划表都来自网路或手工编辑，路径必须先挡住绝对路径和 `..`。

    不挡住的话，`link` / `fetch` / `repair` 里的 `target / row["path"]` 能把文件
    写到目标目录外面（比如 `../../Windows/...`），删除类操作也会跟着越界。
    """
    text = str(path).replace("\\", "/").strip()
    win = PureWindowsPath(text)
    parts = [p for p in text.split("/") if p]
    if not text or not parts or win.drive or win.root or ".." in parts:
        raise ValueError(f"清单里有非法相对路径，已拒绝：{path!r}")
    return "/".join(parts)


def _as_int(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def path_mapper(target: dict, base: dict | None = None):
    """目录名换算规则：把目标区的路径换成基区里的路径。

    `path_map` 记的是「自己 → 另一套名字」。所以目标区那份直接可用；基区那份要反过来用，
    否则拿日服当源区、台服当目标时，一条都对不上（日服在 `AzurPromiliaJP_Data` 下）。
    """
    rules: list[tuple[str, str]] = []

    def add(items, invert: bool = False) -> None:
        for item in items or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                first, second = str(item[0]), str(item[1])
                rules.append((second, first) if invert else (first, second))

    add(target.get("path_map"))
    if base is not None:
        add(base.get("path_map"), invert=True)

    def apply(path: str) -> str:
        for src, dst in rules:
            if path == src:
                return dst
            if src.endswith("/") and path.startswith(src):
                return dst + path[len(src):]
        return path

    return apply, [r[0].rstrip("/") for r in rules]


@dataclass
class PlanStats:
    link_files: int = 0
    link_bytes: int = 0
    reuse_files: int = 0
    reuse_bytes: int = 0
    ready_files: int = 0
    ready_bytes: int = 0
    download_files: int = 0
    download_bytes: int = 0

    def render(self, target_total: int) -> str:
        lines = [
            f"  硬链接 {self.link_files:,} 个，{self.link_bytes / 2**30:.2f} GiB",
        ]
        if self.reuse_files:
            lines.append(f"  复用 {self.reuse_files:,} 个，{self.reuse_bytes / 2**30:.2f} GiB")
        if self.ready_files:
            lines.append(
                f"  已就绪 {self.ready_files:,} 个，{self.ready_bytes / 2**30:.2f} GiB"
                "（目标区已有同大小文件，不重新下载）"
            )
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
    base_dir: Path | None = None,
    cache: Path | None = None,
    trust_names: bool = False,
    existing: Path | None = None,
) -> tuple[list[dict], PlanStats]:
    """比对两份清单，生成计划。

    `existing` 是已装好的目标目录：其中大小已符合清单的文件记为 `ready`，
    不再下载。目标目录即将被清空（`--reinstall`）时不要传。
    """
    a_files = base["files"]
    b_files = target["files"]
    # 两侧清单都是远端数据：目标侧决定写哪，源侧决定读哪、链哪，都得挡住越界路径
    for path in list(a_files) + list(b_files):
        safe_rel(path)
    host = target.get("cdn_host") or base.get("cdn_host") or ""
    a_lower = {k.lower(): k for k in a_files}
    a_kind = hashes.kind_of(base)
    b_kind = hashes.kind_of(target)
    cross_kind = a_kind != b_kind
    map_path, prefixes = path_mapper(target, base)
    if prefixes:
        print(f"  路径映射 {len(prefixes)} 条：{'、'.join(prefixes)}")

    extra: dict[tuple[str, int], Path] = {}
    if local_dir and local_dir.is_dir():
        print(f"  扫描本地额外文件 {local_dir}")
        extra = _scan_extra(local_dir, set(a_files))
        print(f"    清单外 {len(extra):,} 项")

    # 先找「路径对得上、大小也一致」的候选，再决定要不要核对内容
    pairs: list[tuple[str, str, dict]] = []
    for path, info in b_files.items():
        src = a_lower.get(path.lower())
        if src is None:
            mapped = map_path(path)
            if mapped != path:
                src = a_lower.get(mapped.lower())
        if src is not None and a_files[src]["size"] == info["size"]:
            pairs.append((path, src, info))

    src_of = {p: s for p, s, _i in pairs}
    verified: set[str] = set()
    if pairs and not cross_kind:
        verified = {p for p, s, i in pairs if a_files[s]["md5"] == i["md5"]}
    elif pairs and cross_kind:
        pool = sum(i["size"] for _p, _s, i in pairs)
        if trust_names:
            verified = {p for p, _s, _i in pairs}
            print(f"  同名同大小 {len(pairs):,} 个 / {pool / 2**30:.2f} GiB，"
                  "已按 --trust-names 直接采用，未核对内容")
        elif base_dir is None:
            print(f"  校验方式不同（{hashes.label(a_kind)} / {hashes.label(b_kind)}），"
                  "没给 --base-dir 无法核对内容，这 %d 个按下载处理" % len(pairs))
        elif b_kind == hashes.MD5:
            # 源区是 CRC64、目标区是 MD5：只能用 MD5 算源区文件，才能和目标清单比
            print(f"  校验方式不同（{hashes.label(a_kind)} / {hashes.label(b_kind)}），"
                  f"用 MD5 核对 {len(pairs):,} 个同名同大小文件 / {pool / 2**30:.2f} GiB")
            verified = set()
            for n, (p, s, i) in enumerate(pairs, 1):
                try:
                    if hashes.md5_of(base_dir / s) == i["md5"]:
                        verified.add(p)
                except OSError:
                    pass
                if n % 2000 == 0:
                    print(f"    {n:,}/{len(pairs):,}")
            print(f"  内容一致 {len(verified):,} 个，不一致 {len(pairs) - len(verified):,} 个")
        else:
            print(f"  校验方式不同（{hashes.label(a_kind)} / {hashes.label(b_kind)}），"
                  f"核对 {len(pairs):,} 个同名同大小文件 / {pool / 2**30:.2f} GiB")
            try:
                table = native.crc64_for(base_dir, [s for _p, s, _i in pairs], cache)
            except native.NativeError as exc:
                print(f"  核对失败：{exc}")
                table = {}
            verified = {p for p, s, i in pairs
                        if table.get(s) is not None and table[s] == _as_int(i["md5"])}
            print(f"  内容一致 {len(verified):,} 个，不一致 {len(pairs) - len(verified):,} 个")

    stats = PlanStats()
    rows: list[dict] = []
    for path, info in b_files.items():
        if path in verified:
            src = src_of[path]
            rows.append({"action": "link", "size": info["size"], "md5": info["md5"],
                         "path": path, "src_or_url": src, "hash_kind": b_kind})
            stats.link_files += 1
            stats.link_bytes += info["size"]
            continue

        cand = extra.get((path.rsplit("/", 1)[-1], info["size"]))
        if cand is not None:
            try:
                if b_kind == hashes.CRC64:
                    got = native.crc64_file(cand)
                    same = got is not None and str(got) == str(info["md5"])
                else:
                    same = hashes.digest(cand, b_kind) == info["md5"]
                if same:
                    rows.append({"action": "link_local", "size": info["size"], "md5": info["md5"],
                                 "path": path, "src_or_url": cand.relative_to(local_dir).as_posix(),
                                 "hash_kind": b_kind})
                    stats.reuse_files += 1
                    stats.reuse_bytes += info["size"]
                    continue
            except OSError:
                pass

        # 目标区已装过：大小对得上就先算就绪，避免把整套客户端重新算成下载量。
        # 内容对不对交给收尾的复核与 repair，这里只按大小判定，和 preflight 口径一致。
        if existing is not None:
            try:
                if (existing / path).stat().st_size == info["size"]:
                    rows.append({"action": "ready", "size": info["size"], "md5": info["md5"],
                                 "path": path, "src_or_url": "", "hash_kind": b_kind})
                    stats.ready_files += 1
                    stats.ready_bytes += info["size"]
                    continue
            except OSError:
                pass

        rel = info.get("url") or ""
        url = rel if rel.startswith("http") else f"{host}/{rel.lstrip('/')}"
        rows.append({"action": "download", "size": info["size"], "md5": info["md5"],
                     "path": path, "src_or_url": url, "hash_kind": b_kind})
        stats.download_files += 1
        stats.download_bytes += info["size"]

    rows.sort(key=lambda r: (r["action"] != "download", -r["size"]))
    return rows, stats


def write_csv(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [
            {"action": r["action"], "size": int(r["size"]), "md5": r["md5"],
             "path": safe_rel(r["path"]), "src_or_url": r["src_or_url"],
             "hash_kind": r.get("hash_kind") or hashes.MD5}
            for r in csv.DictReader(fh)
        ]
    return rows


def kind_of_rows(rows: list[dict]) -> str:
    return rows[0]["hash_kind"] if rows else hashes.MD5
