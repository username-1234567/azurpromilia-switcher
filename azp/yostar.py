"""日服（Yostar 启动器）的清单。

日服不是 Manjuu/Jimi 那套：没有 `app_config.dat`，接口用
`md5(JSON(head) + body + salt)` 签名，只要 game_tag 和时间，不要账号。
清单是 `{source, file:[{path,size,hash}]}`，hash 为 CRC-64/XZ 的十进制字符串，
文件地址为 `<cdn><source><path>`。

签名盐不写进仓库：运行时从日服启动器的 `resources/*.asar` 里读（`salt: "..."`），
或用 AZP_YOSTAR_SALT 指定。所以抓日服清单前要先装过日服启动器。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

GAME_TAG = "AZUPRO_JP"
SALT_ENV = "AZP_YOSTAR_SALT"
API = "https://api-launcher-jp.yo-star.com"
LAUNCHER_VERSION = "1.8.1"
FOLDER = "AZUPRO_JP"

_SALT_RE = re.compile(rb"""salt\s*[:=]\s*["']([0-9A-Fa-f]{32})["']""")
_salt: str | None = None

# 日服与国服的根目录名不同，建链接时按这个映射找源文件
PATH_MAP = (
    ("AzurPromiliaJP_Data/", "AzurPromilia_Data/"),
    ("AzurPromiliaJP.exe", "AzurPromilia.exe"),
    ("AzurPromiliaJPBase.dll", "AzurPromiliaBase.dll"),
)


class YostarError(RuntimeError):
    pass


def _salt_from_launcher() -> str | None:
    """从日服启动器的 asar 里读 `salt: "..."`，仓库里不存这个常量。"""
    from . import detect

    try:
        installs = detect.find_installs()
    except Exception:  # noqa: BLE001
        return None
    for inst in installs:
        if inst.kind != "yostar":
            continue
        res = inst.launcher_exe.parent / "resources"
        if not res.is_dir():
            continue
        for path in sorted(res.glob("*.asar")):
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            match = _SALT_RE.search(blob)
            if match:
                return match.group(1).decode("ascii")
    return None


def salt() -> str:
    global _salt
    if _salt:
        return _salt
    env = os.environ.get(SALT_ENV, "").strip()
    if re.fullmatch(r"[0-9A-Fa-f]{32}", env):
        _salt = env
        return env
    found = _salt_from_launcher()
    if not found:
        raise YostarError(
            "找不到日服签名盐：抓日服清单要先装过日服启动器，"
            f"或把 {SALT_ENV} 设成 32 位十六进制"
        )
    _salt = found
    return found


def auth_header(data: str = "", version: str = LAUNCHER_VERSION) -> str:
    head = {"game_tag": GAME_TAG, "time": int(time.time()), "version": version}
    sign = hashlib.md5(
        (json.dumps(head, separators=(",", ":")) + (data or "") + salt()).encode()
    ).hexdigest()
    return json.dumps({"head": head, "sign": sign}, separators=(",", ":"))


def _get(path: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        API + path,
        headers={
            "Authorization": auth_header(),
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise YostarError(f"接口请求失败：{exc}") from None
    if data.get("code") != 200:
        raise YostarError(f"接口返回 code={data.get('code')} msg={data.get('msg')}")
    return data.get("data") or {}


def game_config() -> dict:
    """版本号、包路径、入口 exe、CDN。"""
    info = _get("/api/launcher/game/config")
    cdn = _get("/api/launcher/advanced/game/download/cdn")
    return {
        "version": str(info.get("game_latest_version") or ""),
        "basis": str(info.get("game_latest_file_path") or ""),
        "entry": str(info.get("game_start_exe_name") or ""),
        "params": info.get("game_start_params") or [],
        "uninstall": str(info.get("game_uninstall_script") or ""),
        "size_text": str(info.get("decompression_size") or ""),
        "cdn": str(cdn.get("primary_cdn") or "").rstrip("/"),
        "cdn_backup": str(cdn.get("back_up_cdn") or "").rstrip("/"),
    }


def manifest_url(version: str, basis: str) -> str:
    q = urllib.parse.urlencode({"version": version, "file_path": basis})
    data = _get(f"/api/launcher/game/config/json?{q}")
    url = str(data.get("url") or "")
    if not url.startswith("http"):
        raise YostarError("接口没有返回清单地址")
    return url


def _b64_md5(text: str) -> str:
    """启动器的 vc：md5(值用分号连起来) 再 base64。"""
    return base64.b64encode(hashlib.md5(text.encode()).digest()).decode()


def _js_str(value) -> str:
    """按 JS 的 String() 规则转：数组是逗号连接，不是 Python 那套方括号。"""
    if isinstance(value, (list, tuple)):
        return ",".join(_js_str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _vc(obj: dict) -> str:
    return _b64_md5(";".join(_js_str(v) for v in obj.values()))


def local_files(manifest: dict) -> list[dict]:
    items = []
    for path, info in manifest["files"].items():
        item = {"path": "/" + path.lstrip("/"), "hash": str(info["md5"]),
                "size": str(info["size"])}
        item["vc"] = _vc(item)
        items.append(item)
    return items


def finalize(target: Path, manifest: dict) -> list[Path]:
    """写启动器认的两个文件：本地清单与游戏配置。

    目录名必须是 AZUPRO_JP，`handle-check-game` 会先比 basename 再读配置；
    vc 对不上时启动器会删掉配置，表现成「没装过」。
    """
    if target.name != FOLDER:
        raise YostarError(f"日服目录名必须是 {FOLDER}，当前是 {target.name}")
    target.mkdir(parents=True, exist_ok=True)
    version = str(manifest.get("version_code") or "")
    basis = str(manifest.get("basis") or manifest.get("source") or "")
    entry = str(manifest.get("launch_entry") or "").removesuffix(".exe")

    head = {"name": GAME_TAG, "version": version, "basis": basis}
    head["vc"] = _vc(head)
    doc = {**head, "files": local_files(manifest)}
    manifest_path = target / "manifest.json"
    manifest_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), "utf-8")

    config = {
        "tag": GAME_TAG,
        "name": entry,
        "params": manifest.get("launch_params") or [],
        "version": version,
        "gameUninstallScript": str(manifest.get("uninstall_script") or ""),
    }
    config["vc"] = _vc(config)
    config_path = target / "game-launcher-config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")
    return [manifest_path, config_path]


def fetch(out_dir: Path, force: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "manifest.json"
    if target.exists() and not force:
        print(f"  已有清单，跳过（--force 可重抓）：{target}")
        return json.loads(target.read_text("utf-8"))

    print("[日服] 查询版本")
    cfg = game_config()
    if not cfg["version"] or not cfg["basis"]:
        raise YostarError("game config 缺少版本或包路径")
    print(f"  版本 {cfg['version']}  入口 {cfg['entry']}.exe  体积 {cfg['size_text']}")

    url = manifest_url(cfg["version"], cfg["basis"])
    print(f"  清单 {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise YostarError(f"清单下载失败：{exc}") from None

    source = str(raw.get("source") or "")
    items = raw.get("file") or []
    if not source or not items:
        raise YostarError("清单结构不认识（缺 source 或 file）")

    cdn = cfg["cdn"]
    files: dict[str, dict] = {}
    from .plan import safe_rel

    for item in items:
        # 只去前导斜杠不够：`/../x`、盘符、反斜杠都会变成越界写入，统一按相对路径校验
        rel = safe_rel(str(item.get("path") or "").lstrip("/")) if item.get("path") else ""
        if not rel:
            continue
        files[rel] = {
            "size": int(item.get("size") or 0),
            "md5": str(item.get("hash") or ""),  # 实际是 CRC-64/XZ 十进制的字符串
            # source 自带结尾斜杠（"/pkg/"），不削掉会拼出 pkg//file 这种双斜杠地址
            "url": f"{cdn}{source.rstrip('/')}/{rel}",
        }
    total = sum(f["size"] for f in files.values())
    manifest = {
        "region": "jp",
        "label": "日服",
        "version_code": cfg["version"],
        "version_name": cfg["version"],
        "package_type": 2,
        "hash_kind": "crc64",
        "total_size": total,
        "file_count": len(files),
        "dir_count": 0,
        "cdn_host": cdn,
        "source": source,
        "game_folder_name": FOLDER,
        "basis": source,
        "launch_entry": f"{cfg['entry']}.exe" if cfg["entry"] else "",
        "launch_params": cfg["params"],
        "uninstall_script": cfg["uninstall"],
        "path_map": [list(p) for p in PATH_MAP],
        "files": files,
    }
    target.write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
    (out_dir / "manifest.raw.json").write_text(json.dumps(raw, ensure_ascii=False), "utf-8")
    print(f"  清单 {len(files):,} 个文件 / {total / 2**30:.2f} GiB（CRC64）")
    return manifest
