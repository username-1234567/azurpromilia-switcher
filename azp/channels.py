"""从本机已安装的启动器读取渠道配置。

仓库里不保存渠道密钥：appId / channelCode / secretKey / launcherId 都从本机的
`<版本>/res/app_config.dat` 解出来。这个文件是启动器自带的，格式为 IV(16B) + AES-256-CBC(JSON)。

解密它的 AES 密钥也不写进仓库：运行时到启动器自己的 `resources/` 里找 32 位十六进制串，
拿 app_config.dat 逐个试解，能解出带 appId 的 JSON 就是它。扫描顺序是 `.node` → `.dll/.exe`
→ `app.asar`，正常启动器第一个候选就中。也可以用环境变量 AZP_APPCONFIG_KEY 直接指定。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto, regions

_HEX32 = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_SCAN_MAX = 64 * 1024 * 1024
_MAX_CANDIDATES = 3000


@dataclass(frozen=True)
class Channel:
    app_id: str
    channel_code: str
    launcher_id: str
    game_id: str
    secret: str = field(repr=False)
    lang: str = "en"
    domains: tuple[str, ...] = ()
    label: str = ""
    launcher_version: str = ""

    def public(self) -> dict:
        """可安全打印的部分（不含 secret）。"""
        return {
            "label": self.label,
            "app_id": self.app_id,
            "channel_code": self.channel_code,
            "game_id": self.game_id,
            "launcher_id": self.launcher_id,
            "lang": self.lang,
            "api": self.domains[0] if self.domains else "",
            "launcher_version": self.launcher_version,
        }


def _rank(path: Path) -> tuple[int, str]:
    """原生模块和 DLL 里的密钥更靠前，先试它们，省得扫整个 asar。"""
    name = path.name.lower()
    if name.endswith(".node"):
        return (0, name)
    if name.endswith((".dll", ".exe")):
        return (1, name)
    if name == "app.asar":
        return (2, name)
    return (3, name)


def _candidates(launcher_dir: Path):
    env = os.environ.get("AZP_APPCONFIG_KEY", "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{32}", env):
        yield env.encode()

    root = launcher_dir / "resources"
    if not root.is_dir():
        root = launcher_dir
    files: list[Path] = []
    for path in root.rglob("*"):
        try:
            if path.is_file() and path.stat().st_size <= _SCAN_MAX:
                files.append(path)
        except OSError:
            continue
    files.sort(key=_rank)

    seen: set[bytes] = set()
    for path in files:
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        for match in _HEX32.finditer(blob):
            cand = match.group().lower()
            if cand in seen:
                continue
            seen.add(cand)
            yield cand
            if len(seen) >= _MAX_CANDIDATES:
                return


def decrypt_app_config(blob: bytes, launcher_dir: Path) -> dict:
    if len(blob) < 32:
        raise ValueError("app_config.dat 太短，可能不是有效配置")
    iv, body = blob[:16], blob[16:]
    for key in _candidates(launcher_dir):
        try:
            plain = crypto.decrypt(key, iv, body)
        except crypto.BackendMissing:
            raise
        except Exception:  # noqa: BLE001 - 试错，换下一个候选
            continue
        try:
            cfg = json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(cfg, dict) and ("appId" in cfg or "channelCode" in cfg):
            return cfg
    raise ValueError(
        f"解不开 {launcher_dir / 'res' / 'app_config.dat'}：启动器里没找到匹配的密钥。"
        "可以设 AZP_APPCONFIG_KEY 指定 32 位十六进制密钥，或确认启动器版本"
    )


def read_app_config(launcher_dir: Path) -> dict:
    path = launcher_dir / "res" / "app_config.dat"
    if not path.exists():
        raise FileNotFoundError(f"找不到 {path}")
    return decrypt_app_config(path.read_bytes(), launcher_dir)


def load(launcher_exe: Path) -> Channel:
    """由 launcher.exe 的路径推出渠道信息。"""
    launcher_dir = launcher_exe.parent
    cfg = read_app_config(launcher_dir)
    app_id = str(cfg.get("appId") or cfg.get("gameId") or "")
    channel_code = str(cfg.get("channelCode") or "")
    region = regions.resolve(app_id, channel_code)
    extra = cfg.get("channelExtraParams") or {}
    return Channel(
        app_id=app_id,
        channel_code=channel_code,
        launcher_id=str(cfg.get("launcherId") or ""),
        game_id=str(cfg.get("gameId") or ""),
        secret=str(cfg.get("secretKey") or ""),
        lang=str(extra.get("lang") or (region.lang if region else "en")),
        domains=tuple(region.domains) if region else (),
        label=region.label if region else f"未知区服 {app_id}/{channel_code}",
        launcher_version=launcher_dir.name,
    )
