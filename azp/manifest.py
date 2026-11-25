"""抓取并解析启动器的逐文件清单（路径 / 大小 / MD5 / CDN 地址）。"""

from __future__ import annotations

import base64
import json
import urllib.parse
from pathlib import Path

from . import crypto
from .api import Api, ApiError, http_get_bytes, user_agent

INDEX_KEY = b"mJ9dH2bR7fT1zW4eQ6cY8a=="
INDEX_IV = b"aP3sD8fG0hJ6kL9z"


def decrypt_index(blob: bytes) -> str:
    binary = base64.b64decode("".join(blob.decode("utf-8", "ignore").split()))
    return crypto.decrypt(INDEX_KEY, INDEX_IV, binary).decode("utf-8", "replace")


def encrypt_index(text: str) -> bytes:
    return base64.b64encode(crypto.encrypt(INDEX_KEY, INDEX_IV, text.encode("utf-8")))


def _rows(text: str):
    for line in text.splitlines():
        line = line.strip("\r\n")
        if not line:
            continue
        if line.endswith(","):
            line = line[:-1]
        yield [f.strip() for f in line.split(",")]


def parse_index(text: str, package_type: int) -> dict:
    """type=2 直连清单：首行 [总数,文件数,目录数]；4 列=目录；5 列=[类型,大小,md5,路径,url]。"""
    files: dict[str, dict] = {}
    dirs: list[str] = []
    first = True

    for fields in _rows(text):
        n = len(fields)
        if package_type == 2:
            if first and n == 3:
                first = False
                continue
            first = False
            if n == 4:
                dirs.append(fields[3])
                continue
            if n != 5:
                continue
            _, size, md5, path, url = fields
            try:
                size_i = int(size)
            except ValueError:
                continue
            files[path.replace("\\", "/")] = {
                "size": size_i,
                "md5": md5.lower(),
                "url": url,
            }
        else:
            first = False
            if n != 5:
                continue
            _, _sz, size, md5, _url = fields
            try:
                size_i = int(size)
            except ValueError:
                continue
            files[f"slice-{len(files) + 1:06d}"] = {"size": size_i, "md5": md5.lower()}

    return {"files": files, "dirs": dirs}


def fetch(api: Api, out_dir: Path, force: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "manifest.json"
    if target.exists() and not force:
        print(f"  已有清单，跳过（--force 可重抓）：{target}")
        return json.loads(target.read_text("utf-8"))

    tag = api.ch.label or api.ch.app_id
    print(f"[{tag}] 查询版本")
    config = api.get(
        "/pc-launcher/client/gameversion/packageconfig", {"launcherId": api.ch.launcher_id}
    )
    (out_dir / "packageconfig.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), "utf-8"
    )

    released = (config.get("data") or {}).get("released") or {}
    version_code = str(released.get("versionCode") or "")
    version_name = str(released.get("versionName") or "")

    # 目录名与入口文件在另一个接口里，拿不到就退回默认值
    folder = str(released.get("gameFolderName") or "")
    entry = str(released.get("launchEntry") or "")
    try:
        extra = api.get(
            "/pc-launcher/client/gameversion/config", {"launcherId": api.ch.launcher_id}
        )
        rel2 = (extra.get("data") or {}).get("released") or {}
        folder = str(rel2.get("gameFolderName") or folder)
        entry = str(rel2.get("launchEntry") or entry)
    except ApiError:
        pass

    print(f"  版本 {version_code or version_name}")
    if folder:
        print(f"  目录名 {folder}  入口 {entry}")

    packages = released.get("gamePackageList") or []
    pkg = next((p for p in packages if int(p.get("packageType", 0)) == 2), None) or next(
        (p for p in packages if int(p.get("packageType", 0)) == 1), None
    )
    if pkg is None:
        raise ApiError("接口没有返回可用的 packageType（1/2）")

    ptype = int(pkg.get("packageType", 2))
    raw_url = (pkg.get("downloadUrl") or "").replace("\\", "/")
    bases = pkg.get("cdnBaseUrls") or released.get("domainList") or []
    urls = [raw_url] if raw_url.startswith("http") else [
        f"{b.rstrip('/')}/{raw_url.lstrip('/')}" for b in bases
    ]
    if not urls:
        raise ApiError("下不到清单地址：downloadUrl 为空")

    blob = None
    used = ""
    for url in urls[:3]:
        try:
            blob = http_get_bytes(url, ua=user_agent(api.version))
            used = url
            break
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 清单下载失败 {url[:90]}: {exc}")
    if blob is None:
        raise ApiError("清单下载全部失败")

    (out_dir / "index.raw").write_bytes(blob)
    text = blob.decode("utf-8", "ignore")
    if "\x00" in text[:64] or "," not in text[:2000]:
        text = decrypt_index(blob)
    (out_dir / "index.csv").write_text(text, "utf-8")

    parsed = parse_index(text, ptype)
    total = sum(f["size"] for f in parsed["files"].values())
    parsed_url = urllib.parse.urlsplit(used)
    manifest = {
        "region": api.ch.app_id,
        "label": api.ch.label,
        "version_code": version_code,
        "version_name": version_name,
        "package_type": ptype,
        "total_size": total,
        "file_count": len(parsed["files"]),
        "dir_count": len(parsed["dirs"]),
        "cdn_host": f"{parsed_url.scheme}://{parsed_url.netloc}",
        "game_folder_name": folder,
        "launch_entry": entry,
        "files": parsed["files"],
        "dirs": parsed["dirs"],
    }
    target.write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
    print(f"  清单 {len(parsed['files'])} 个文件 / {len(parsed['dirs'])} 个目录 / {total / 2**30:.2f} GiB")
    return manifest
