"""命令行入口：python -m azp <子命令>"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

from . import (
    __version__,
    channels,
    detect,
    export,
    fetch,
    hashes,
    indexfile,
    journal,
    link,
    manifest,
    native,
    nofade,
    plan,
    preflight,
    regions,
    remove,
    repair,
    yostar,
)
from .api import Api

DEFAULT_OUT = Path("out")
PREFS_NAME = "quickstart.json"


def region_key(ch: channels.Channel) -> str:
    return regions.key_for(ch.app_id, ch.channel_code)


def fmt_gib(n: int | float) -> str:
    return f"{n / 2**30:.2f} GiB"


def _load_prefs(out: Path) -> dict:
    try:
        data = json.loads((out / PREFS_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_prefs(out: Path, base: str, target: str, target_dir: Path) -> None:
    """记住上次选择，下次作为交互默认值；写不进去不影响主流程。"""
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / PREFS_NAME).write_text(json.dumps(
            {"base": base, "target": target, "target_dir": str(target_dir)},
            ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        pass


def _region_label(key: str) -> str:
    try:
        found = regions.find(key)
    except (OSError, ValueError):
        found = None
    return found.label if found else key


def _nofade_notice(base_dir: Path, target_dir: Path, is_jp: bool, reinstall: bool,
                   can_ask: bool, dry_run: bool) -> set[str]:
    """检测反虚化补丁与本次流程的冲突，返回要跳过的文件后缀。

    源区有补丁时硬链接会把它带到目标区；目标区有补丁时复核修复会按官方清单
    覆盖它。这里只提示并允许保留，不改动 nofade 的任何状态。
    """
    if is_jp:
        return set()
    try:
        patch = nofade.load()
        suffix = nofade.rel_path(patch)
    except (nofade.NofadeError, OSError, ValueError, KeyError):
        return set()
    if not suffix:
        return set()

    def state(root: Path) -> str:
        try:
            md5 = hashes.md5_of(root / "AzurPromilia_Data" / Path(patch.file))
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unknown"
        if md5 == patch.patched_md5:
            return "patched"
        if md5 == patch.original_md5:
            return "original"
        return "unknown"

    source = state(base_dir)
    target = state(target_dir)
    ignore: set[str] = set()
    if source == "patched":
        print("  注意：源区已装反虚化补丁，硬链接会把这个文件带到目标区。")
        print("        目标区要官方文件时，先对源区执行 nofade restore。")
    if target == "patched":
        print("  注意：目标区已装反虚化补丁，复核修复会用官方文件覆盖它。")
        if can_ask:
            print("    1. 保留补丁（跳过该文件的校验与修复）")
            print("    2. 按官方清单修复（补丁被覆盖）")
            if _ask("选择", "1") == "1":
                ignore.add(suffix)
                print("  已保留补丁。")
            else:
                print("  将按官方清单覆盖补丁。")
        else:
            ignore.add(suffix)
            if dry_run:
                print("  本次只计算计划，未改动文件；正式执行时会跳过该文件。")
            else:
                print("  已保留补丁；要按官方清单覆盖，请先执行 nofade restore。")
    if reinstall and target in ("patched", "original"):
        print("  注意：--reinstall 会把目标区的 .nofade-backup 一并移入回收站，之后无法 restore。")
    return ignore


def cmd_detect(args) -> int:
    installs = detect.find_installs(args.root or [])
    if not installs:
        print("没有找到启动器。可用 --root 指定安装根目录，例如 --root D:\\AzurPromilia")
        return 1
    for inst in installs:
        print(f"启动器 {inst.launcher_exe}")
        for game in inst.game_dirs:
            print(f"  游戏 {game}")
        if inst.kind == "yostar":
            print("  渠道 日服（Yostar），没有 app_config.dat；"
                  "清单用 python -m azp manifests --region jp")
            print()
            continue
        try:
            ch = channels.load(inst.launcher_exe)
            info = ch.public()
            print(f"  渠道 {info['label']} appId={info['app_id']} 渠道号={info['channel_code']} "
                  f"gameId={info['game_id']} → {region_key(ch)}")
            for line in _region_hint(ch):
                print(line)
        except FileNotFoundError as exc:
            print(f"  渠道读取失败：{exc}")
            print("  没找到 res/app_config.dat，认不出是哪个渠道。")
        except Exception as exc:  # noqa: BLE001
            print(f"  渠道读取失败：{exc}")
        print()
    return 0


def _region_hint(ch: channels.Channel) -> list[str]:
    region = regions.resolve(ch.app_id, ch.channel_code)
    if region is not None and region.ready:
        return []
    return [
        "  提示 这个渠道不在区服表里，抓不了清单。知道它属于哪个区的话，按 regions.example.json",
        f"       的格式往 regions.json 加一项。当前 appId={ch.app_id} channelCode={ch.channel_code}",
    ]


def cmd_regions(args) -> int:
    path = regions.config_path()
    print(f"配置文件 {path}{'' if path.exists() else '（不存在，只用内置表）'}")
    print()
    try:
        items = regions.load()
    except ValueError as exc:
        print(f"  {exc}")
        print("  改好再跑，或先把它删掉用内置表。")
        return 1
    for r in items:
        if r.channel_codes:
            codes = f"{r.app_id}/{'、'.join(r.channel_codes)}"
        else:
            codes = r.app_id or "未配置"
        domain = r.domains[0] if r.domains else "—"
        if r.note:
            tail = f"   ← {r.note}"
        elif not r.ready:
            tail = "   ← 缺 app_id、渠道号或域名"
        else:
            tail = ""
        print(f"  {r.key}\t{r.label}\t{codes}\t{domain}{tail}")
    print()
    print("没匹配到的渠道回落成 app<appId>：能 detect，抓不了清单。")
    print("加区服：在 regions.json 写 {\"regions\": {\"xx\": {...}}}，字段见 regions.example.json。")
    return 0


def cmd_nofade(args) -> int:
    target = Path(args.target)
    try:
        if args.action == "status":
            return nofade.status(target)
        if args.action == "install":
            return nofade.install(
                target,
                payload=args.payload,
                accept_risk=args.accept_risk,
                yes=args.yes,
                dry=args.dry_run,
            )
        return nofade.restore(
            target,
            base=Path(args.base) if args.base else None,
            yes=args.yes,
            dry=args.dry_run,
        )
    except nofade.NofadeError as exc:
        print(f"  {exc}")
        return 1


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
    known = {r.key for r in regions.load()}
    for bad in sorted(wanted - known):
        print(f"区服表里没有 {bad}，先跑 python -m azp regions 看看有哪些")
    ok = 0
    seen: set[str] = set()
    for inst in detect.find_installs():
        try:
            ch = channels.load(inst.launcher_exe)
        except Exception as exc:  # noqa: BLE001
            print(f"跳过 {inst.launcher_exe}：{exc}")
            continue
        key = region_key(ch)
        if wanted and key not in wanted:
            continue
        seen.add(key)
        print(f"[{key}] {inst.root}")
        api = Api(ch)
        try:
            manifest.fetch(api, out / key, force=args.force)
        except Exception as exc:  # noqa: BLE001
            print(f"  失败：{exc}")
            print()
            continue
        ok += 1
        print()
    for region in regions.load():
        if region.kind != "yostar" or not region.ready:
            continue
        if wanted and region.key not in wanted:
            continue
        seen.add(region.key)
        try:
            yostar.fetch(out / region.key, force=args.force)
        except Exception as exc:  # noqa: BLE001
            print(f"  失败：{exc}")
        else:
            ok += 1
        print()
    missing = sorted(wanted - seen)
    if missing:
        print(f"没有找到这些区的启动器：{'、'.join(missing)}")
    if not ok:
        print("没有抓成功的区，按上面的报错处理；启动器没装就先跑 python -m azp detect 看看")
        return 1
    return 0


def cmd_plan(args) -> int:
    base_p = Path(args.out) / args.base / "manifest.json"
    target_p = Path(args.out) / args.target / "manifest.json"
    for p in (base_p, target_p):
        if not p.exists():
            have = sorted(x.parent.name for x in Path(args.out).glob("*/manifest.json"))
            hint = f"（已有 {', '.join(have)}）" if have else ""
            print(f"缺少清单 {p}，先运行 python -m azp manifests{hint}")
            return 1
    base = json.loads(base_p.read_text("utf-8"))
    target = json.loads(target_p.read_text("utf-8"))
    print(f"基线 {base['label']} {base.get('version_code')} → 目标 {target['label']} {target.get('version_code')}")

    local = Path(args.local) if args.local else None
    cache = Path(args.out) / "cache" / "crc64.json"
    rows, stats = plan.build(
        base, target, local,
        base_dir=Path(args.base_dir) if args.base_dir else None,
        cache=cache,
        trust_names=args.trust_names,
    )
    print(stats.render(target["total_size"]))

    csv_path = Path(args.csv or Path(args.out) / f"plan_{args.base}_to_{args.target}.csv")
    plan.write_csv(rows, csv_path)
    print(f"  计划表 {csv_path}")
    if args.dry_run:
        return 0
    return 0


def _save_journal(entry: journal.Entry) -> Path | None:
    path = entry.save()
    if path is not None:
        print(f"  账本 {path}（改坏了用 python -m azp rollback 撤销）")
    return path


def _fetch_with_retry(rows: list[dict], target: Path, workers: int,
                      entry: journal.Entry) -> fetch.Stats:
    """下一轮，失败的文件再试一次；还失败就交给 verify/repair。"""
    stats = fetch.run(rows, target, workers=workers, journal=entry)
    if stats.failed:
        failed = {path for path, _ in stats.failed}
        retry_rows = [r for r in rows if r["path"] in failed]
        if retry_rows:
            print(f"  重试 {len(retry_rows)} 个失败文件")
            again = fetch.run(retry_rows, target, workers=max(4, workers // 2), journal=entry)
            stats.failed = again.failed
    return stats


def cmd_fetch(args) -> int:
    rows = plan.read_csv(Path(args.plan))
    target = Path(args.target)
    entry = journal.Entry("fetch", target)
    try:
        stats = _fetch_with_retry(rows, target, args.workers, entry)
    finally:
        _save_journal(entry)
    return 0 if not stats.failed else 2


def cmd_link(args) -> int:
    rows = plan.read_csv(Path(args.plan))
    target = Path(args.target)
    entry = journal.Entry("link", target)
    try:
        stats = link.run(
            rows,
            base_dir=Path(args.base),
            target_dir=target,
            local_dir=Path(args.local) if args.local else None,
            workers=args.workers,
            dry=args.dry_run,
            journal=entry,
        )
    finally:
        if not args.dry_run:
            _save_journal(entry)
    return 0 if not stats.failed else 2


def cmd_export(args) -> int:
    source, dest = Path(args.source), Path(args.dest)
    if args.full:
        try:
            stats = export.run_full(source, dest, hardlink=args.hardlink, dry=args.dry_run)
        except (OSError, ValueError) as exc:
            print(f"  {exc}")
            return 1
        return 0 if not stats["missing"] else 2
    if not args.plan:
        print("要 --plan 只导增量，或者 --full 搬整个客户端。")
        return 1
    rows = plan.read_csv(Path(args.plan))
    try:
        stats = export.run(
            rows,
            source=source,
            dest=dest,
            include_links=args.include_links,
            dry=args.dry_run,
            hardlink=args.hardlink,
        )
    except ValueError as exc:
        print(f"  {exc}")
        return 1
    return 0 if not stats["missing"] else 2


def cmd_import(args) -> int:
    """从局域网共享（或任意已装好的目录）只拉取需要下载的那部分文件。"""
    rows = plan.read_csv(Path(args.plan))
    print(f"来源 {args.source}")
    print(f"目标 {args.target}")
    try:
        stats = export.run(
            rows,
            source=Path(args.source),
            dest=Path(args.target),
            include_links=args.include_links,
            dry=args.dry_run,
        )
    except ValueError as exc:
        print(f"  {exc}")
        return 1
    if not stats["missing"]:
        print("\n接着跑 azp fetch --plan <同一计划> --target <同一目录>，已就位的会被跳过。")
    return 0 if not stats["missing"] else 2


def cmd_finalize(args) -> int:
    target = Path(args.target)
    if not target.is_dir():
        print(f"目标目录不存在：{target}")
        return 1
    data = json.loads(Path(args.manifest).read_text("utf-8"))
    entry = journal.Entry("finalize", target)
    if hashes.kind_of(data) == hashes.CRC64:
        for name in ("manifest.json", "game-launcher-config.json"):
            entry.backup(target / name)
        try:
            written = yostar.finalize(target, data)
        except yostar.YostarError as exc:
            print(f"  {exc}")
            return 1
        for p in written:
            print(f"  写入 {p}")
            entry.add_created(p, p.stat().st_size)
        _save_journal(entry)
        print("  打开日服启动器，应当直接认出已安装，不用重下")
        return 0
    if not args.index_csv:
        print("国服/台服要 --index-csv（清单解出来的 index.csv）")
        return 1
    folder = data.get("game_folder_name")
    if folder and target.name != folder:
        print(f"目录名应为 {folder}，当前是 {target.name}；未写入任何索引文件")
        return 1
    written = indexfile.write(
        target_game_dir=target,
        version_code=data["version_code"],
        version_name=data.get("version_name") or "1.0.0",
        index_csv=Path(args.index_csv),
        paths_from=Path(args.paths_from) if args.paths_from else None,
        journal=entry,
    )
    for p in written:
        print(f"  写入 {p}")
        # 不记 created 的话，首次 finalize（没有旧文件可备份）账本是空的，
        # save() 会直接返回 None，用户这次写入就完全没法 rollback
        entry.add_created(p, p.stat().st_size)
    _save_journal(entry)

    return 0


def cmd_check(args) -> int:
    base = Path(args.base) if args.base else None
    target = Path(args.target) if args.target else None
    need = int(args.need * 2**30) if args.need else 0
    manifest_data = None
    if args.manifest:
        manifest_data = json.loads(Path(args.manifest).read_text("utf-8"))
    if not need and args.plan:
        rows = plan.read_csv(Path(args.plan))
        need = preflight.pending_bytes(rows, target) if target else sum(
            r["size"] for r in rows if r["action"] == "download"
        )
    if base is None and target is None:
        print("只检查了运行环境。加 --base / --target 可以连磁盘一起查。")
    ok, items = preflight.run(base, target, need_bytes=need, manifest=manifest_data)
    print(preflight.render(items))
    return 0 if ok else 2


def cmd_remove(args) -> int:
    return remove.run(
        Path(args.target),
        base=Path(args.base) if args.base else None,
        yes=args.yes,
        dry=args.dry_run,
        force=args.force,
        trash=args.trash,
    )


def cmd_repair(args) -> int:
    target = Path(args.target)
    data = json.loads(Path(args.manifest).read_text("utf-8"))
    stats = repair.run(
        target,
        data,
        base=Path(args.base) if args.base else None,
        workers=args.workers,
        sample=args.sample,
        full=args.full,
        dry=args.dry_run,
        cache=Path("out") / "cache" / "crc64.json",
    )
    return 0 if not stats["failed"] else 2


def cmd_rollback(args) -> int:
    target = Path(args.target) if args.target else None
    path = journal.find(target, args.id)
    if path is None:
        rows = journal.available(target)
        if not rows:
            print("没有可回退的记录。账本写在 out/journal/，由 link、fetch、finalize、quickstart、nofade 产生。")
            return 1
        print("可回退的记录：")
        for item in rows[-10:]:
            try:
                data = journal.load(item)
            except (OSError, ValueError):
                continue
            when = time.strftime("%m-%d %H:%M", time.localtime(data.get("time", 0)))
            print(f"  {item.name}  {when}  新建 {len(data.get('created', [])):,} 个  "
                  f"覆盖 {len(data.get('backups', []))} 个")
        print("\n用 --id <文件名> 指定，不加参数默认用最近一条。")
        return 0

    data = journal.load(path)
    print(f"账本 {path.name}")
    print(f"  目标 {data['target']}")
    print(f"  新建 {len(data['created']):,} 个文件，覆盖过 {len(data['backups'])} 个")
    print("  回退会删掉这些新建文件，并把覆盖前的备份放回去；源区不受影响")

    if args.dry_run:
        removed, restored, skipped, notes = journal.rollback(path, dry=True)
        print(f"试运行：会删 {removed:,} 个，还原 {restored} 个，保留 {skipped} 个")
        for line in notes[:8]:
            print(f"  {line}")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("当前不是交互终端，确认无误请加 --yes 再执行。")
            return 1
        if not _ask_yes("确认撤销", default=False):
            print("已取消。")
            return 1

    removed, restored, skipped, notes = journal.rollback(path)
    print(f"已删 {removed:,} 个新建文件，还原 {restored} 个备份，保留 {skipped} 个")
    for line in notes[:8]:
        print(f"  {line}")
    if skipped or notes:
        print("被保留的是操作之后又被改过或删不掉的文件，自己看一眼再决定。")
        # 没退干净就留着账本：删了就没法针对同一笔再跑一次 rollback
        print(f"账本保留在 {path}，处理完可以重跑同一条 rollback。")
        return 2
    path.unlink(missing_ok=True)
    print("账本已作废，同一笔不能再回退。")
    return 0


def cmd_gui(args) -> int:
    """启动本地图形界面，复用 CLI 的检测与计划逻辑。"""
    from .ui.server import serve

    return serve(out=args.out, port=args.port, open_browser=not args.no_browser)


def cmd_quickstart(args) -> int:
    """交互式向导：一条命令走完全流程。"""
    print("蓝色星原：旅谣 国服 / 台服 / 日服切换")
    print("需要：至少一个区已装好当源；目标区可以是空目录，也可以已装好客户端")
    print("      目标区已有的同大小文件不重新下载，只补差异")
    out = Path(args.out)
    prefs = _load_prefs(out)
    if prefs.get("base") and prefs.get("target"):
        last = f"上次：{_region_label(prefs['base'])} → {_region_label(prefs['target'])}"
        if prefs.get("target_dir"):
            last += f"  {prefs['target_dir']}"
        print(last)
    print()

    installs = detect.find_installs(args.root or [])
    manual: detect.Install | None = None
    if args.launcher:
        try:
            manual = detect.from_path(args.launcher)
        except (OSError, FileNotFoundError, ValueError) as exc:
            print(f"  手动指定的启动器读不了：{exc}")
            return 1
        if manual.kind == "yostar":
            print("  这是日服启动器，日服不用指定；把 --target 设成 jp 就行。")
            return 1
        if all(i.launcher_exe != manual.launcher_exe for i in installs):
            installs.append(manual)
    if not installs:
        print("没有找到启动器。先安装，或用 --root 指定安装根目录，例如 --root D:\\AzurPromilia；")
        print("装在别处也可以用 --launcher 直接指 launcher.exe 或版本目录。")
        return 1

    entries = []
    for inst in installs:
        if inst.kind == "yostar":
            continue
        try:
            ch = channels.load(inst.launcher_exe)
        except Exception as exc:  # noqa: BLE001
            print(f"  跳过 {inst.launcher_exe}：{exc}")
            continue
        entries.append((inst, ch))

    if not entries:
        print("没有可用的国服/台服启动器。日服不能单独加另一个区，先把目标区启动器装上。")
        return 1

    ready = [(inst, ch) for inst, ch in entries if inst.game_dirs]
    if not ready:
        print("还没有装好的游戏目录，先装一个区的客户端。")
        return 1
    if not args.base:
        for inst, ch in entries:
            if not inst.game_dirs:
                print(f"  {ch.label} 只装了启动器（{inst.root}），可作为目标区")

    if args.base:
        key = args.base.lower()
        match = [(i, c) for i, c in ready if region_key(c) == key]
        if not match:
            have = "、".join(sorted({region_key(c) for _, c in ready}))
            print(f"  --base {args.base} 没找到已装好的客户端；当前可用：{have}")
            return 1
        if manual is not None:
            match.sort(key=lambda pair: pair[0].launcher_exe != manual.launcher_exe)
        base_inst, base_ch = match[0]
        print(f"基线：{base_ch.label}  {base_inst.game_dirs[0]}")
    elif len(ready) == 1:
        base_inst, base_ch = ready[0]
        print(f"基线：{base_ch.label}  {base_inst.game_dirs[0]}")
    else:
        print("检测到以下已装好的客户端：")
        for i, (inst, ch) in enumerate(ready, 1):
            print(f"  {i}. {ch.label}  {inst.game_dirs[0]}")
        default = "1"
        for i, (_inst, ch) in enumerate(ready, 1):
            if prefs.get("base") and region_key(ch) == prefs["base"]:
                default = str(i)
                break
        pick = _ask("选择作为基线的编号", default)
        try:
            base_inst, base_ch = ready[int(pick) - 1]
        except (ValueError, IndexError):
            print("编号无效")
            return 1

    others = [(inst, ch) for inst, ch in entries if region_key(ch) != region_key(base_ch)]
    if args.target:
        key = args.target.lower()
        if key == region_key(base_ch):
            print(f"  --target 和基线是同一个区（{key}），换一个")
            return 1
        if key == "jp":
            target_inst, target_ch = None, None
        else:
            match = [(i, c) for i, c in others if region_key(c) == key]
            if not match:
                print(f"  --target {args.target} 没找到对应的启动器；先装它，"
                      f"装在别处就用 --launcher 指 launcher.exe")
                return 1
            if manual is not None:
                match.sort(key=lambda pair: pair[0].launcher_exe != manual.launcher_exe)
            target_inst, target_ch = match[0]
    else:
        # 日服不靠 detect：清单是匿名接口，目标目录自己指定就行，不需要先有日服启动器
        choices: list[tuple[detect.Install | None, channels.Channel | None]] = list(others)
        choices.append((None, None))
        print("\n加哪个区？")
        for i, (inst, ch) in enumerate(choices, 1):
            if ch is None:
                print(f"  {i}. 日服（Yostar）  目录自己指定，不用先装启动器")
            else:
                print(f"  {i}. {ch.label}  {inst.root}")
        default = "1"
        for i, (_inst, ch) in enumerate(choices, 1):
            key = "jp" if ch is None else region_key(ch)
            if prefs.get("target") == key:
                default = str(i)
                break
        pick = _ask("选择编号", default)
        try:
            target_inst, target_ch = choices[int(pick) - 1]
        except (ValueError, IndexError):
            print("编号无效")
            return 1

    base_dir = base_inst.game_dirs[0]

    is_jp = target_ch is None

    print("\n[1/5] 抓取两个区的文件清单（约 30 MB）")
    try:
        base_man = manifest.fetch(Api(base_ch), out / region_key(base_ch), force=args.force)
        if is_jp:
            target_man = yostar.fetch(out / "jp", force=args.force)
        else:
            target_man = manifest.fetch(Api(target_ch), out / region_key(target_ch), force=args.force)
    except Exception as exc:  # noqa: BLE001
        print(f"  抓清单失败：{exc}")
        if is_jp:
            print("  日服清单走 api-launcher-jp.yo-star.com，确认网络能连上。")
        else:
            print("  先看 python -m azp regions：这个区的域名配好了没有，网络通不通。")
        return 1

    if is_jp:
        folder = "AZUPRO_JP"
        if args.target_dir:
            target_dir = Path(args.target_dir)
        else:
            last = prefs.get("target_dir") if prefs.get("target") == "jp" else ""
            if not last or Path(last).name != folder:
                last = str(base_dir.parent.parent / folder)
            raw = _ask(f"日服客户端放在哪（目录名必须是 {folder}）", last)
            target_dir = Path(raw)
        # 必须和 yostar.finalize 用同一种比较（精确匹配），否则这里放行、
        # 几十 GiB 下完之后 finalize 才报「目录名必须是 AZUPRO_JP」
        if target_dir.name != folder:
            print(f"  目录名必须叫 {folder}（大小写也要一致），启动器按这个名字认；"
                  f"现在是 {target_dir.name}")
            return 1
        if native.find() is None:
            print("  日服算 CRC64 要用 7za。装日服启动器（自带 7za），或设 AZP_7ZA 指向 7za.exe。")
            return 1
    else:
        known = regions.find(region_key(target_ch))
        folder = (known.folder if known and known.folder else "") or (
            target_inst.game_dirs[0].name if target_inst.game_dirs else ""
        )
        if args.target_dir:
            target_dir = Path(args.target_dir)
            if folder and target_dir.name != folder:
                print(f"  目录名应为 {folder}，当前是 {target_dir.name}")
                return 1
        elif folder:
            target_dir = target_inst.root / folder
        else:
            example = "AzurPromilia_TC_game"
            last = ""
            if prefs.get("target") == region_key(target_ch) and prefs.get("target_dir"):
                last = Path(prefs["target_dir"]).name
            folder = _ask(f"目标区要求的游戏目录名（例如 {example}）", last)
            if not folder:
                print("缺少目录名")
                return 1
            target_dir = target_inst.root / folder
    real_folder = target_man.get("game_folder_name")
    if real_folder and target_dir.name != real_folder:
        print(f"  清单要求目录名 {real_folder}，当前是 {target_dir.name}，否则启动器找不到")
        return 1
    try:
        target_root = target_dir.resolve()
        base_root = base_dir.resolve()
    except OSError:
        target_root = target_dir
        base_root = base_dir
    if target_root == base_root:
        print("  目标目录和源区是同一个，换一个")
        return 1
    if target_root.is_relative_to(base_root) or base_root.is_relative_to(target_root):
        print("  目标目录和源区不能互相包含")
        return 1
    print(f"\n目标目录：{target_dir}")
    if target_dir.exists() and not target_dir.is_dir():
        print(f"  这个路径是个文件，不是目录：{target_dir}")
        return 1
    reinstall = False
    if target_dir.exists() and any(target_dir.iterdir()):
        if args.dry_run:
            print("  该目录已有内容（--dry-run 只算增量，不动文件）")
        elif args.reinstall:
            print("  已选择清空重装；完成环境检查和最终确认后才移入回收站")
            reinstall = True
        elif args.yes:
            print("  该目录已有内容，只补缺失（要重装加 --reinstall）")
        else:
            print("  该目录已有内容：")
            print("    1. 保留，只补缺失")
            print("    2. 清空重装（整个目录进回收站）")
            print("    3. 取消")
            pick = _ask("选择", "1")
            if pick == "3":
                return 1
            if pick == "2":
                reinstall = True

    print("\n[2/5] 计算增量")
    rows, stats = plan.build(
        base_man, target_man, base_dir, base_dir,
        cache=out / "cache" / "crc64.json",
        existing=None if reinstall else target_dir,
    )
    print(stats.render(target_man["total_size"]))
    target_key = "jp" if is_jp else region_key(target_ch)
    csv_path = out / f"plan_{region_key(base_ch)}_to_{target_key}.csv"
    plan.write_csv(rows, csv_path)
    print(f"  计划表 {csv_path}")

    print("\n环境检查")
    ok, items = preflight.run(
        base_dir,
        target_dir,
        need_bytes=preflight.pending_bytes(rows, target_dir),
        manifest=target_man,
        probe_links=not args.dry_run,
    )
    print(preflight.render(items))
    if not ok:
        print("\n有致命项，处理好再跑一次。")
        return 1

    ignore = _nofade_notice(base_dir, target_dir, is_jp, reinstall,
                            can_ask=not args.dry_run and not args.yes, dry_run=args.dry_run)

    gib = stats.download_bytes / 2**30
    if gib >= 0.5:
        print(f"\n要下载 {gib:.2f} GiB，按 10 MiB/s 估算约 {gib * 1024 / 10 / 60:.0f} 分钟，实际看网络")
    if args.dry_run:
        print("\n--dry-run：停在计划阶段，没有往目标目录写文件；计划表在 out\\")
        return 0
    if not args.yes and not _ask_yes("\n开始建硬链接并下载", default=False):
        print("停在计划阶段。之后手动执行：")
        print(f"  python -m azp link  --plan {csv_path} --base \"{base_dir}\" --local \"{base_dir}\" --target \"{target_dir}\"")
        print(f"  python -m azp fetch --plan {csv_path} --target \"{target_dir}\" --workers {args.jobs}")
        return 0

    _save_prefs(out, region_key(base_ch), target_key, target_dir)

    if reinstall:
        print("  最终确认已完成，现有目标目录移入回收站")
        code = remove.run(target_dir, base=base_dir, yes=True, force=True,
                          trash=True, fallback=False)
        if code != 0:
            return code

    entry = journal.Entry("quickstart", target_dir)
    try:
        print("\n[3/5] 建立硬链接")
        link.run(rows, base_dir=base_dir, target_dir=target_dir, local_dir=base_dir,
                 workers=8, journal=entry)
        _save_journal(entry)

        print("\n[4/5] 下载缺失文件，Ctrl+C 中断后重跑会继续")
        _fetch_with_retry(rows, target_dir, args.jobs, entry)

        if is_jp:
            print("\n[5/5] 写日服的安装标记，校验并在必要时修复")
            for name in ("manifest.json", "game-launcher-config.json"):
                entry.backup(target_dir / name)
            try:
                written = yostar.finalize(target_dir, target_man)
            except yostar.YostarError as exc:
                print(f"  {exc}")
                return 1
        else:
            print("\n[5/5] 补齐 _index，校验并在必要时修复")
            written = indexfile.write(
                target_game_dir=target_dir,
                version_code=target_man["version_code"],
                version_name=target_man.get("version_name") or "1.0.0",
                index_csv=out / region_key(target_ch) / "index.csv",
                paths_from=base_dir,
                journal=entry,
            )
        for p in written:
            print(f"  写入 {p}")
            entry.add_created(p, p.stat().st_size)
        cache = out / "cache" / "crc64.json"
        if not _verify_dir(target_man, target_dir, args.sample, cache, ignore):
            print("\n  对不上的文件按清单修一遍")
            fixed = repair.run(
                target_dir, target_man, base=base_dir, workers=args.jobs, sample=args.sample,
                cache=cache, ignore=ignore,
            )
            if fixed["failed"]:
                print(f"  还有 {len(fixed['failed'])} 个修不好，先别进游戏；"
                      f"重跑 python -m azp repair --full")
                _save_journal(entry)
                return 1
            print("\n  复查")
            if not _verify_dir(target_man, target_dir, args.sample, cache, ignore):
                print("  复查仍未通过，先别进游戏；重跑 python -m azp repair --full")
                _save_journal(entry)
                return 1
        _save_journal(entry)
    except KeyboardInterrupt:
        _save_journal(entry)
        print("\n已中断。重跑同一条命令会继续；要撤销这次写入：")
        print(f'  python -m azp rollback --target "{target_dir}"')
        return 130

    print("\n完成。启动目标区启动器做一次校验修复再进游戏。两个区不要同时开。")
    print(f'改坏了：python -m azp rollback --target "{target_dir}" 撤销这次写入，'
          f'python -m azp repair 只修坏文件。')
    return 0


def _verify_dir(data: dict, root: Path, sample: int = 20,
                cache: Path | None = None, ignore: set[str] | None = None) -> bool:
    files = data["files"]
    kind = hashes.kind_of(data)
    suffixes = set(ignore or ())
    skipped = {p for p in files if any(p.endswith(s) for s in suffixes if s)}
    if skipped:
        print(f"  跳过已装补丁的文件 {len(skipped)} 个")
    missing: list[str] = []
    wrong: list[str] = []
    for p, info in files.items():
        if p in skipped:
            continue
        try:
            size = (root / p).stat().st_size
        except OSError:
            missing.append(p)
            continue
        if size != info["size"]:
            wrong.append(p)
    print(f"  清单 {len(files)} 个文件，缺失 {len(missing)}，大小不符 {len(wrong)}")
    bad = 0
    if sample and not missing and not wrong:
        pool = sorted(p for p in files if p not in skipped)
        picks = random.sample(pool, min(sample, len(pool)))
        table: dict[str, int] = {}
        if kind == hashes.CRC64:
            try:
                table = native.crc64_for(root, picks, cache)
            except native.NativeError as exc:
                print(f"    {exc}")
        for path in picks:
            if kind == hashes.CRC64 and table and path in table:
                want = int(files[path]["md5"]) if str(files[path]["md5"]).isdigit() else -1
                ok = table[path] == want
            else:
                ok = hashes.digest(root / path, kind) == files[path]["md5"]
            if not ok:
                bad += 1
                print(f"    ! {hashes.label(kind)} 不符 {path}")
        print(f"  抽样 {len(picks)} 个，通过 {len(picks) - bad} 个")
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
    return 0 if _verify_dir(data, root, args.sample, Path("out") / "cache" / "crc64.json") else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="azp", description="蓝色星原 国服/台服/日服客户端切换工具")
    p.add_argument("--version", action="version", version=f"azp {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("detect", help="列出本机找到的启动器与游戏目录")
    d.add_argument("--root", action="append", help="额外搜索的安装根目录，可重复")
    d.set_defaults(func=cmd_detect)

    rg = sub.add_parser("regions", help="列出区服表与本地 regions.json 配置")
    rg.set_defaults(func=cmd_regions)

    ck = sub.add_parser("check", help="检查环境、文件系统与硬链接能力")
    ck.add_argument("--base", help="源区游戏目录")
    ck.add_argument("--target", help="目标区游戏目录，可以还不存在")
    ck.add_argument("--manifest", help="目标区清单，用来算最长路径")
    ck.add_argument("--plan", help="计划表，用其中的下载量作为空间需求")
    ck.add_argument("--need", type=float, help="需要多少 GiB，优先于 --plan")
    ck.set_defaults(func=cmd_check)

    rm = sub.add_parser("remove", help="删除一个区的客户端，不影响另一区")
    rm.add_argument("--target", required=True, help="要删的客户端目录")
    rm.add_argument("--base", help="另一区目录，用来防止误删")
    rm.add_argument("--dry-run", action="store_true", help="只统计，不删")
    rm.add_argument("--yes", action="store_true", help="跳过确认")
    rm.add_argument("--force", action="store_true", help="目录不像客户端时也允许删")
    rm.add_argument("--trash", action="store_true", help="移入回收站而不是彻底删除")
    rm.set_defaults(func=cmd_remove)

    rb = sub.add_parser("rollback", help="撤销上一次写入，按 out/journal 账本回退")
    rb.add_argument("--target", help="只看这个目标目录的账本")
    rb.add_argument("--id", help="指定账本文件名")
    rb.add_argument("--dry-run", action="store_true", help="只算不删")
    rb.add_argument("--yes", action="store_true", help="跳过确认")
    rb.set_defaults(func=cmd_rollback)

    q = sub.add_parser("quickstart", help="交互式向导：一条命令走完全流程")
    q.add_argument("--out", default=str(DEFAULT_OUT))
    q.add_argument("--jobs", type=int, default=16, help="下载并发数")
    q.add_argument("--sample", type=int, default=20, help="校验时抽样校验值的文件数")
    q.add_argument("--force", action="store_true", help="强制重抓清单")
    q.add_argument("--root", action="append", help="额外搜索的安装根目录，可重复")
    q.add_argument("--base", help="源区代号（cn/bili/global），跳过基线选择")
    q.add_argument("--target", help="要加的区代号（cn/bili/global/jp），跳过选择")
    q.add_argument("--target-dir", help="目标客户端目录，跳过询问")
    q.add_argument("--launcher", help="启动器路径（launcher.exe、版本目录、安装根或游戏目录）")
    q.add_argument("--reinstall", action="store_true", help="目标目录有内容时移入回收站重装")
    q.add_argument("--yes", action="store_true", help="不再确认，直接开始")
    q.add_argument("--dry-run", action="store_true", help="只算计划和环境检查，不写文件")
    q.set_defaults(func=cmd_quickstart)

    gu = sub.add_parser("gui", help="启动图形界面（本地网页窗口）")
    gu.add_argument("--out", default=str(DEFAULT_OUT))
    gu.add_argument("--port", type=int, default=0, help="监听端口，默认随机")
    gu.add_argument("--no-browser", action="store_true", help="只启动服务，不自动打开浏览器")
    gu.set_defaults(func=cmd_gui)

    m = sub.add_parser("manifests", help="抓取各区逐文件清单")
    m.add_argument("--out", default=str(DEFAULT_OUT))
    m.add_argument("--region", action="append", help="只抓指定区（cn/bili/global/jp，见 azp regions）")
    m.add_argument("--force", action="store_true")
    m.set_defaults(func=cmd_manifests)

    pl = sub.add_parser("plan", help="生成硬链接 + 增量下载计划")
    pl.add_argument("--base", required=True)
    pl.add_argument("--target", required=True)
    pl.add_argument("--out", default=str(DEFAULT_OUT))
    pl.add_argument("--local", help="额外扫描的本地目录（如已装客户端的 BundleFiles）")
    pl.add_argument("--base-dir", help="基区客户端目录；两区校验方式不同时用它逐个核对内容")
    pl.add_argument("--trust-names", action="store_true",
                    help="两区校验方式不同时，跳过内容核对，直接按同名同大小建链接")
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

    ex = sub.add_parser("export", help="导出增量或整个客户端，便于离线拷给另一台机器")
    ex.add_argument("--plan", help="计划表；只导增量时必填，--full 时不用")
    ex.add_argument("--source", required=True, help="已装好的客户端目录")
    ex.add_argument("--dest", required=True, help="导出到这个目录（会保留相对路径）")
    ex.add_argument("--full", action="store_true",
                    help="整客户端导出（含 _index）；接收方只装启动器就能用")
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

    fi = sub.add_parser("finalize", help="写启动器认的索引：Manjuu 系 _index，日服 manifest.json")
    fi.add_argument("--target", required=True)
    fi.add_argument("--manifest", required=True)
    fi.add_argument("--index-csv", help="清单解出来的 index.csv；日服不需要")
    fi.add_argument("--paths-from", help="从哪个已装客户端复制 _index/paths 模板")
    fi.set_defaults(func=cmd_finalize)

    nf = sub.add_parser("nofade", help="风险功能：反虚化（改客户端资源，可能封号）")
    nf.add_argument("action", choices=["status", "install", "restore"])
    nf.add_argument("--target", required=True,
                    help="游戏目录，例如 C:/AzurPromilia/AzurPromilia_Game")
    nf.add_argument("--base", help="restore 时从这个区重建硬链接")
    nf.add_argument("--payload", help="install 用的补丁资源包，默认取 patches/cn/nofade/")
    nf.add_argument("--accept-risk", action="store_true",
                    help="确认已知晓封号风险，install 必填；--yes 不能代替")
    nf.add_argument("--yes", action="store_true", help="跳过回车确认")
    nf.add_argument("--dry-run", action="store_true")
    nf.set_defaults(func=cmd_nofade)

    v = sub.add_parser("verify", help="按清单校验目录完整性")
    v.add_argument("--manifest", required=True)
    v.add_argument("--dir", required=True)
    v.add_argument("--sample", type=int, default=20, help="抽样做 MD5 的文件数，0 表示跳过")
    v.set_defaults(func=cmd_verify)

    rp = sub.add_parser("repair", help="按清单检查并修好坏掉的文件")
    rp.add_argument("--target", required=True)
    rp.add_argument("--manifest", required=True)
    rp.add_argument("--base", help="源区目录，有同 MD5 的文件就重建硬链接而不是下载")
    rp.add_argument("--sample", type=int, default=20, help="抽样做 MD5 的文件数，0 表示只比大小")
    rp.add_argument("--full", action="store_true", help="全量校验 MD5，95 GiB 要跑几分钟")
    rp.add_argument("--workers", type=int, default=16)
    rp.add_argument("--dry-run", action="store_true")
    rp.set_defaults(func=cmd_repair)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"路径不对：{exc}")
        return 1
    except json.JSONDecodeError as exc:
        print(f"JSON 解析失败（清单或 regions.json 被改坏了？）：{exc}")
        return 1
    except ValueError as exc:
        print(f"{exc}")
        return 1
    except OSError as exc:
        print(f"系统调用失败：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
