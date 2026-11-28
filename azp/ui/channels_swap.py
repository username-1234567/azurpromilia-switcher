"""官服 ↔ 哔哩哔哩服热切换。

参考 AzurPromilia-Server-Switcher 的做法：切换的是客户端内的渠道配置与登录组件，
不是换目录、也不是换渠道号：

    <game_root>/mj_sdk/res/app_config.dat
    <game_root>/AzurPromilia_Data/Plugins/mj_sdk

B 服的 mj_sdk 下有 BLPlatform64，官服没有，用它判断当前渠道。
切换前把当前渠道文件备份到 out/backup/channel/，再从 out/cn_sources/<server>/ 覆盖。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from .. import preflight

SERVERS = ("official", "bilibili")
LABELS = {"official": "官服", "bilibili": "哔哩哔哩服"}
SOURCE_DIR = Path("out") / "cn_sources"
BACKUP_DIR = Path("out") / "backup" / "channel"


class ChannelError(RuntimeError):
    pass


def sdk_dir(game_root: Path) -> Path:
    return game_root / "AzurPromilia_Data" / "Plugins" / "mj_sdk"


def config_path(game_root: Path) -> Path:
    return game_root / "mj_sdk" / "res" / "app_config.dat"


def current(game_root: Path) -> str | None:
    """当前渠道：bilibili / official；不像国服客户端时返回 None。"""
    sdk = sdk_dir(game_root)
    if not sdk.is_dir():
        return None
    return "bilibili" if (sdk / "BLPlatform64").is_dir() else "official"


def source_ready(server: str, source_dir: Path = SOURCE_DIR) -> bool:
    base = source_dir / server
    return (base / "app_config.dat").is_file() and (base / "Plugins_mj_sdk").is_dir()


def sources(source_dir: Path = SOURCE_DIR) -> dict[str, bool]:
    return {server: source_ready(server, source_dir) for server in SERVERS}


def extract(game_root: Path, server: str, source_dir: Path = SOURCE_DIR) -> Path:
    """把当前客户端里的渠道配置与登录组件提取成可复用的渠道源。"""
    if server not in SERVERS:
        raise ChannelError("只支持 official / bilibili")
    cfg = config_path(game_root)
    sdk = sdk_dir(game_root)
    if not cfg.is_file() or not sdk.is_dir():
        raise ChannelError("没有找到 mj_sdk 渠道文件，确认这是国服客户端")
    detected = current(game_root)
    if detected is not None and detected != server:
        raise ChannelError(
            f"当前客户端是{LABELS[detected]}，不能提取成{LABELS[server]}的源；"
            "请在对应渠道的客户端里提取"
        )

    dest = source_dir / server
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cfg, dest / "app_config.dat")
    target_sdk = dest / "Plugins_mj_sdk"
    if target_sdk.exists():
        shutil.rmtree(target_sdk)
    shutil.copytree(sdk, target_sdk)
    return dest


def switch(game_root: Path, from_server: str, to_server: str,
           source_dir: Path = SOURCE_DIR, backup_dir: Path = BACKUP_DIR,
           journal=None) -> Path:
    """把客户端从 from_server 热切换到 to_server，返回本次备份目录。"""
    if to_server not in SERVERS or from_server not in SERVERS:
        raise ChannelError("只支持 official / bilibili")
    if from_server == to_server:
        raise ChannelError(f"当前已经是{LABELS[to_server]}")
    if preflight.running_games():
        raise ChannelError("游戏正在运行，先完全退出再切换")
    if not source_ready(to_server, source_dir):
        raise ChannelError(
            f"还没有{LABELS[to_server]}的渠道源。先在同渠道客户端里提取一次，"
            f"或把已有的 {source_dir / to_server} 放进来"
        )

    cfg = config_path(game_root)
    sdk = sdk_dir(game_root)
    if not cfg.is_file() or not sdk.is_dir():
        raise ChannelError("没找到当前客户端的渠道文件，确认目录正确")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / from_server / stamp
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cfg, backup / "app_config.dat")
    shutil.copytree(sdk, backup / "Plugins_mj_sdk", dirs_exist_ok=True)
    if journal is not None:
        journal.backup(cfg)

    src = source_dir / to_server
    cfg.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "app_config.dat", cfg)
    shutil.rmtree(sdk)
    shutil.copytree(src / "Plugins_mj_sdk", sdk)
    return backup


def restore(backup: Path, game_root: Path) -> None:
    """把某次渠道备份还原回客户端。"""
    if not (backup / "app_config.dat").is_file():
        raise ChannelError(f"备份不完整：{backup}")
    sdk = sdk_dir(game_root)
    cfg = config_path(game_root)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup / "app_config.dat", cfg)
    saved_sdk = backup / "Plugins_mj_sdk"
    if saved_sdk.is_dir():
        if sdk.exists():
            shutil.rmtree(sdk)
        shutil.copytree(saved_sdk, sdk)