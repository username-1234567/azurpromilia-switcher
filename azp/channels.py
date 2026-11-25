"""从本机已安装的启动器读取渠道配置。

仓库里不保存渠道密钥：appId / channelCode / secretKey / launcherId 都从本机的
`<版本>/res/app_config.dat` 解出来。这个文件是启动器自带的，格式为 IV(16B) + AES-256-CBC(JSON)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto

APP_CONFIG_KEY = b"5f109ac9d64946e191c354d524bfe590"

# 接口域名与语言按 appId 区分；appId 由 app_config.dat 提供，这里只做路由。
PROFILES: dict[str, dict] = {
    "354": {
        "label": "国服",
        "domains": ("https://api-grp.manjuu.com", "https://api-grp-l2.manjuu.com"),
        "lang": "zh-Hans",
    },
    "434": {
        "label": "国际服",
        "domains": ("https://api-grp-rgn1.jimi-global.com", "https://l2-api-grp-rgn1.jimi-global.com"),
        "lang": "en",
    },
}


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


def decrypt_app_config(blob: bytes) -> dict:
    if len(blob) < 32:
        raise ValueError("app_config.dat 太短，可能不是有效配置")
    plain = crypto.decrypt(APP_CONFIG_KEY, blob[:16], blob[16:])
    return json.loads(plain.decode("utf-8"))


def read_app_config(launcher_dir: Path) -> dict:
    path = launcher_dir / "res" / "app_config.dat"
    if not path.exists():
        raise FileNotFoundError(f"找不到 {path}")
    return decrypt_app_config(path.read_bytes())


def load(launcher_exe: Path) -> Channel:
    """由 launcher.exe 的路径推出渠道信息。"""
    launcher_dir = launcher_exe.parent
    cfg = read_app_config(launcher_dir)
    app_id = str(cfg.get("appId") or cfg.get("gameId") or "")

    profile = PROFILES.get(app_id)
    if profile is None:
        raise RuntimeError(
            f"未知渠道 appId={app_id}，目前支持 {sorted(PROFILES)}；"
            "如确认是新渠道，可在 azp/channels.py 的 PROFILES 里补充域名。"
        )
    extra = cfg.get("channelExtraParams") or {}
    return Channel(
        app_id=app_id,
        channel_code=str(cfg.get("channelCode") or ""),
        launcher_id=str(cfg.get("launcherId") or ""),
        game_id=str(cfg.get("gameId") or ""),
        secret=str(cfg.get("secretKey") or ""),
        lang=str(extra.get("lang") or profile["lang"]),
        domains=tuple(profile["domains"]),
        label=profile["label"],
        launcher_version=launcher_dir.name,
    )
