"""后台执行任务：建链接、下载、写索引、校验修复。

界面只负责启动任务和轮询快照；所有文件操作都走 CLI 同款的底层模块，
因此命令行与图形界面的行为一致。
"""

from __future__ import annotations

import contextlib
import io
import threading
import traceback
import uuid
from pathlib import Path

from .. import fetch, indexfile, journal, link, nofade, preflight, remove, repair, yostar
from . import backend, channels_swap


class _LogWriter(io.TextIOBase):
    """把底层模块的 print 收进任务日志，界面里能直接看到。"""

    def __init__(self, job: "Job"):
        self.job = job

    def write(self, text: str) -> int:
        line = text.rstrip()
        if line:
            self.job.log(line)
        return len(text)


class Job:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.status = "running"
        self.phase = "准备"
        self.message = ""
        self.done_files = 0
        self.total_files = 0
        self.done_bytes = 0
        self.total_bytes = 0
        self.link_done = 0
        self.link_bytes = 0
        self.logs: list[str] = []
        self.error = ""
        self.result: dict = {}
        self.cancel = False
        self._lock = threading.Lock()

    def log(self, text: str) -> None:
        with self._lock:
            self.logs.append(text)
            if len(self.logs) > 400:
                self.logs = self.logs[-400:]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "phase": self.phase,
                "message": self.message,
                "done_files": self.done_files,
                "total_files": self.total_files,
                "done_bytes": self.done_bytes,
                "total_bytes": self.total_bytes,
                "logs": list(self.logs),
                "error": self.error,
                "result": dict(self.result),
            }


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()
_CURRENT: str | None = None


def _on_link(job: Job, stats: link.LinkStats) -> None:
    job.link_done = stats.created + stats.skipped + len(stats.failed)
    job.link_bytes = stats.bytes
    job.done_files = job.link_done
    job.done_bytes = job.link_bytes
    job.message = f"链接 {stats.created:,} · 跳过 {stats.skipped:,} · 失败 {len(stats.failed):,}"


def _on_fetch(job: Job, stats: fetch.Stats) -> None:
    job.done_files = job.link_done + stats.done_files
    job.done_bytes = job.link_bytes + stats.done_bytes
    job.message = f"下载 {stats.done_files:,}/{stats.total_files:,}"


def _stop_if_cancelled(job: Job) -> bool:
    if not job.cancel:
        return False
    job.status = "cancelled"
    job.phase = "已取消"
    job.log("已取消；已写入的文件可用 rollback 撤销")
    return True


def start(payload: dict) -> dict:
    global _CURRENT
    action = str(payload.get("action") or "switch")
    if action in ("switch", "plan"):
        base = str(payload.get("base") or "")
        target = str(payload.get("target") or "")
        if not base or not target:
            raise ValueError("先选择源区和目标区")
        if base == target:
            raise ValueError("源区和目标区不能相同")
    elif action != "journal-clean" and not payload.get("target"):
        raise ValueError("没有指定目标目录")
    with _LOCK:
        if _CURRENT is not None and _JOBS.get(_CURRENT) and _JOBS[_CURRENT].status == "running":
            raise ValueError("已有任务在运行")
        job = Job()
        _JOBS[job.id] = job
        _CURRENT = job.id
    threading.Thread(target=_run, args=(job, dict(payload)), daemon=True).start()
    return {"id": job.id}


def get(payload: dict) -> dict:
    job_id = str(payload.get("id") or "")
    job = _JOBS.get(job_id)
    if job is None:
        raise ValueError("任务不存在或已结束")
    return job.snapshot()


def cancel(payload: dict) -> dict:
    job_id = str(payload.get("id") or "")
    job = _JOBS.get(job_id)
    if job is None:
        raise ValueError("任务不存在")
    job.cancel = True
    job.log("收到取消请求，当前阶段结束后停止")
    return {"id": job.id}


def _run(job: Job, payload: dict) -> None:
    with contextlib.redirect_stdout(_LogWriter(job)):
        try:
            action = str(payload.get("action") or "switch")
            if action == "switch":
                _execute(job, payload)
            elif action == "plan":
                _execute_plan(job, payload)
            else:
                _execute_maintenance(job, payload)
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = str(exc)
            job.phase = "失败"
            job.log(f"失败：{exc}")
            job.log(traceback.format_exc(limit=3))


def _execute_plan(job: Job, payload: dict) -> None:
    job.phase = "计算计划"
    job.log("读取清单并比对；日服首次核对 CRC64 可能需要几分钟")
    result = backend.plan_preview(payload)
    job.result = {"plan": result}
    job.status = "done"
    job.phase = "完成"
    job.message = "计划已生成"
    job.log("计划已生成")


def _execute(job: Job, payload: dict) -> None:
    out = Path(payload.get("out") or "out")
    out.mkdir(parents=True, exist_ok=True)
    journal.JOURNAL_DIR = out / "journal"
    journal.BACKUP_DIR = out / "backup"

    job.log("准备清单并计算计划")
    data = backend.prepare(payload)
    base_dir: Path = data["base_dir"]
    target_dir: Path = data["target_dir"]
    rows = data["rows"]
    stats = data["stats"]
    target_man = data["target_man"]

    job.total_files = stats.link_files + stats.download_files
    job.total_bytes = stats.link_bytes + stats.download_bytes
    job.log(f"硬链接 {stats.link_files:,}，已就绪 {stats.ready_files:,}，"
            f"下载 {stats.download_files:,}，新增占用 {stats.download_bytes / 2**30:.2f} GiB")

    job.phase = "环境检查"
    ok, items = preflight.run(
        base=base_dir, target=target_dir,
        need_bytes=preflight.pending_bytes(rows, target_dir),
        manifest=target_man, probe_links=True,
    )
    if not ok:
        raise RuntimeError("环境检查有致命项，先在命令行运行 python -m azp check 排查")

    ignore: set[str] = set()
    suffix = nofade.rel_path()
    if suffix and nofade.state(target_dir) == "patched":
        ignore.add(suffix)
        job.log("目标区已装反虚化补丁，本次跳过该文件，不覆盖")
    if nofade.state(base_dir) == "patched":
        job.log("源区已装反虚化补丁，硬链接会把它带到目标区")

    if _stop_if_cancelled(job):
        return

    if data["reinstall"] and target_dir.exists():
        job.phase = "清空目标目录"
        job.log("现有目标目录移入回收站")
        if remove.run(target_dir, base=base_dir, yes=True, force=True,
                      trash=True, fallback=False) != 0:
            raise RuntimeError("清空目标目录失败")

    entry = journal.Entry("gui", target_dir)

    job.phase = "建立硬链接"
    link.run(rows, base_dir=base_dir, target_dir=target_dir, local_dir=base_dir,
             workers=8, journal=entry, on_change=lambda s: _on_link(job, s))
    if entry.created:
        entry.save()
    if _stop_if_cancelled(job):
        return

    job.phase = "下载差异"
    workers = int(payload.get("jobs") or 16)
    result = fetch.run(rows, target_dir, workers=workers, journal=entry,
                       on_change=lambda s: _on_fetch(job, s))
    if result.failed:
        job.log(f"{len(result.failed)} 个文件失败，重试一次")
        failed = {path for path, _ in result.failed}
        result = fetch.run([r for r in rows if r["path"] in failed], target_dir,
                           workers=workers, journal=entry)
    if result.failed:
        raise RuntimeError(f"仍有 {len(result.failed)} 个文件下载失败，重跑可继续")
    if _stop_if_cancelled(job):
        return

    job.phase = "写入索引"
    if data["target_key"] == "jp":
        for name in ("manifest.json", "game-launcher-config.json"):
            entry.backup(target_dir / name)
        written = yostar.finalize(target_dir, target_man)
    else:
        written = indexfile.write(
            target_game_dir=target_dir,
            version_code=target_man["version_code"],
            version_name=target_man.get("version_name") or "1.0.0",
            index_csv=out / data["target_key"] / "index.csv",
            paths_from=base_dir,
            journal=entry,
        )
    for path in written:
        entry.add_created(path, path.stat().st_size)
        job.log(f"写入 {path.name}")

    job.phase = "校验与修复"
    from ..cli import _verify_dir

    cache = out / "cache" / "crc64.json"
    sample = int(payload.get("sample") or 20)
    if not _verify_dir(target_man, target_dir, sample, cache, ignore):
        job.log("校验不一致，按清单修复")
        fixed = repair.run(target_dir, target_man, base=base_dir, workers=workers,
                           sample=sample, cache=cache, ignore=ignore)
        if fixed["failed"]:
            raise RuntimeError(f"{len(fixed['failed'])} 个文件修复失败，可运行 repair --full")
        if not _verify_dir(target_man, target_dir, sample, cache, ignore):
            raise RuntimeError("复查仍未通过，可运行 repair --full")

    if entry.created:
        entry.save()
    job.status = "done"
    job.phase = "完成"
    job.message = "已完成"
    job.result = {"target": str(target_dir), "written": [str(p) for p in written]}
    job.log("完成。启动目标区启动器做一次校验修复再进游戏。")

def _execute_maintenance(job: Job, payload: dict) -> None:
    action = str(payload.get("action") or "")
    out = Path(payload.get("out") or "out")
    out.mkdir(parents=True, exist_ok=True)
    journal.JOURNAL_DIR = out / "journal"
    journal.BACKUP_DIR = out / "backup"
    target = Path(str(payload.get("target") or ""))
    base = Path(str(payload["base"])) if payload.get("base") else None

    if action == "nofade-install":
        job.phase = "安装反虚化补丁"
        code = nofade.install(target, accept_risk=True, yes=True)
    elif action == "nofade-restore":
        job.phase = "还原官方文件"
        code = nofade.restore(target, base=base, yes=True)
    elif action == "channel-extract":
        job.phase = "提取渠道源"
        server = str(payload.get("server") or "")
        dest = channels_swap.extract(target, server)
        job.result = {"channel_extract": {"server": server, "path": str(dest)}}
        job.log(f"已提取{channels_swap.LABELS.get(server, server)}渠道源：{dest}")
        code = 0
    elif action == "channel-switch":
        job.phase = "切换渠道"
        from_server = str(payload.get("from") or "")
        to_server = str(payload.get("to") or "")
        entry = journal.Entry("channel", target)
        backup = channels_swap.switch(target, from_server, to_server, journal=entry)
        if entry.backups:
            entry.save()
        job.result = {"channel_switch": {
            "from": from_server, "to": to_server, "backup": str(backup),
        }}
        job.log(f"已切换到{channels_swap.LABELS.get(to_server, to_server)}，"
                f"原文件备份在 {backup}")
        code = 0
    elif action == "channel-restore":
        job.phase = "还原渠道备份"
        backup = Path(str(payload.get("backup") or "")).resolve()
        allowed = (Path("out") / "backup" / "channel").resolve()
        if allowed != backup and allowed not in backup.parents:
            raise ValueError("备份路径不在允许目录内")
        channels_swap.restore(backup, target)
        job.result = {"channel_restore": {"backup": str(backup)}}
        job.log("已还原渠道备份")
        code = 0
    elif action == "repair":
        job.phase = "按清单修复"
        key = str(payload.get("key") or "")
        if not key:
            raise ValueError("缺少区服代号")
        manifest_data, _ = backend._manifest_for(key, out, bool(payload.get("force")))
        ignore: set[str] = set()
        suffix = nofade.rel_path()
        if suffix and nofade.state(target) == "patched":
            ignore.add(suffix)
            job.log("目标区已装补丁，跳过该文件")
        fixed = repair.run(
            target, manifest_data, base=base,
            workers=int(payload.get("jobs") or 16),
            sample=int(payload.get("sample") or 20),
            full=bool(payload.get("full")),
            cache=out / "cache" / "crc64.json",
            ignore=ignore,
        )
        if fixed["failed"]:
            raise RuntimeError(f"{len(fixed['failed'])} 个文件修复失败，可运行 repair --full")
        job.result = {"repair": {
            "linked": fixed["linked"],
            "downloaded": fixed["downloaded"],
            "failed": len(fixed["failed"]),
            "bytes": fixed["bytes"],
            "full": bool(payload.get("full")),
        }}
        job.log(f"修复完成：重建链接 {fixed['linked']}，下载 {fixed['downloaded']}，"
                f"失败 {len(fixed['failed'])}")
        code = 0
    elif action == "verify":
        job.phase = "校验目录"
        key = str(payload.get("key") or "")
        if not key:
            raise ValueError("缺少区服代号")
        manifest_data, _ = backend._manifest_for(key, out, False)
        ignore: set[str] = set()
        suffix = nofade.rel_path()
        if suffix and nofade.state(target) == "patched":
            ignore.add(suffix)
        found = repair.scan(
            target, manifest_data,
            sample=int(payload.get("sample") or 20),
            full=bool(payload.get("full")),
            cache=out / "cache" / "crc64.json",
            ignore=ignore,
        )
        job.result = {"verify": {
            "missing": len(found["missing"]),
            "wrong": len(found["wrong"]),
            "bad": len(found["bad"]),
            "blocked": len(found["blocked"]),
            "checked": found["checked"],
            "full": bool(payload.get("full")),
        }}
        job.log(f"校验完成：缺失 {len(found['missing'])}，大小不符 {len(found['wrong'])}，"
                f"校验不符 {len(found['bad'])}，已核对 {found['checked']}")
        code = 0
    elif action == "manifests":
        job.phase = "重抓清单"
        keys = [str(k) for k in (payload.get("keys") or []) if k]
        if not keys:
            raise ValueError("没有指定区服")
        for key in keys:
            job.log(f"重抓 {key} 的清单")
            backend._manifest_for(key, out, True)
        job.result = {"manifests": keys}
        code = 0
    elif action == "journal-clean":
        job.phase = "清理失效账本"
        dry = bool(payload.get("dry"))
        names: list[str] = []
        for path in journal.available(None):
            try:
                data = journal.load(path)
                dest = Path(data["target"])
            except (OSError, ValueError, KeyError):
                dest = None
            if dest is not None and dest.exists():
                continue
            names.append(path.name)
            if not dry:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    job.log(f"删不掉 {path.name}：{exc}")
        job.result = {"journal_clean": {
            "removed": len(names),
            "names": names[:30],
            "dry": dry,
        }}
        job.log(f"{'将删除' if dry else '已删除'} {len(names)} 条失效账本")
        code = 0
    elif action == "rollback":
        job.phase = "回滚账本"
        path = journal.find(target, payload.get("id"))
        if path is None:
            raise ValueError("这个目录没有可用账本")
        deleted, restored, kept, notes = journal.rollback(path, dry=bool(payload.get("dry")))
        for line in notes:
            job.log(line)
        job.log(f"删除 {deleted}，还原 {restored}，保留 {kept}")
        job.result = {"rollback": {
            "id": path.name,
            "deleted": deleted,
            "restored": restored,
            "kept": kept,
            "notes": notes,
            "dry": bool(payload.get("dry")),
        }}
        code = 0
    elif action == "remove":
        dry = bool(payload.get("dry"))
        job.phase = "预览删除" if dry else "移入回收站"
        code = remove.run(target, base=base, yes=not dry, dry=dry, force=False,
                          trash=True, fallback=False)
        job.result = {"remove": {"code": code, "dry": dry}}
    else:
        raise ValueError(f"未知操作：{action}")

    if code != 0:
        raise RuntimeError(f"操作返回 {code}")
    job.status = "done"
    job.phase = "完成"
    job.message = "已完成"
    job.log("操作完成。")