"""国服反虚化补丁：替换一个相机资源包。

只改 `.res/default_package/BundleFiles/rjmh704fkrsjimysndpsg.r` 里的相机预制体，
文件大小不变、逐段 CRC 已配平，启动器按 manifest 校验能过。

这是修改客户端资源，官方协议不允许，存在封号风险，所以 install 必须显式加
`--accept-risk`。替换文件放在 `azp/patches/cn/nofade/`，也可以用 `--payload` 指定。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import hashes, preflight
from .journal import Entry
from .plan import safe_rel

PATCH_DIR = Path(__file__).resolve().parent / "patches" / "cn" / "nofade"
BACKUP_NAME = ".nofade-backup"
META = "backup.json"

WARNING = (
    "  警告：会替换游戏客户端里的资源包，属于修改客户端文件。\n"
    "  官方用户协议不允许，被检测到可能封号；账号和数据的后果由使用者承担。\n"
    "  这不是可选的装饰项，只在明确接受风险时使用。"
)


class NofadeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Patch:
    name: str
    file: str  # 相对 AzurPromilia_Data 的路径
    size: int
    original_md5: str
    patched_md5: str
    resource_version: str

    @property
    def filename(self) -> str:
        return Path(self.file).name


def load() -> Patch:
    path = PATCH_DIR / "patch.json"
    if not path.exists():
        raise NofadeError(f"补丁元数据缺失：{path}")
    data = json.loads(path.read_text("utf-8"))
    # patch.json 可手工编辑 / 随补丁包分发，file 越界就会把补丁写到客户端之外
    return Patch(
        name=str(data.get("name") or "反虚化"),
        file=safe_rel(str(data["file"])),
        size=int(data["size"]),
        original_md5=str(data["original_md5"]),
        patched_md5=str(data["patched_md5"]),
        resource_version=str(data.get("resource_version") or ""),
    )


def _data_dir(target: Path) -> Path:
    if (target / "AzurPromilia_Data").is_dir():
        return target / "AzurPromilia_Data"
    if target.name == "AzurPromilia_Data" and target.is_dir():
        return target
    raise NofadeError(f"{target} 不像国服客户端（找不到 AzurPromilia_Data）")


def paths(target: Path, patch: Patch,
          payload: Path | None = None) -> tuple[Path, Path, Path]:
    """返回 (游戏文件, 备份, 补丁文件)。"""
    data = _data_dir(target)
    return (
        data / Path(patch.file),
        target / BACKUP_NAME / patch.filename,
        payload or (PATCH_DIR / patch.filename),
    )


def _find_backup(target: Path, patch: Patch) -> Path | None:
    """兼容本工具、原版 install.bat 和手工备份的位置。"""
    candidates = (
        target / BACKUP_NAME / patch.filename,
        target / "AzurPromilia_Data" / BACKUP_NAME / patch.filename,
        target / ".cn-nofade-backup" / patch.filename,
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def _base_file(base: Path, patch: Patch) -> Path:
    return _data_dir(base) / Path(patch.file)


def _relink(dest: Path, base: Path | None, patch: Patch) -> bool:
    """从源区重建硬链接。先链到临时名再替换，失败不会弄丢 dest。"""
    if base is None:
        return False
    try:
        src = _base_file(base, patch)
        if src.resolve() == dest.resolve():
            return False
        if hashes.md5_of(src) != patch.original_md5:
            return False
    except (OSError, NofadeError):
        return False
    tmp = dest.with_suffix(dest.suffix + ".nofade.tmp")
    try:
        os.link(src, tmp)
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    print(f"  已和源区重建硬链接：{src}")
    return True


def state(target: Path) -> str:
    """只读判断补丁文件状态：patched / original / missing / unknown。

    quickstart 用它决定是否提示「硬链接会带走补丁」或「复核会覆盖补丁」；
    补丁元数据缺失、目录不像客户端时一律 unknown，不因此中断主流程。
    """
    try:
        patch = load()
        dest = _data_dir(target) / Path(patch.file)
    except (NofadeError, OSError, ValueError, KeyError):
        return "unknown"
    try:
        md5 = hashes.md5_of(dest)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"
    if md5 == patch.patched_md5:
        return "patched"
    if md5 == patch.original_md5:
        return "original"
    return "unknown"


def rel_path(patch: Patch | None = None) -> str:
    """补丁文件相对 `AzurPromilia_Data` 的路径，用于按后缀匹配清单条目。

    清单的键通常带 `AzurPromilia_Data/` 前缀，按后缀匹配才不受目标区目录结构影响。
    """
    try:
        patch = patch or load()
    except (NofadeError, OSError, ValueError, KeyError):
        return ""
    return Path(patch.file).as_posix().lstrip("/")


def status(target: Path) -> int:
    patch = load()
    dest, _backup, payload = paths(target, patch)
    print(f"  补丁 {patch.name}（国服资源版本 {patch.resource_version}）")
    print(f"  文件 {dest}")
    if not dest.exists():
        print("  状态 文件不存在")
        return 1

    md5 = hashes.md5_of(dest)
    if md5 == patch.patched_md5:
        print("  状态 已安装")
    elif md5 == patch.original_md5:
        print("  状态 原版")
    else:
        print(f"  状态 未知（{md5}）")
        print("  游戏可能更新过，这个补丁不适用")
        return 1

    backup = _find_backup(target, patch)
    if backup is None:
        print("  备份 没有")
    elif hashes.md5_of(backup) == patch.original_md5:
        print(f"  备份 {backup}")
    else:
        print(f"  备份 {backup}（校验不符，别用它恢复）")
    print(f"  补丁文件 {'有' if payload.exists() else '缺失'}：{payload}")
    if not payload.exists():
        print("  azp/patches/cn/nofade/ 下应该有这个文件；也可以用 --payload 指定")
    return 0


def install(target: Path, payload: Path | str | None = None, accept_risk: bool = False,
            yes: bool = False, dry: bool = False) -> int:
    print(WARNING)
    patch = load()
    payload_path = Path(payload) if payload else PATCH_DIR / patch.filename
    dest, backup, _ = paths(target, patch, payload_path)
    if not dest.exists():
        print(f"  找不到 {dest}")
        return 1

    md5 = hashes.md5_of(dest)
    if md5 == patch.patched_md5:
        print("  已经装过了")
        return 0
    if not accept_risk:
        print("  已拒绝：改客户端资源可能导致封号，--yes 不算确认。")
        print("  确认接受风险后加 --accept-risk 再跑。")
        return 1
    if not payload_path.exists():
        print(f"  补丁文件不存在：{payload_path}")
        print("  azp/patches/cn/nofade/ 下应该有这个文件；也可以用 --payload 指定")
        return 1
    if hashes.md5_of(payload_path) != patch.patched_md5:
        print(f"  补丁文件校验不符：{payload_path}")
        print(f"  期望 {patch.patched_md5}，不是这个版本的补丁就别用")
        return 1

    running = preflight.running_games()
    if running:
        print(f"  游戏正在运行（{'、'.join(running)}），先完全退出")
        return 1

    if md5 != patch.original_md5:
        print(f"  当前文件不是资源版本 {patch.resource_version} 的原版")
        print(f"  实际 MD5 {md5}")
        print("  游戏更新过的话这个补丁不适用；先 restore 再等新版本")
        return 1

    links = dest.stat().st_nlink
    print(f"  将替换 {dest}")
    print(f"  原文件备份到 {backup}")
    if links > 1:
        print(f"  注意：这个文件有 {links} 个硬链接（和别的区共用），只断这个区，别的区不动")
    if dry:
        print("  [试运行] 没有改文件")
        return 0
    if not yes:
        try:
            input("  回车继续，Ctrl+C 取消: ")
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消")
            return 1

    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(dest, backup)
    if hashes.md5_of(backup) != patch.original_md5:
        print(f"  备份校验不符，停下：{backup}")
        return 1

    entry = Entry("nofade", target)
    entry.backup(dest)
    tmp = dest.with_suffix(dest.suffix + ".nofade.tmp")
    shutil.copy2(payload_path, tmp)
    os.replace(tmp, dest)
    got = hashes.md5_of(dest)
    if got != patch.patched_md5:
        print(f"  写入后校验不符（{got}），还原")
        shutil.copy2(backup, tmp)
        os.replace(tmp, dest)
        return 1

    entry.add_created(dest, patch.size)
    entry.save()
    (backup.parent / META).write_text(
        json.dumps(
            {
                "file": patch.file,
                "original_md5": patch.original_md5,
                "patched_md5": patch.patched_md5,
                "resource_version": patch.resource_version,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    print(f"  已安装，原文件在 {backup}")
    print("  大版本更新前先跑 python -m azp nofade restore")
    return 0


def restore(target: Path, base: Path | None = None,
            yes: bool = False, dry: bool = False) -> int:
    patch = load()
    dest, _backup, _payload = paths(target, patch)
    if not dest.exists():
        print(f"  找不到 {dest}")
        return 1

    md5 = hashes.md5_of(dest)
    if md5 == patch.original_md5:
        if not dry:
            _relink(dest, base, patch)
        print("  已经是原版，不用恢复")
        return 0

    backup = _find_backup(target, patch)
    if backup is None:
        print(f"  没有备份：{target / BACKUP_NAME / patch.filename}")
        print("  只能让启动器做一次校验修复，或者重新下载这个文件")
        return 1
    if hashes.md5_of(backup) != patch.original_md5:
        print(f"  备份校验不对，别用它恢复：{backup}")
        return 1

    running = preflight.running_games()
    if running:
        print(f"  游戏正在运行（{'、'.join(running)}），先完全退出")
        return 1

    print(f"  将恢复 {dest}")
    print(f"  来源 {backup}")
    if dry:
        print("  [试运行] 没有改文件")
        return 0
    if not yes:
        try:
            input("  回车继续，Ctrl+C 取消: ")
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消")
            return 1

    if _relink(dest, base, patch):
        return 0

    tmp = dest.with_suffix(dest.suffix + ".nofade.tmp")
    shutil.copy2(backup, tmp)
    os.replace(tmp, dest)
    got = hashes.md5_of(dest)
    if got != patch.original_md5:
        print(f"  恢复后校验不符（{got}）")
        return 1
    print("  已恢复原版")
    return 0
