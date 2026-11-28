"""增量下载器：多线程、断点续传、逐文件校验、连接复用。"""

from __future__ import annotations

import http.client
import os
import threading
import time
import urllib.parse
from pathlib import Path

from . import hashes, native
from .journal import Entry

CHUNK = 1 << 20
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) azp/0.1"


class Stats:
    def __init__(self, total_files: int, total_bytes: int, on_change=None):
        self.lock = threading.Lock()
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.done_files = 0
        self.done_bytes = 0
        self.failed: list[tuple[str, str]] = []
        self.started = time.time()
        self.on_change = on_change

    def add(self, size: int, ok: bool, path: str, msg: str = "") -> None:
        with self.lock:
            self.done_files += 1
            if ok:
                self.done_bytes += size
            else:
                self.failed.append((path, msg))
        if self.on_change is not None:
            try:
                self.on_change(self)
            except Exception:  # noqa: BLE001
                pass

    def report(self) -> str:
        with self.lock:
            elapsed = max(time.time() - self.started, 1e-6)
            rate = self.done_bytes / elapsed / 2**20
            left = self.total_bytes - self.done_bytes
            eta = left / (self.done_bytes / elapsed) / 60 if self.done_bytes else 0
            return (
                f"{self.done_files}/{self.total_files} 个  "
                f"{self.done_bytes / 2**30:.2f}/{self.total_bytes / 2**30:.2f} GiB  "
                f"{rate:.2f} MiB/s  剩余 {eta:.0f} 分钟  失败 {len(self.failed)}"
            )


def md5_of(path: Path) -> str:
    return hashes.md5_of(path)


def _match(path: Path, expect: str, kind: str) -> bool:
    """日服的 CRC64 走 7za，Python 逐字节算不动 30 GiB。"""
    if kind == hashes.CRC64:
        got = native.crc64_file(path)
        if got is not None:
            return str(got) == expect
    return hashes.digest(path, kind) == expect


class Connections:
    """每个线程复用自己的 HTTPS 连接，省掉重复握手。"""

    def __init__(self) -> None:
        self._local = threading.local()

    def get(self, url: str) -> tuple[http.client.HTTPSConnection, str]:
        parts = urllib.parse.urlsplit(url)
        key = (parts.scheme, parts.netloc)
        cache = getattr(self._local, "cache", None)
        if cache is None:
            cache = self._local.cache = {}
        conn = cache.get(key)
        if conn is None:
            if parts.scheme != "https":
                raise ValueError(f"只支持 https：{url}")
            conn = http.client.HTTPSConnection(parts.netloc, timeout=60)
            cache[key] = conn
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        return conn, path

    def drop(self, url: str) -> None:
        parts = urllib.parse.urlsplit(url)
        cache = getattr(self._local, "cache", {}) or {}
        conn = cache.pop((parts.scheme, parts.netloc), None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def download_one(pool: Connections, url: str, dest: Path, size: int, expect: str,
                 kind: str = hashes.MD5, retries: int = 4) -> tuple[bool, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        try:
            if dest.stat().st_size == size and _match(dest, expect, kind):
                return True, "skip"
        except OSError:
            pass
    if size == 0:
        # 空文件没有分片可续，直接请求一次，确认服务端确实提供这份资源
        headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
        try:
            conn, path = pool.get(url)
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status not in (200, 206):
                return False, f"HTTP {resp.status}"
            if body:
                return False, f"服务端返回 {len(body)} 字节，清单写的是 0"
            if hashes.digest_bytes(b"", kind) != expect:
                return False, f"{hashes.label(kind)} 不符"
            dest.write_bytes(b"")
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            pool.drop(url)
            return False, f"{type(exc).__name__}: {exc}"

    part = dest.with_suffix(dest.suffix + ".part")
    last = ""

    for attempt in range(retries):
        offset = part.stat().st_size if part.exists() else 0
        if offset > size:
            try:
                part.unlink()
            except OSError:
                pass
            offset = 0
        if part.exists() and offset == size:
            if _match(part, expect, kind):
                os.replace(part, dest)
                return True, "ok"
            try:
                part.unlink()
            except OSError:
                pass
            offset = 0

        headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            conn, path = pool.get(url)
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            if resp.status not in (200, 206):
                body = resp.read(200)
                if resp.status == 416:
                    part.unlink(missing_ok=True)
                    last = "HTTP 416（分片越界，已重置）"
                    continue
                raise RuntimeError(f"HTTP {resp.status} {body[:80]!r}")
            mode = "ab" if (offset and resp.status == 206) else "wb"
            if mode == "wb":
                offset = 0
            with part.open(mode) as fh:
                while True:
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    fh.write(block)
            if part.stat().st_size != size:
                last = f"大小不符 {part.stat().st_size}/{size}"
                continue
            if not _match(part, expect, kind):
                last = f"{hashes.label(kind)} 不符"
                part.unlink(missing_ok=True)
                pool.drop(url)
                continue
            os.replace(part, dest)
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            pool.drop(url)
            time.sleep(1.5 * (attempt + 1))

    return False, last


def run(rows: list[dict], target: Path, workers: int = 16,
        journal: Entry | None = None, kind: str | None = None,
        on_change=None) -> Stats:
    pending = [r for r in rows if r["action"] == "download"]
    if kind is None:
        kind = pending[0]["hash_kind"] if pending else hashes.MD5
    stats = Stats(len(pending), sum(r["size"] for r in pending), on_change=on_change)
    if not pending:
        print("  没有需要下载的文件")
        return stats
    # 边下边写账本：Ctrl+C 之后 rollback 还能找到这批文件
    save_every = max(200, stats.total_files // 20)

    lock = threading.Lock()
    queue = list(pending)
    pool = Connections()
    stop = threading.Event()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            while not stop.is_set():
                with lock:
                    if not queue:
                        return
                    row = queue.pop()
                dest = target / row["path"]
                try:
                    ok, msg = download_one(pool, row["src_or_url"], dest, row["size"],
                                           row["md5"], kind)
                except Exception as exc:  # noqa: BLE001
                    ok, msg = False, f"{type(exc).__name__}: {exc}"
                stats.add(row["size"], ok, row["path"], msg)
                if ok and msg == "ok" and journal is not None:
                    with lock:
                        journal.add_created(dest, row["size"])
                        hit = len(journal.created) % save_every == 0
                    if hit:
                        journal.save()
                if not ok:
                    print(f"  ! 失败 {row['path']}: {msg}")
        except BaseException as exc:  # Ctrl+C 只会落到主线程，这里是兜底
            with lock:
                errors.append(exc)
            stop.set()

    def ticker() -> None:
        while not stop.wait(5):
            print(f"  {stats.report()}")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    tick = threading.Thread(target=ticker, daemon=True)
    try:
        tick.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if journal is not None and journal.created:
            journal.save()
    finally:
        stop.set()
        for t in threads:
            if t.is_alive():
                t.join()
        if tick.is_alive():
            tick.join()
        if journal is not None and journal.created:
            journal.save()
    if errors:
        raise errors[0]
    print(f"  完成：{stats.report()}")
    return stats
