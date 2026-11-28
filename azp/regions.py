"""区服表：把启动器里的 (appId, channelCode) 映射成短代号。

内置国服、哔哩哔哩服、台服；其他渠道（同一套 Manjuu/Jimi 启动器的）可以在仓库根的
`regions.json` 里补 appId、渠道号和接口域名，不用改代码：

    {"regions": {"xx": {"label": "…", "app_id": "…", "channel_codes": ["…"],
                        "domains": ["https://…"], "lang": "…", "folder": "…"}}}

日服单独一套：Yostar 启动器，接口用时间戳 + salt 签名，不需要账号，清单从
`api-launcher-jp.yo-star.com` 现拉，所以内置表里直接给了它的域名。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

CONFIG_ENV = "AZP_REGIONS"
CONFIG_NAME = "regions.json"

MANJUU = ("https://api-grp.manjuu.com", "https://api-grp-l2.manjuu.com")
JIMI = ("https://api-grp-rgn1.jimi-global.com", "https://l2-api-grp-rgn1.jimi-global.com")


@dataclass(frozen=True)
class Region:
    key: str
    label: str
    app_id: str = ""
    channel_codes: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    lang: str = "en"
    folder: str = ""
    source: str = "内置"
    note: str = ""
    kind: str = "manjuu"
    homepage: str = ""

    @property
    def ready(self) -> bool:
        """能抓清单：Yostar 只要有接口域名，其余要 appId、渠道号、域名。"""
        if self.kind == "yostar":
            return bool(self.domains)
        return bool(self.app_id and self.channel_codes and self.domains)


BUILTIN: tuple[Region, ...] = (
    Region("cn", "国服", "354", ("C000",), MANJUU, "zh-Hans", "AzurPromilia_Game",
           homepage="https://azurpromilia.manjuu.com/"),
    Region("bili", "哔哩哔哩服", "354", ("C001",), MANJUU, "zh-Hans", "AzurPromilia_Game"),
    Region("global", "台服", "434", ("C500",), JIMI, "en", "AzurPromilia_TC_game",
           homepage="https://azurpromilia.jimi-global.com/home/"),
    Region(
        "jp",
        "日服",
        "AZUPRO_JP",
        (),
        ("https://api-launcher-jp.yo-star.com",),
        "ja",
        "AZUPRO_JP",
        kind="yostar",
        homepage="https://azurpromilia.jp/",
    ),
)


def config_path() -> Path:
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env)
    candidates = []
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后 __file__ 在临时解压目录，优先读 exe 旁边的配置
        candidates.append(Path(sys.executable).resolve().parent / CONFIG_NAME)
    candidates.append(Path(__file__).resolve().parent.parent / CONFIG_NAME)
    candidates.append(Path.cwd() / CONFIG_NAME)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


def load(path: Path | str | None = None) -> list[Region]:
    """内置表 + 本地配置。同 key 的配置项覆盖内置项。"""
    p = Path(path) if path else config_path()
    table: dict[str, Region] = {r.key: r for r in BUILTIN}
    if not p.exists():
        return list(table.values())
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{p} 读不出来：{exc}") from None
    items = data.get("regions") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        raise ValueError(f"{p} 缺少 regions 对象")
    for key, raw in items.items():
        if not isinstance(raw, dict):
            raise ValueError(f"{p} 里 {key} 不是一个对象")
        base = table.get(key) or Region(key=key, label=str(raw.get("label") or key))
        entry = replace(
            base,
            label=str(raw.get("label") or base.label),
            app_id=str(raw.get("app_id") or base.app_id),
            channel_codes=tuple(str(c) for c in raw.get("channel_codes") or base.channel_codes),
            domains=tuple(str(d) for d in raw.get("domains") or base.domains),
            lang=str(raw.get("lang") or base.lang),
            folder=str(raw.get("folder") or base.folder),
            source=str(p),
            kind=str(raw.get("kind") or base.kind),
            homepage=str(raw.get("homepage") or base.homepage),
        )
        table[key] = entry
    return list(table.values())


def resolve(app_id: str, channel_code: str, path: Path | str | None = None) -> Region | None:
    for region in load(path):
        if region.app_id == str(app_id) and str(channel_code) in region.channel_codes:
            return region
    return None


def key_for(app_id: str, channel_code: str, path: Path | str | None = None) -> str:
    region = resolve(app_id, channel_code, path)
    return region.key if region else f"app{app_id}"


def find(key: str, path: Path | str | None = None) -> Region | None:
    for region in load(path):
        if region.key == key:
            return region
    return None
