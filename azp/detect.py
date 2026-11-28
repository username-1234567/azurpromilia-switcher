"""定位本机已安装的启动器与游戏目录。

优先读注册表卸载项（InstallLocation 指向游戏目录），再回退到常见盘符扫描。
"""

from __future__ import annotations

import json
import re
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
MANUAL_FILE = Path("out") / "launchers.json"


@dataclass
class Install:
    root: Path
    launcher_exe: Path
    game_dirs: list[Path] = field(default_factory=list)
    kind: str = "manjuu"  # manjuu / yostar

    @property
    def version(self) -> str:
        return self.launcher_exe.parent.name

    @property
    def game_dir(self) -> Path | None:
        return self.game_dirs[0] if self.game_dirs else None


def looks_like_yostar(launcher_exe: Path) -> bool:
    """日服启动器是 Electron 套壳，没有 app_config.dat，靠特征文件认。"""
    if "azupro" in launcher_exe.name.lower():
        return True
    upd = launcher_exe.parent / "resources" / "app-update.yml"
    try:
        text = upd.read_text("utf-8", errors="ignore").lower()
    except OSError:
        return False
    return "azupro" in text or "yo-star" in text


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


def _game_dirs_in(root: Path, kind: str = "manjuu") -> list[Path]:
    out = []
    if not root.is_dir():
        return out
    if kind == "yostar" and root.name.upper() == "AZUPRO_JP" and (
        root / "AzurPromiliaJP_Data"
    ).is_dir():
        return [root]
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if kind == "yostar":
            if child.name.upper() == "AZUPRO_JP" and (child / "AzurPromiliaJP_Data").is_dir():
                out.append(child)
            continue
        if child.name.lower().endswith("_game") and any(
            (child / n).exists() for n in ("AzurPromilia.exe", "AzurPromilia_Data")
        ):
            out.append(child)
    return sorted(out)


def _installs_in(root: Path) -> list[Install]:
    """root 下形如 <版本号>/launcher.exe（或 *Gamelauncher.exe）的启动器目录。"""
    out = []
    if not root.is_dir():
        return out
    direct = [root / "launcher.exe"] if (root / "launcher.exe").is_file() else []
    direct += sorted(p for p in root.glob("*amelauncher.exe") if p.is_file())
    for exe in direct:
        kind = "yostar" if looks_like_yostar(exe) else "manjuu"
        out.append(Install(root=root, launcher_exe=exe,
                           game_dirs=_game_dirs_in(root, kind), kind=kind))
    for child in root.iterdir():
        if not child.is_dir():
            continue
        exe = child / "launcher.exe"
        kind = "manjuu"
        if not exe.exists():
            candidates = sorted(p for p in child.glob("*amelauncher.exe") if p.is_file())
            if not candidates:
                continue
            exe = candidates[0]
            kind = "yostar"
        try:
            exe.relative_to(root)
        except ValueError:
            continue
        out.append(Install(root=root, launcher_exe=exe,
                           game_dirs=_game_dirs_in(root, kind), kind=kind))
    return out


def from_path(raw: str | Path) -> Install:
    """从 launcher.exe、版本目录或安装根手动构造一个 Install。

    装在非标准位置、扫描扫不到时用。认不出启动器就抛 FileNotFoundError。
    """
    p = Path(raw).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"路径不存在：{p}")
    if p.is_dir() and p.name.lower().endswith("_game"):
        p = p.parent
    if p.is_dir() and p.parent == p:
        raise ValueError(f"不要指向盘符根：{p}，给安装根或 launcher.exe")
    if p.is_file():
        exe = p
    elif (p / "launcher.exe").exists():
        exe = p / "launcher.exe"
    else:
        cands = [e for e in p.glob("*amelauncher.exe") if e.is_file()]
        cands += [
            c / "launcher.exe"
            for c in p.iterdir()
            if c.is_dir() and (c / "launcher.exe").exists()
        ]
        cands += [
            e
            for c in p.iterdir()
            if c.is_dir()
            for e in c.glob("*amelauncher.exe")
            if e.is_file()
        ]
        if not cands:
            raise FileNotFoundError(f"{p} 下没有 launcher.exe 或 *Gamelauncher.exe")
        exe = max(cands, key=lambda e: _version_key(e.parent.name))

    kind = "yostar" if looks_like_yostar(exe) else "manjuu"
    root = exe.parent.parent if _is_version_dir(exe.parent.name) else exe.parent
    if p.is_dir() and exe.parent != p and not _is_version_dir(exe.parent.name):
        root = p
    return Install(root=root, launcher_exe=exe, game_dirs=_game_dirs_in(root, kind), kind=kind)


def _install_root_of(path: Path) -> Path:
    """注册表里可能是游戏目录，也可能直接是安装根；统一成安装根再扫。"""
    p = path
    if p.name.lower().endswith("_game") or any(
        (p / n).exists() for n in ("AzurPromilia.exe", "AzurPromilia_Data", "AzurPromiliaJP_Data")
    ):
        return p.parent
    return p


HINTS = ("azurpromilia", "azupro", "yostar")


def _scan_roots(base: Path) -> list[Path]:
    """盘符根或 Program Files 下，名字像启动器安装目录的目录。"""
    out: list[Path] = []
    if not base.is_dir():
        return out
    try:
        children = list(base.iterdir())
    except OSError:
        return out
    for child in children:
        if not child.is_dir():
            continue
        low = child.name.lower()
        if low.startswith(HINTS):
            out.append(child)
        elif low in ("program files", "program files (x86)"):
            try:
                for sub in child.iterdir():
                    if sub.is_dir() and sub.name.lower().startswith(HINTS):
                        out.append(sub)
            except OSError:
                pass
    return out


def _manual_paths() -> list[Path]:
    try:
        data = json.loads(MANUAL_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("launchers") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [Path(item) for item in items if isinstance(item, str) and item.strip()]


def add_manual(raw: str | Path) -> Install:
    path = Path(raw).expanduser()
    install = from_path(path)
    resolved = str(path.resolve())
    paths = _manual_paths()
    if all(str(item.resolve()).lower() != resolved.lower() for item in paths):
        paths.append(path)
    MANUAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = MANUAL_FILE.with_suffix(MANUAL_FILE.suffix + ".tmp")
    temp.write_text(json.dumps({"launchers": [str(p) for p in paths]},
                               ensure_ascii=False, indent=2), "utf-8")
    temp.replace(MANUAL_FILE)
    return install


def find_installs(extra_roots: list[str] | None = None) -> list[Install]:
    roots: list[Path] = []
    for game_dir in _registry_game_dirs():
        roots.append(_install_root_of(game_dir))
    for drive in string.ascii_uppercase:
        base = Path(f"{drive}:/")
        if not base.exists():
            continue
        roots.extend(_scan_roots(base))
    roots.extend(_manual_paths())
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
        if root.is_file():
            try:
                installs.append(from_path(root))
            except (OSError, ValueError):
                continue
        else:
            installs.extend(_installs_in(root))

    # 去重：同 root 只保留版本号最大的启动器
    best: dict[str, Install] = {}
    for inst in installs:
        key = str(inst.root.resolve()).lower()
        cur = best.get(key)
        if cur is None or _version_key(inst.version) > _version_key(cur.version):
            best[key] = inst
    ordered = sorted(best.values(), key=lambda i: str(i.root).lower())
    unique: dict[str, Install] = {}
    for inst in ordered:
        unique.setdefault(str(inst.launcher_exe.resolve()).lower(), inst)
    return sorted(unique.values(), key=lambda i: str(i.root).lower())


def _version_key(text: str) -> tuple:
    parts = []
    for chunk in text.replace("-", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)

def _is_version_dir(name: str) -> bool:
    """是不是 `1.0.0` 这种版本目录。

    不能用 `_version_key(name) != (0,)` 判断：`azurpromilia-jp` 这种带横线的普通目录
    会得到 (0, 0)，于是扁平安装的日服启动器会被多往上一层，root 直接算错。
    """
    return bool(re.fullmatch(r"[0-9][0-9.\-]*", name))
