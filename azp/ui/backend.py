"""GUI 后端：把检测、清单、计划、环境检查包成可 JSON 序列化的结果。

只做只读计算，不建链接、不下载、不写客户端目录；真正执行仍由 CLI 的流程负责，
后续接入时复用同一批底层模块。
"""

from __future__ import annotations

import json
import os
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from .. import channels, detect, journal, manifest, nofade, plan, preflight, regions, yostar
from ..api import Api
from . import channels_swap


def _install_rows() -> list[dict]:
    rows: list[dict] = []
    for inst in detect.find_installs():
        if inst.kind == "yostar":
            region = regions.find("jp")
            rows.append({
                "key": "jp",
                "label": region.label if region else "日服",
                "root": str(inst.root),
                "game": str(inst.game_dirs[0]) if inst.game_dirs else "",
                "installed": bool(inst.game_dirs),
                "kind": "yostar",
                "homepage": region.homepage if region else "",
            })
            continue
        try:
            ch = channels.load(inst.launcher_exe)
            key = regions.key_for(ch.app_id, ch.channel_code)
            region = regions.find(key)
            rows.append({
                "key": key,
                "label": ch.label or (region.label if region else key),
                "root": str(inst.root),
                "game": str(inst.game_dirs[0]) if inst.game_dirs else "",
                "installed": bool(inst.game_dirs),
                "kind": "manjuu",
                "homepage": region.homepage if region else "",
            })
        except Exception as exc:  # noqa: BLE001
            rows.append({
                "key": "",
                "label": "无法识别",
                "root": str(inst.root),
                "game": "",
                "installed": False,
                "kind": "unknown",
                "error": str(exc),
            })
    return rows


GROUP = {"bili": "cn"}


def _group(key: str) -> str:
    """官服与哔哩哔哩服共用同一套安装与目录，按组处理。"""
    return GROUP.get(key, key)


def _game_dir(key: str) -> Path | None:
    group = _group(key)
    for row in _install_rows():
        if row["key"] == group and row["installed"]:
            return Path(row["game"])
    return None


def _api_for(key: str) -> Api | None:
    group = _group(key)
    for inst in detect.find_installs():
        if inst.kind == "yostar":
            continue
        try:
            ch = channels.load(inst.launcher_exe)
        except Exception:  # noqa: BLE001
            continue
        if regions.key_for(ch.app_id, ch.channel_code) == group:
            return Api(ch)
    return None


def _manifest_for(key: str, out: Path, force: bool) -> tuple[dict, list[str]]:
    path = out / key / "manifest.json"
    if path.exists() and not force:
        return json.loads(path.read_text("utf-8")), [f"复用已有清单：{path}"]
    if key == "jp":
        has_launcher = any(inst.kind == "yostar" for inst in detect.find_installs())
        if not has_launcher and not os.environ.get("AZP_YOSTAR_SALT"):
            raise ValueError("日服需要先安装日服启动器，或设置 AZP_YOSTAR_SALT")
        data = yostar.fetch(out / key, force=force)
    else:
        api = _api_for(key)
        if api is None:
            raise ValueError(f"没找到 {key} 的启动器，无法抓清单")
        data = manifest.fetch(api, out / key, force=force)
    return data, [f"已抓取清单：{path}"]


INSTALLER_PATTERNS = (
    "*Promilia*installer*.exe",
    "*Promilia*setup*.exe",
    "*Azur*installer*.exe",
    "*Azur*setup*.exe",
)

INSTALLER_MIN_BYTES = 50 * 2**20


def _installer_region(name: str) -> str:
    low = name.lower()
    if "jimi" in low or "_tc" in low:
        return "global"
    if "yostar" in low or "_jp" in low:
        return "jp"
    if "bili" in low:
        return "bili"
    return "cn"


def _scan_installers(dirs: list[Path],
                     min_bytes: int = INSTALLER_MIN_BYTES) -> dict[str, str]:
    """在常见位置找启动器安装包，按文件名判断属于哪个区服。"""
    found: dict[str, str] = {}
    for folder in dirs:
        if not folder.is_dir():
            continue
        for pattern in INSTALLER_PATTERNS:
            for path in folder.glob(pattern):
                try:
                    if not path.is_file() or path.stat().st_size < min_bytes:
                        continue
                except OSError:
                    continue
                found.setdefault(_installer_region(path.name), str(path))
    return found


def find_installers() -> dict[str, str]:
    home = Path.home()
    return _scan_installers(
        [home / "Downloads", home / "Desktop", home / "Documents", Path.cwd()]
    )


def _variants_for(key: str) -> list[dict]:
    if _group(key) != "cn":
        return []
    return [
        {"key": "cn", "label": "官服"},
        {"key": "bili", "label": "哔哩哔哩服"},
    ]


def detect_all(payload: dict | None = None) -> dict:
    rows = _install_rows()
    installers = find_installers()
    defaults: dict[str, str] = {}
    base = next((row for row in rows if row["installed"]), None)
    if base is not None:
        # 未安装的目标区默认放在源区所在盘的根级目录，和 CLI 的推导保持一致
        root = Path(base["game"]).parent.parent
        for region in regions.load():
            # 国服与哔哩哔哩服用同一个目录名，无法按「源区同级」推导，必须手填
            if region.key in ("cn", "bili") or not region.folder:
                continue
            if region.key != base["key"]:
                defaults[region.key] = str(root / region.folder)

    def decorate(row: dict) -> dict:
        group = _group(row["key"])
        row["installer"] = installers.get(row["key"]) or installers.get(group) or ""
        row["variants"] = _variants_for(row["key"])
        return row

    for row in rows:
        decorate(row)
    for key in ("cn", "global", "jp"):
        if any(_group(row["key"]) == key for row in rows):
            continue
        region = regions.find(key)
        if region is not None:
            rows.append(decorate({
                "key": region.key, "label": region.label, "root": "",
                "game": "", "installed": False, "kind": region.kind,
                "homepage": region.homepage,
            }))
    return {"installs": rows, "defaults": defaults, "installers": installers}


def prepare(payload: dict) -> dict:
    """校验参数、准备清单并生成计划，返回带 Path 与 rows 的内部结构。"""
    base_key = str(payload.get("base") or "")
    target_key = str(payload.get("target") or "")
    if not base_key or not target_key:
        raise ValueError("先选择源区和目标区")
    if base_key == target_key:
        raise ValueError("源区和目标区不能相同")

    base_region = regions.find(base_key)
    target_region = regions.find(target_key)
    if target_region is None:
        raise ValueError(f"区服表里没有 {target_key}")


    base_dir = _game_dir(base_key)
    if base_dir is None:
        raise ValueError("源区还没有装好的游戏目录")

    out = Path(payload.get("out") or "out")
    force = bool(payload.get("force"))
    target_dir = Path(payload["target_dir"]) if payload.get("target_dir") else None
    if target_dir is None:
        installed_dir = _game_dir(target_key)
        if installed_dir is not None:
            # 目标区已装好时用它的实际目录，不能按默认路径重新推
            target_dir = installed_dir
        elif target_region.key in ("cn", "bili"):
            # 两者共用 AzurPromilia_Game 目录名，各有独立启动器，不能自动推路径
            raise ValueError(
                f"{target_region.label}需要独立安装它自己的启动器，"
                "并手动填写启动器生成的游戏目录"
            )
        elif target_region.folder:
            target_dir = base_dir.parent.parent / target_region.folder
        else:
            raise ValueError("目标区没有配置目录名，请手动填写目标目录")

    notes: list[str] = []
    base_man, lines = _manifest_for(base_key, out, force)
    notes += lines
    target_man, lines = _manifest_for(target_key, out, force)
    notes += lines
    if target_region.kind == "yostar":
        notes.append("日服需要本机装过日服启动器（读取签名盐），CRC64 校验需要 7-Zip")

    reinstall = bool(payload.get("reinstall"))
    rows, stats = plan.build(
        base_man, target_man, base_dir, base_dir,
        cache=out / "cache" / "crc64.json",
        existing=None if reinstall else (target_dir if target_dir.exists() else None),
    )
    return {
        "base_key": base_key,
        "base_label": base_region.label if base_region else base_key,
        "base_dir": base_dir,
        "target_key": target_key,
        "target_label": target_region.label,
        "target_dir": target_dir,
        "base_man": base_man,
        "target_man": target_man,
        "rows": rows,
        "stats": stats,
        "notes": notes,
        "reinstall": reinstall,
    }


def plan_preview(payload: dict) -> dict:
    p = prepare(payload)
    base_dir = p["base_dir"]
    target_dir = p["target_dir"]
    rows = p["rows"]
    target_man = p["target_man"]
    stats = p["stats"]
    notes = list(p["notes"])

    need = preflight.pending_bytes(rows, target_dir)
    env_ok, items = preflight.run(
        base=base_dir, target=target_dir, need_bytes=need,
        manifest=target_man, probe_links=False,
    )

    source_state = nofade.state(base_dir)
    target_state = nofade.state(target_dir) if target_dir.exists() else "missing"
    if source_state == "patched":
        notes.append("源区已装反虚化补丁，硬链接会把这个文件带到目标区")
    if target_state == "patched":
        notes.append("目标区已装反虚化补丁，正式执行时默认保留，不会被官方文件覆盖")

    return {
        "base": {"key": p["base_key"], "label": p["base_label"], "game": str(base_dir)},
        "target": {"key": p["target_key"], "label": p["target_label"], "dir": str(target_dir)},
        "target_exists": target_dir.exists(),
        "stats": {
            "link_files": stats.link_files,
            "link_bytes": stats.link_bytes,
            "reuse_files": stats.reuse_files,
            "reuse_bytes": stats.reuse_bytes,
            "ready_files": stats.ready_files,
            "ready_bytes": stats.ready_bytes,
            "download_files": stats.download_files,
            "download_bytes": stats.download_bytes,
            "total_bytes": int(target_man.get("total_size") or 0),
        },
        "need_bytes": need,
        "env_ok": env_ok,
        "env": [{"name": i.name, "status": i.status, "detail": i.detail} for i in items],
        "nofade": {"source": source_state, "target": target_state},
        "notes": notes,
    }


def nofade_info(payload: dict) -> dict:
    target = payload.get("target") or ""
    if not target:
        raise ValueError("没有指定目录")
    return {"target": target, "state": nofade.state(Path(target))}


def open_path(payload: dict) -> dict:
    path = payload.get("path") or ""
    if not path:
        raise ValueError("路径为空")
    target = Path(path)
    if not target.exists():
        raise ValueError(f"目录不存在：{target}")
    os.startfile(str(target))  # type: ignore[attr-defined]
    return {"path": str(target)}

OFFICIAL_HOSTS = (
    "azurpromilia.manjuu.com",
    "azurpromilia.jimi-global.com",
    "azurpromilia.jp",
)


def open_url(payload: dict) -> dict:
    url = str(payload.get("url") or "")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in OFFICIAL_HOSTS:
        raise ValueError("只允许打开官方下载页")
    webbrowser.open(url)
    return {"url": url}


def open_installer(payload: dict) -> dict:
    raw = str(payload.get("path") or "")
    allowed = set(find_installers().values())
    if raw not in allowed:
        raise ValueError("只允许打开检测到的启动器安装包")
    os.startfile(raw)  # type: ignore[attr-defined]
    return {"path": raw}


def channel_info(payload: dict) -> dict:
    """当前客户端的渠道（官服 / B 服）与已有渠道源。"""
    raw = str(payload.get("target") or "").strip()
    if not raw:
        raise ValueError("没有指定目录")
    root = Path(raw)
    current = channels_swap.current(root)
    return {
        "target": raw,
        "current": current,
        "current_label": channels_swap.LABELS.get(current or "", ""),
        "sources": channels_swap.sources(),
        "labels": channels_swap.LABELS,
    }

def regions_info(payload: dict | None = None) -> dict:
    return {"regions": [
        {"key": r.key, "label": r.label, "app_id": r.app_id, "folder": r.folder,
         "homepage": r.homepage, "ready": r.ready, "source": r.source}
        for r in regions.load()
    ]}


def journal_list(payload: dict) -> dict:
    raw = str(payload.get("target") or "").strip()
    if not raw:
        raise ValueError("没有指定目录")
    target = Path(raw)
    if not target.exists():
        return {"target": raw, "entries": []}
    entries = []
    for path in reversed(journal.available(target)):
        try:
            data = journal.load(path)
        except (OSError, ValueError):
            continue
        created = data.get("created") or []
        backups = data.get("backups") or []
        entries.append({
            "id": path.name,
            "cmd": str(data.get("cmd") or ""),
            "time": float(data.get("time") or 0),
            "created_files": len(created),
            "created_bytes": sum(int(i.get("size") or 0) for i in created),
            "backup_files": len(backups),
        })
        if len(entries) >= 50:
            break
    return {"target": raw, "entries": entries}