"""定位本机已安装的启动器与游戏目录。

优先读注册表卸载项（InstallLocation 指向游戏目录），再回退到常见盘符扫描。
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from pathlib import Path

try:
    import winreg  # type: ignore
except ImportError:  # 非 Windows 平台仅支持手动 --launcher 指定
    winreg = None

UNINSTALL_ROOTS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)


@dataclass
class Install:
    root: Path
    launcher_exe: Path
    game_dirs: list[Path] = field(default_factory=list)

    @property
    def version(self) -> str:
        return self.launcher_exe.parent.name

    @property
    def game_dir(self) -> Path | None:
        return self.game_dirs[0] if self.game_dirs else None


def _registry_game_dirs() -> list[Path]:
    found: list[Path] = []
    if winreg is None:
        return found
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in UNINSTALL_ROOTS:
            try:
                key = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            with key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        name = winreg.EnumKey(key, i)
                        if "azurpromilia" not in name.lower():
                            continue
                        with winreg.OpenKey(key, name) as item:
                            loc, _ = winreg.QueryValueEx(item, "InstallLocation")
                    except OSError:
                        continue
                    if loc:
                        found.append(Path(str(loc)))
    return found


def _game_dirs_in(root: Path) -> list[Path]:
    out = []
    if not root.is_dir():
        return out
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name.lower().endswith("_game") and any(
            (child / n).exists() for n in ("AzurPromilia.exe", "AzurPromilia_Data")
        ):
            out.append(child)
    return sorted(out)


def _installs_in(root: Path) -> list[Install]:
    """root 下形如 <版本号>/launcher.exe 的启动器目录。"""
    out = []
    if not root.is_dir():
        return out
    for child in root.iterdir():
        if not child.is_dir():
            continue
        exe = child / "launcher.exe"
        if not exe.exists():
            continue
        try:
            exe.relative_to(root)
        except ValueError:
            continue
        out.append(Install(root=root, launcher_exe=exe, game_dirs=_game_dirs_in(root)))
    return out


def find_installs(extra_roots: list[str] | None = None) -> list[Install]:
    roots: list[Path] = []
    for game_dir in _registry_game_dirs():
        roots.append(game_dir.parent)
    for drive in string.ascii_uppercase:
        base = Path(f"{drive}:/")
        if not base.exists():
            continue
        try:
            for child in base.iterdir():
                if child.is_dir() and child.name.lower().startswith("azurpromilia"):
                    roots.append(child)
        except OSError:
            continue
    for extra in extra_roots or []:
        roots.append(Path(extra))

    seen: set[Path] = set()
    installs: list[Install] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        installs.extend(_installs_in(root))

    # 去重：同 root 只保留版本号最大的启动器
    best: dict[str, Install] = {}
    for inst in installs:
        key = str(inst.root.resolve()).lower()
        cur = best.get(key)
        if cur is None or _version_key(inst.version) > _version_key(cur.version):
            best[key] = inst
    return sorted(best.values(), key=lambda i: str(i.root).lower())


def _version_key(text: str) -> tuple:
    parts = []
    for chunk in text.replace("-", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)
