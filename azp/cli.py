"""命令行入口：python -m azp <子命令>"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from . import __version__, channels, detect, export, fetch, indexfile, link, manifest, plan
from .api import Api

DEFAULT_OUT = Path("out")

REGION_KEYS = {
    ("354", "C000"): "cn",
    ("354", "C001"): "bili",
    ("434", "C500"): "global",
}


def region_key(ch: channels.Channel) -> str:
    return REGION_KEYS.get((ch.app_id, ch.channel_code), f"app{ch.app_id}")


def fmt_gib(n: int | float) -> str:
    return f"{n / 2**30:.2f} GiB"


def cmd_detect(args) -> int:
    installs = detect.find_installs(args.root or [])
    if not installs:
        print("没有找到启动器。可用 --root 指定安装根目录，例如 --root D:\\AzurPromilia")
        return 1
    for inst in installs:
        print(f"启动器 {inst.launcher_exe}")
        for game in inst.game_dirs:
            print(f"  游戏 {game}")
        try:
            ch = channels.load(inst.launcher_exe)
            info = ch.public()
            print(f"  渠道 {info['label']} appId={info['app_id']} 渠道号={info['channel_code']} "
                  f"gameId={info['game_id']} → {region_key(ch)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  渠道读取失败：{exc}")
        print()
    return 0


def _install_by_region(wanted: str) -> detect.Install | None:
    for inst in detect.find_installs():
        try:
            if region_key(channels.load(inst.launcher_exe)) == wanted:
                return inst
        except Exception:  # noqa: BLE001
            continue
    return None


def cmd_manifests(args) -> int:
    out = Path(args.out)
    wanted = set(args.region or [])
    ok = 0
    for inst in detect.find_installs():
        try:
            ch = channels.load(inst.launcher_exe)
        except Exception as exc:  # noqa: BLE001
            print(f"跳过 {inst.launcher_exe}：{exc}")
            continue
        key = region_key(ch)
        if wanted and key not in wanted:
            continue
        print(f"[{key}] {inst.root}")
        api = Api(ch)
        manifest.fetch(api, out / key, force=args.force)
        ok += 1
        print()
    if not ok:
        print("没有可用的启动器，先跑 azp detect")
        return 1
    return 0


def cmd_plan(args) -> int:
    base_p = Path(args.out) / args.base / "manifest.json"
    target_p = Path(args.out) / args.target / "manifest.json"
    for p in (base_p, target_p):
        if not p.exists():
            print(f"缺少清单 {p}，先运行：azp manifests")
            return 1
    base = json.loads(base_p.read_text("utf-8"))
    target = json.loads(target_p.read_text("utf-8"))
    print(f"基线 {base['label']} {base.get('version_code')} → 目标 {target['label']} {target.get('version_code')}")

    local = Path(args.local) if args.local else None
    rows, stats = plan.build(base, target, local)
    print(stats.render(target["total_size"]))

    csv_path = Path(args.csv or Path(args.out) / f"plan_{args.base}_to_{args.target}.csv")
    plan.write_csv(rows, csv_path)
    print(f"  计划表 {csv_path}")
    if args.dry_run:
        return 0
    return 0


def cmd_fetch(args) -> int:
    rows = plan.read_csv(Path(args.plan))
    target = Path(args.target)
    stats = fetch.run(rows, target, workers=args.workers)
    return 0 if not stats.failed else 2


def cmd_link(args) -> int:
    rows = plan.read_csv(Path(args.plan))
    stats = link.run(
        rows,
        base_dir=Path(args.base),
        target_dir=Path(args.target),
        local_dir=Path(args.local) if args.local else None,
        workers=args.workers,
        dry=args.dry_run,
    )
    return 0 if not stats.failed else 2


def cmd_export(args) -> int:
    rows = plan.read_csv(Path(args.plan))
    stats = export.run(
        rows,
        source=Path(args.source),
        dest=Path(args.dest),
        include_links=args.include_links,
        dry=args.dry_run,
        hardlink=getattr(args, "hardlink", False),
    )
    return 0 if not stats["missing"] else 2


def cmd_import(args) -> int:
    """从局域网共享（或任意已装好的目录）只拉取需要下载的那部分文件。"""
    rows = plan.read_csv(Path(args.plan))
    print(f"来源 {args.source}")
    print(f"目标 {args.target}")
    stats = export.run(
        rows,
        source=Path(args.source),
        dest=Path(args.target),
        include_links=args.include_links,
        dry=args.dry_run,
    )
    if not stats["missing"]:
        print("\n接着跑 azp fetch --plan <同一计划> --target <同一目录>，已就位的会被跳过。")
    return 0 if not stats["missing"] else 2


def cmd_finalize(args) -> int:
    target = Path(args.target)
    if not target.is_dir():
        print(f"目标目录不存在：{target}")
        return 1
    data = json.loads(Path(args.manifest).read_text("utf-8"))
    written = indexfile.write(
        target_game_dir=target,
        version_code=data["version_code"],
        version_name=data.get("version_name") or "1.0.0",
        index_csv=Path(args.index_csv),
        paths_from=Path(args.paths_from) if args.paths_from else None,
    )
    for p in written:
        print(f"  写入 {p}")
    folder = data.get("game_folder_name")
    if folder and target.name != folder:
        print(f"\n目录名应为 {folder}，当前是 {target.name}，否则启动器找不到 exe")
    return 0


def cmd_quickstart(args) -> int:
    """交互式向导：一条命令走完全流程。"""
    print("蓝色星原：旅谣 国服/国际服切换")
    print("需要：一个区的客户端已装好，另一个区只装了启动器")
    print()

    installs = detect.find_installs()
    if not installs:
        print("没有找到启动器。先安装，或用 --root 指定安装根目录。")
        return 1

    entries = []
    for inst in installs:
        try:
            ch = channels.load(inst.launcher_exe)
        except Exception as exc:  # noqa: BLE001
            print(f"  跳过 {inst.launcher_exe}：{exc}")
            continue
        entries.append((inst, ch))

    if not entries:
        print("启动器配置读不出来。")
        return 1

    print("检测到以下安装：")
    for i, (inst, ch) in enumerate(entries, 1):
        games = "、".join(p.name for p in inst.game_dirs) or "（无游戏目录）"
        print(f"  {i}. {ch.label}  根目录 {inst.root}  游戏 {games}")
    print()

    ready = [(inst, ch) for inst, ch in entries if inst.game_dirs]
    if not ready:
        print("还没有装好的游戏目录，先装一个区的客户端。")
        return 1

    if len(ready) == 1:
        base_inst, base_ch = ready[0]
        print(f"基线：{base_ch.label}  {base_inst.game_dirs[0]}")
    else:
        pick = _ask("选择作为基线的编号", "1")
        try:
            base_inst, base_ch = ready[int(pick) - 1]
        except (ValueError, IndexError):
            print("编号无效")
            return 1

    others = [(inst, ch) for inst, ch in entries if region_key(ch) != region_key(base_ch)]
    if not others:
        print("只检测到一个区服，还需要另一个区的启动器。")
        return 1
    if len(others) == 1:
        target_inst, target_ch = others[0]
        print(f"目标：{target_ch.label}  根目录 {target_inst.root}")
    else:
        print("\n可选的另一区：")
        for i, (_inst, ch) in enumerate(others, 1):
            print(f"  {i}. {ch.label}  {_inst.root}")
        pick = _ask("选择目标编号", "1")
        try:
            target_inst, target_ch = others[int(pick) - 1]
        except (ValueError, IndexError):
            print("编号无效")
            return 1

    base_dir = base_inst.game_dirs[0]
    out = Path(args.out)

    print("\n[1/5] 抓取两个区的文件清单（约 30 MB）")
    base_man = manifest.fetch(Api(base_ch), out / region_key(base_ch), force=args.force)
    target_man = manifest.fetch(Api(target_ch), out / region_key(target_ch), force=args.force)

    folder = target_man.get("game_folder_name") or (
        target_inst.game_dirs[0].name if target_inst.game_dirs else ""
    )
    if not folder:
        folder = _ask("目标区要求的游戏目录名（例如 AzurPromilia_TC_game）", "")
        if not folder:
            print("缺少目录名")
            return 1
    target_dir = target_inst.root / folder
    print(f"\n目标目录：{target_dir}")
    if target_dir.exists() and any(target_dir.iterdir()):
        print("  该目录已有内容，只补缺失文件。")

    print("\n[2/5] 计算增量")
    rows, stats = plan.build(base_man, target_man, base_dir)
    print(stats.render(target_man["total_size"]))
    csv_path = out / f"plan_{region_key(base_ch)}_to_{region_key(target_ch)}.csv"
    plan.write_csv(rows, csv_path)
    print(f"  计划表 {csv_path}")

    drive_ok = str(base_dir).split(":")[0].lower() == str(target_dir).split(":")[0].lower()
    if not drive_ok:
        print("\n两个目录不在同一分区，硬链接不能跨卷，先挪到一起。")
        return 1

    if not _ask_yes("\n开始建硬链接并下载", default=False):
        print("停在计划阶段。之后手动执行：")
        print(f"  python -m azp link  --plan {csv_path} --base \"{base_dir}\" --local \"{base_dir}\" --target \"{target_dir}\"")
        print(f"  python -m azp fetch --plan {csv_path} --target \"{target_dir}\" --workers {args.jobs}")
        return 0

    print("\n[3/5] 建立硬链接")
    link.run(rows, base_dir=base_dir, target_dir=target_dir, local_dir=base_dir, workers=8)

    print("\n[4/5] 下载缺失文件，Ctrl+C 中断后重跑会继续")
    fetch.run(rows, target_dir, workers=args.jobs)

    print("\n[5/5] 补齐 _index 并校验")
    written = indexfile.write(
        target_game_dir=target_dir,
        version_code=target_man["version_code"],
        version_name=target_man.get("version_name") or "1.0.0",
        index_csv=out / region_key(target_ch) / "index.csv",
        paths_from=base_dir,
    )
    for p in written:
        print(f"  写入 {p}")
    _verify_dir(target_man, target_dir, args.sample)

    print("\n完成。启动目标区启动器做一次校验修复再进游戏。两个区别同时开。")
    return 0


def _verify_dir(data: dict, root: Path, sample: int = 20) -> bool:
    files = data["files"]
    missing = [p for p in files if not (root / p).exists()]
    wrong = [p for p in files if (root / p).exists() and (root / p).stat().st_size != files[p]["size"]]
    print(f"  清单 {len(files)} 个文件，缺失 {len(missing)}，大小不符 {len(wrong)}")
    bad = 0
    if sample and not missing and not wrong:
        for path in random.sample(sorted(files), min(sample, len(files))):
            if plan.md5_of(root / path) != files[path]["md5"]:
                bad += 1
                print(f"    ! MD5 不符 {path}")
        print(f"  抽样 {sample} 个，通过 {sample - bad} 个")
    return not (missing or wrong or bad)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise SystemExit("\n已取消") from None
    return raw or default


def _ask_yes(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    return _ask(f"{prompt} ({hint})", "Y" if default else "N").lower().startswith("y")


def cmd_verify(args) -> int:
    data = json.loads(Path(args.manifest).read_text("utf-8"))
    root = Path(args.dir)
    print(f"清单 {len(data['files'])} 个 / {fmt_gib(data['total_size'])}")
    return 0 if _verify_dir(data, root, args.sample) else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="azp", description="蓝色星原 国服/国际服客户端切换工具")
    p.add_argument("--version", action="version", version=f"azp {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("detect", help="列出本机找到的启动器与游戏目录")
    d.add_argument("--root", action="append", help="额外搜索的安装根目录，可重复")
    d.set_defaults(func=cmd_detect)

    q = sub.add_parser("quickstart", help="交互式向导：一条命令走完全流程")
    q.add_argument("--out", default=str(DEFAULT_OUT))
    q.add_argument("--jobs", type=int, default=16, help="下载并发数")
    q.add_argument("--sample", type=int, default=20, help="校验时抽样 MD5 的文件数")
    q.add_argument("--force", action="store_true", help="强制重抓清单")
    q.set_defaults(func=cmd_quickstart)

    m = sub.add_parser("manifests", help="抓取各区逐文件清单")
    m.add_argument("--out", default=str(DEFAULT_OUT))
    m.add_argument("--region", action="append", help="只抓指定区（cn/bili/global）")
    m.add_argument("--force", action="store_true")
    m.set_defaults(func=cmd_manifests)

    pl = sub.add_parser("plan", help="生成硬链接 + 增量下载计划")
    pl.add_argument("--base", required=True)
    pl.add_argument("--target", required=True)
    pl.add_argument("--out", default=str(DEFAULT_OUT))
    pl.add_argument("--local", help="额外扫描的本地目录（如已装客户端的 BundleFiles）")
    pl.add_argument("--csv")
    pl.add_argument("--dry-run", action="store_true")
    pl.set_defaults(func=cmd_plan)

    f = sub.add_parser("fetch", help="按计划下载缺失文件")
    f.add_argument("--plan", required=True)
    f.add_argument("--target", required=True)
    f.add_argument("--workers", type=int, default=16)
    f.set_defaults(func=cmd_fetch)

    lk = sub.add_parser("link", help="按计划建立硬链接")
    lk.add_argument("--plan", required=True)
    lk.add_argument("--base", required=True)
    lk.add_argument("--local")
    lk.add_argument("--target", required=True)
    lk.add_argument("--workers", type=int, default=8)
    lk.add_argument("--dry-run", action="store_true")
    lk.set_defaults(func=cmd_link)

    ex = sub.add_parser("export", help="导出需要下载的那部分，便于离线拷给另一台机器")
    ex.add_argument("--plan", required=True)
    ex.add_argument("--source", required=True, help="已装好的目标客户端目录")
    ex.add_argument("--dest", required=True, help="导出到这个目录（会保留相对路径）")
    ex.add_argument("--include-links", action="store_true", help="连硬链接文件一起导出（体积会很大）")
    ex.add_argument("--hardlink", action="store_true",
                    help="用硬链接做暂存目录：同一分区内几乎不占空间，适合先导出再往外发")
    ex.add_argument("--dry-run", action="store_true")
    ex.set_defaults(func=cmd_export)

    im = sub.add_parser("import", help="从局域网共享只拉取需要下载的那部分")
    im.add_argument("--plan", required=True)
    im.add_argument("--source", required=True, help=r"来源目录，例如 \\HOST\share\AzurPromilia_TC_game")
    im.add_argument("--target", required=True, help="本地目标目录")
    im.add_argument("--include-links", action="store_true", help="连硬链接文件一起拉（体积会很大）")
    im.add_argument("--dry-run", action="store_true")
    im.set_defaults(func=cmd_import)

    fi = sub.add_parser("finalize", help="补齐目标客户端的 _index 缓存")
    fi.add_argument("--target", required=True)
    fi.add_argument("--manifest", required=True)
    fi.add_argument("--index-csv", required=True)
    fi.add_argument("--paths-from", help="从哪个已装客户端复制 _index/paths 模板")
    fi.set_defaults(func=cmd_finalize)

    v = sub.add_parser("verify", help="按清单校验目录完整性")
    v.add_argument("--manifest", required=True)
    v.add_argument("--dir", required=True)
    v.add_argument("--sample", type=int, default=20, help="抽样做 MD5 的文件数，0 表示跳过")
    v.set_defaults(func=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
