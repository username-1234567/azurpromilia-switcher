"""核心逻辑的离线测试：不碰真实游戏目录，不联网。

跑法：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from azp import (
    channels, cli, crypto, detect, export, fetch, hashes, indexfile, journal,
    link, manifest, native, nofade, plan, preflight, regions, remove, repair,
    update, yostar,
)
from azp.api import Api
from azp.ui import backend, channels_swap, jobs

NO_CONFIG = "_no_such_regions_.json"


class RegionsTest(unittest.TestCase):
    def test_builtin_keys(self):
        keys = {r.key for r in regions.load(NO_CONFIG)}
        self.assertEqual(keys, {"cn", "bili", "global", "jp"})

    def test_resolve_channel(self):
        self.assertEqual(regions.key_for("354", "C000", NO_CONFIG), "cn")
        self.assertEqual(regions.key_for("354", "C001", NO_CONFIG), "bili")
        self.assertEqual(regions.key_for("434", "C500", NO_CONFIG), "global")
        self.assertEqual(regions.key_for("999", "X", NO_CONFIG), "app999")

    def test_jp_ready_without_channels(self):
        jp = regions.find("jp", NO_CONFIG)
        self.assertIsNotNone(jp)
        self.assertTrue(jp.ready)
        self.assertEqual(jp.folder, "AZUPRO_JP")
        self.assertEqual(jp.kind, "yostar")

    def test_local_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "regions.json"
            path.write_text(
                json.dumps(
                    {
                        "regions": {
                            "xx": {
                                "label": "测试服",
                                "app_id": "777",
                                "channel_codes": ["C777"],
                                "domains": ["https://example.invalid"],
                                "folder": "Test_Game",
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                "utf-8",
            )
            self.assertEqual(regions.key_for("777", "C777", path), "xx")
            self.assertEqual(regions.find("xx", path).folder, "Test_Game")
            self.assertTrue(regions.find("xx", path).ready)


class PathMapperTest(unittest.TestCase):
    JP = {
        "path_map": [
            ["AzurPromiliaJP_Data/", "AzurPromilia_Data/"],
            ["AzurPromiliaJP.exe", "AzurPromilia.exe"],
            ["AzurPromiliaJPBase.dll", "AzurPromiliaBase.dll"],
        ]
    }

    def test_target_side(self):
        fn, _ = plan.path_mapper(self.JP)
        self.assertEqual(fn("AzurPromiliaJP_Data/a.bin"), "AzurPromilia_Data/a.bin")
        self.assertEqual(fn("AzurPromiliaJP.exe"), "AzurPromilia.exe")
        self.assertEqual(fn("other/b.bin"), "other/b.bin")

    def test_jp_as_base_is_inverted(self):
        fn, _ = plan.path_mapper({}, self.JP)
        self.assertEqual(fn("AzurPromilia_Data/a.bin"), "AzurPromiliaJP_Data/a.bin")
        self.assertEqual(fn("AzurPromilia.exe"), "AzurPromiliaJP.exe")


class JournalTest(unittest.TestCase):
    def test_save_reuses_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = journal.JOURNAL_DIR
            journal.JOURNAL_DIR = Path(tmp) / "journal"
            try:
                entry = journal.Entry("test", Path(tmp) / "target")
                entry.add_created(Path(tmp) / "target" / "a.bin", 1)
                first = entry.save()
                entry.add_created(Path(tmp) / "target" / "b.bin", 2)
                second = entry.save()
                self.assertEqual(first, second)
                data = json.loads(second.read_text("utf-8"))
                self.assertEqual([Path(x["path"]).name for x in data["created"]],
                                 ["a.bin", "b.bin"])
                self.assertEqual(list(Path(tmp).glob("journal/*.tmp")), [])
            finally:
                journal.JOURNAL_DIR = old

    def test_empty_entry_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = journal.JOURNAL_DIR
            journal.JOURNAL_DIR = Path(tmp) / "journal"
            try:
                self.assertIsNone(journal.Entry("test", Path(tmp)).save())
            finally:
                journal.JOURNAL_DIR = old


class CrossKindPlanTest(unittest.TestCase):
    def test_jp_base_to_md5_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "AzurPromiliaJP_Data"
            data.mkdir()
            src_file = data / "a.bin"
            src_file.write_bytes(b"abc")
            crc = hashes.digest(src_file, hashes.CRC64)
            base = {
                "label": "日服",
                "hash_kind": "crc64",
                "path_map": [["AzurPromiliaJP_Data/", "AzurPromilia_Data/"]],
                "files": {"AzurPromiliaJP_Data/a.bin": {
                    "size": 3, "md5": str(crc), "url": "a",
                }},
            }
            target = {
                "label": "台服",
                "hash_kind": "md5",
                "cdn_host": "https://example.invalid",
                "files": {"AzurPromilia_Data/a.bin": {
                    "size": 3,
                    "md5": "900150983cd24fb0d6963f7d28e17f72",
                    "url": "a",
                }},
            }
            with contextlib.redirect_stdout(io.StringIO()):
                rows, stats = plan.build(base, target, base_dir=root)
            self.assertEqual(rows[0]["action"], "link")
            self.assertEqual(rows[0]["src_or_url"], "AzurPromiliaJP_Data/a.bin")
            self.assertEqual(stats.link_files, 1)
            self.assertEqual(stats.download_files, 0)


class HashesTest(unittest.TestCase):
    def test_kind_of(self):
        self.assertEqual(hashes.kind_of({"hash_kind": "crc64"}), hashes.CRC64)
        self.assertEqual(hashes.kind_of({}), hashes.MD5)

    def test_md5_of_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(b"abc")
            self.assertEqual(hashes.md5_of(path), "900150983cd24fb0d6963f7d28e17f72")


class YostarTest(unittest.TestCase):
    def test_vc_stable_and_order_sensitive(self):
        head = {"name": "AZUPRO_JP", "version": "1.0.0", "basis": "x"}
        self.assertEqual(yostar._vc(head), yostar._vc(dict(head)))
        self.assertNotEqual(yostar._vc(head), yostar._vc({**head, "version": "1.0.1"}))
        self.assertNotEqual(
            yostar._vc({"a": "1", "b": "2"}),
            yostar._vc({"b": "2", "a": "1"}),
        )


class JobFeedbackTest(unittest.TestCase):
    def test_plan_network_failure_is_visible(self):
        job = jobs.Job()
        with mock.patch.object(jobs.backend, "plan_preview",
                               side_effect=TimeoutError("清单连接超时")):
            jobs._run(job, {"action": "plan"})
        result = job.snapshot()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["phase"], "失败")
        self.assertIn("清单连接超时", result["message"])
        self.assertIn("清单连接超时", result["error"])
        self.assertTrue(any("失败：清单连接超时" in line for line in result["logs"]))


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.old_state = update.status()
        self.old_auto = update._auto_started
        update._state = {"status": "idle", "current": update.__version__}
        update._auto_started = False

    def tearDown(self):
        update._state = self.old_state
        update._auto_started = self.old_auto

    def test_version_comparison(self):
        self.assertGreater(update._version("v0.1.10"), update._version("0.1.2"))
        self.assertEqual(update._version("v0.1.0"), update._version("0.1.0"))
        with self.assertRaises(ValueError):
            update._version("not-a-version")

    def test_update_available_updates_shared_status(self):
        with mock.patch.object(update, "_latest_version", return_value="v9.2.0"):
            result = update.check()
        self.assertEqual(result["status"], "update")
        self.assertEqual(result["latest"], "v9.2.0")
        self.assertEqual(result["url"], update.RELEASE_URL)
        self.assertEqual(update.status(), result)

    def test_current_version(self):
        with mock.patch.object(update, "_latest_version", return_value=update.__version__):
            result = update.check(manual=True)
        self.assertEqual(result["status"], "current")

    def test_auto_failure_is_silent(self):
        with mock.patch.object(update, "_latest_version", side_effect=OSError("offline")):
            result = update.check(manual=False)
        self.assertEqual(result["status"], "idle")
        self.assertNotIn("error", result)
        self.assertEqual(update.status()["status"], "idle")

    def test_manual_failure_is_actionable(self):
        with mock.patch.object(update, "_latest_version", side_effect=OSError("offline")):
            result = update.check(manual=True)
        self.assertEqual(result["status"], "error")
        self.assertIn("稍后重试", result["error"])

    def test_auto_starts_only_once(self):
        with mock.patch.object(update.threading, "Thread") as thread:
            update.start_auto()
            update.start_auto()
        thread.assert_called_once()
        thread.return_value.start.assert_called_once_with()


class DetectTest(unittest.TestCase):
    def _make_install(self, tmp: Path) -> Path:
        version = tmp / "1.0.0"
        version.mkdir()
        (version / "launcher.exe").write_bytes(b"")
        game = tmp / "AzurPromilia_Game"
        game.mkdir()
        (game / "AzurPromilia_Data").mkdir()
        return version

    def test_from_version_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            version = self._make_install(root)
            install = detect.from_path(version)
            self.assertEqual(install.root, root)
            self.assertEqual(install.kind, "manjuu")
            self.assertEqual([g.name for g in install.game_dirs], ["AzurPromilia_Game"])

    def test_from_install_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_install(root)
            install = detect.from_path(root)
            self.assertEqual(install.launcher_exe, root / "1.0.0" / "launcher.exe")
            self.assertEqual(install.kind, "manjuu")

    def test_from_game_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_install(root)
            install = detect.from_path(root / "AzurPromilia_Game")
            self.assertEqual(install.root, root)
            self.assertEqual([g.name for g in install.game_dirs], ["AzurPromilia_Game"])

    def test_yostar_by_exe_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            version = root / "1.0.0"
            version.mkdir()
            exe = version / "AZUPRO_JP_Gamelauncher.exe"
            exe.write_bytes(b"")
            install = detect.from_path(exe)
            self.assertEqual(install.kind, "yostar")
            self.assertEqual(install.root, root)

    def test_missing_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            detect.from_path("C:/definitely/not/here/launcher.exe")

    def test_scan_finds_yostar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            version = root / "2.0.0"
            version.mkdir()
            (version / "AZUPRO_JP_Gamelauncher.exe").write_bytes(b"")
            game = root / "AZUPRO_JP"
            game.mkdir()
            (game / "AzurPromiliaJP_Data").mkdir()
            installs = detect._installs_in(root)
            self.assertEqual(len(installs), 1)
            self.assertEqual(installs[0].kind, "yostar")
            self.assertEqual([g.name for g in installs[0].game_dirs], ["AZUPRO_JP"])

    def test_scan_finds_yostar_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "azurpromilia-jp"
            root.mkdir()
            (root / "AZUPRO_JP_Gamelauncher.exe").write_bytes(b"")
            installs = detect._installs_in(root)
            self.assertEqual(len(installs), 1)
            self.assertEqual(installs[0].kind, "yostar")

    def test_drive_root_rejected(self):
        with self.assertRaises(ValueError):
            detect.from_path("C:/")


class NofadeTest(unittest.TestCase):
    """补丁用临时文件自造：单测不该依赖 patches/ 下 4.7 MB 的游戏资源。

    真实补丁是版权资源，未必随仓库分发；没有它时这些测试也必须能跑。
    """

    ORIGINAL = b"\x00" * 64
    PATCHED = b"\x01" * 64

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.patch_dir = Path(self._tmp.name) / "patches"
        self.patch_dir.mkdir()
        (self.patch_dir / "test.r").write_bytes(self.PATCHED)
        meta = {
            "name": "测试反虚化",
            "file": ".res/default_package/BundleFiles/test.r",
            "size": len(self.PATCHED),
            "original_md5": hashlib.md5(self.ORIGINAL).hexdigest(),
            "patched_md5": hashlib.md5(self.PATCHED).hexdigest(),
            "resource_version": "test",
        }
        (self.patch_dir / "patch.json").write_text(json.dumps(meta), "utf-8")
        self._old_patch_dir = nofade.PATCH_DIR
        nofade.PATCH_DIR = self.patch_dir

    def tearDown(self):
        nofade.PATCH_DIR = self._old_patch_dir
        self._tmp.cleanup()

    def _client(self, root: Path) -> Path:
        game = root / "AzurPromilia_Game"
        (game / "AzurPromilia_Data").mkdir(parents=True, exist_ok=True)
        return game

    def _place(self, game: Path, content: bytes) -> Path:
        dest = game / "AzurPromilia_Data" / Path(nofade.load().file)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        return dest

    def test_patch_meta(self):
        patch = nofade.load()
        self.assertEqual(patch.filename, "test.r")
        self.assertEqual(patch.size, 64)
        self.assertTrue((nofade.PATCH_DIR / patch.filename).exists())
        self.assertEqual(patch.resource_version, "test")

    def test_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = self._client(Path(tmp))
            self.assertEqual(nofade._data_dir(game), game / "AzurPromilia_Data")
            self.assertEqual(nofade._data_dir(game / "AzurPromilia_Data"),
                             game / "AzurPromilia_Data")
            with self.assertRaises(nofade.NofadeError):
                nofade._data_dir(Path(tmp) / "not-a-client")

    def test_status_patched(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = self._client(Path(tmp))
            self._place(game, self.PATCHED)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(nofade.status(game), 0)
            self.assertIn("已安装", out.getvalue())

    def test_status_original_and_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = self._client(Path(tmp))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(nofade.status(game), 1)  # 文件不存在
            dest = self._place(game, self.ORIGINAL)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(nofade.status(game), 0)
            self.assertIn("原版", out.getvalue())
            dest.write_bytes(b"\x07" * 64)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(nofade.status(game), 1)
            self.assertIn("未知", out.getvalue())

    def test_state_and_rel_path(self):
        patch = nofade.load()
        self.assertEqual(nofade.rel_path(patch), ".res/default_package/BundleFiles/test.r")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = self._client(root)
            self.assertEqual(nofade.state(game), "missing")
            self._place(game, self.ORIGINAL)
            self.assertEqual(nofade.state(game), "original")
            self._place(game, self.PATCHED)
            self.assertEqual(nofade.state(game), "patched")
            self._place(game, b"\x07" * 64)
            self.assertEqual(nofade.state(game), "unknown")
            self.assertEqual(nofade.state(root / "not-a-client"), "unsupported")

    def test_rejects_non_cn_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / "AzurPromilia_TC_game"
            (game / "AzurPromilia_Data").mkdir(parents=True)
            self.assertEqual(nofade.state(game), "unsupported")
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(nofade.NofadeError) as ctx:
                    nofade.install(game, accept_risk=True, yes=True)
            self.assertIn("只支持国服", str(ctx.exception))
            self.assertFalse((game / nofade.BACKUP_NAME).exists())

    def test_install_already_patched(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = self._client(Path(tmp))
            self._place(game, self.PATCHED)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(nofade.install(game, accept_risk=True, yes=True), 0)
            self.assertIn("已经装过", out.getvalue())

    def test_install_requires_risk_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = self._client(Path(tmp))
            self._place(game, self.ORIGINAL)
            with mock.patch.object(nofade.preflight, "running_games", return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(nofade.install(game, yes=True), 1)
            self.assertIn("封号", out.getvalue())

    def test_install_refuses_unknown_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = self._client(Path(tmp))
            self._place(game, b"\x07" * 64)  # 既不是原版也不是补丁版
            with mock.patch.object(nofade.preflight, "running_games", return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(
                        nofade.install(game, accept_risk=True, yes=True, dry=True), 1)
            self.assertIn("不是资源版本", out.getvalue())

    def test_install_refuses_running_game_and_bad_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = self._client(Path(tmp))
            self._place(game, self.ORIGINAL)
            with mock.patch.object(nofade.preflight, "running_games",
                                   return_value=["azurpromilia.exe"]):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(nofade.install(game, accept_risk=True, yes=True), 1)
            self.assertIn("正在运行", out.getvalue())
            bad = self.patch_dir / "bad.r"
            bad.write_bytes(b"\x09" * 64)
            with mock.patch.object(nofade.preflight, "running_games", return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(
                        nofade.install(game, payload=bad, accept_risk=True, yes=True), 1)
            self.assertIn("校验不符", out.getvalue())

    def test_install_dry_run_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = self._client(Path(tmp))
            dest = self._place(game, self.ORIGINAL)
            with mock.patch.object(nofade.preflight, "running_games", return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        nofade.install(game, accept_risk=True, yes=True, dry=True), 0)
            self.assertEqual(dest.read_bytes(), self.ORIGINAL)

    def test_install_and_restore_roundtrip(self):
        patch = nofade.load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = self._client(root)
            dest = self._place(game, self.ORIGINAL)
            # 账本与备份都重定向到临时目录，别往真实 out/ 里写
            with mock.patch.object(journal, "JOURNAL_DIR", root / "journal"), \
                 mock.patch.object(journal, "BACKUP_DIR", root / "backup"), \
                 mock.patch.object(nofade.preflight, "running_games", return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(nofade.install(game, accept_risk=True, yes=True), 0)
                self.assertEqual(dest.read_bytes(), self.PATCHED)
                backup = game / nofade.BACKUP_NAME / patch.filename
                self.assertEqual(backup.read_bytes(), self.ORIGINAL)
                self.assertTrue((backup.parent / nofade.META).exists())
                self.assertTrue(list((root / "journal").glob("*.json")))
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(nofade.restore(game, yes=True), 0)
            self.assertEqual(dest.read_bytes(), self.ORIGINAL)

    def test_restore_without_backup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = self._client(Path(tmp))
            self._place(game, self.PATCHED)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(nofade.restore(game, yes=True), 1)
            self.assertIn("没有备份", out.getvalue())


class YostarSaltTest(unittest.TestCase):
    def setUp(self):
        yostar._salt = None

    def tearDown(self):
        yostar._salt = None

    def test_env_override(self):
        with mock.patch.dict("os.environ", {"AZP_YOSTAR_SALT": "A" * 32}):
            self.assertEqual(yostar.salt(), "A" * 32)
            self.assertIn("sign", yostar.auth_header())

    def test_missing_reports_actionable_error(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AZP_YOSTAR_SALT", None)
            with mock.patch.object(yostar, "_salt_from_launcher", return_value=None):
                with self.assertRaises(yostar.YostarError) as ctx:
                    yostar.salt()
        self.assertIn("启动器", str(ctx.exception))


class PathSafetyTest(unittest.TestCase):
    """清单来自网络、计划表可手工编辑，路径越界必须被挡住。"""

    def test_safe_rel_accepts_normal(self):
        self.assertEqual(plan.safe_rel("a/b.bin"), "a/b.bin")
        self.assertEqual(plan.safe_rel("a\\b.bin"), "a/b.bin")
        self.assertEqual(plan.safe_rel(".res/default_package/x.r"),
                         ".res/default_package/x.r")

    def test_safe_rel_rejects_escape(self):
        for bad in ("../x", "a/../../x", "..", "/abs/x", "C:/x", "C:\\x",
                    "\\\\host\\share\\x", "", "   "):
            with self.assertRaises(ValueError, msg=bad):
                plan.safe_rel(bad)

    def test_read_csv_rejects_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "plan.csv"
            plan.write_csv([{"action": "download", "size": 1, "md5": "x",
                             "path": "../evil.bin", "src_or_url": "u",
                             "hash_kind": "md5"}], csv_path)
            with self.assertRaises(ValueError):
                plan.read_csv(csv_path)

    def test_build_rejects_escape(self):
        target = {"files": {"../evil": {"size": 1, "md5": "x", "url": "u"}}}
        with self.assertRaises(ValueError):
            plan.build({"files": {}}, target)

    def test_csv_roundtrip(self):
        rows = [{"action": "download", "size": 3, "md5": "abc",
                 "path": "a/b.bin", "src_or_url": "https://x/a", "hash_kind": "md5"}]
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "plan.csv"
            plan.write_csv(rows, csv_path)
            self.assertEqual(plan.read_csv(csv_path), rows)
            self.assertEqual(plan.kind_of_rows(rows), hashes.MD5)
            self.assertEqual(plan.kind_of_rows([]), hashes.MD5)


class LinkRunTest(unittest.TestCase):
    ROW = {"action": "link", "size": 3, "md5": "", "path": "a.bin",
           "src_or_url": "a.bin", "hash_kind": "md5"}

    def test_dry_run_then_link_then_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            base.mkdir()
            (base / "a.bin").write_bytes(b"abc")
            target = root / "target"
            with contextlib.redirect_stdout(io.StringIO()):
                dry = link.run([self.ROW], base, target, dry=True)
            self.assertEqual(dry.created, 1)
            self.assertFalse((target / "a.bin").exists())  # 试运行不落盘

            with contextlib.redirect_stdout(io.StringIO()):
                stats = link.run([self.ROW], base, target)
            self.assertEqual(stats.created, 1)
            self.assertEqual(stats.failed, [])
            self.assertEqual((target / "a.bin").read_bytes(), b"abc")
            if os.name == "nt":
                self.assertEqual((target / "a.bin").stat().st_nlink, 2)

            with contextlib.redirect_stdout(io.StringIO()):
                again = link.run([self.ROW], base, target)
            self.assertEqual(again.skipped, 1)  # 重跑幂等
            self.assertEqual(again.failed, [])

    def test_failures_are_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            base.mkdir()
            target = root / "target"
            target.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                missing = link.run([self.ROW], base, target)
            self.assertEqual(len(missing.failed), 1)
            self.assertIn("源文件不存在", missing.failed[0][1])

            (base / "a.bin").write_bytes(b"abcd")  # 大小与计划不符
            with contextlib.redirect_stdout(io.StringIO()):
                mismatch = link.run([self.ROW], base, target)
            self.assertIn("大小与清单不符", mismatch.failed[0][1])

            (base / "a.bin").write_bytes(b"abc")
            (target / "a.bin").write_bytes(b"zz")
            with contextlib.redirect_stdout(io.StringIO()):
                occupied = link.run([self.ROW], base, target)
            self.assertIn("目标已存在且大小不同", occupied.failed[0][1])

    def test_link_local_and_empty_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            local.mkdir()
            (local / "b.bin").write_bytes(b"xyz")
            row = {"action": "link_local", "size": 3, "md5": "", "path": "b.bin",
                   "src_or_url": "b.bin", "hash_kind": "md5"}
            with contextlib.redirect_stdout(io.StringIO()):
                stats = link.run([row], root / "base", root / "target", local_dir=local)
            self.assertEqual(stats.created, 1)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                empty = link.run([dict(row, action="download")], root, root / "t2")
            self.assertEqual(empty.created, 0)
            self.assertIn("没有需要建立链接", out.getvalue())


class IndexfileTest(unittest.TestCase):
    def test_write_and_decrypt_roundtrip(self):
        text = "1,2,3\n0,3,900150983cd24fb0d6963f7d28e17f72,a.bin,u\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "index.csv"
            csv_path.write_text(text, "utf-8")
            game = root / "game"
            entry = journal.Entry("finalize", game)
            written = indexfile.write(game, "100", "1.0.0", csv_path, journal=entry)
            self.assertEqual({p.name for p in written}, {"version", "100", "paths"})
            version = json.loads((game / "_index" / "version").read_text("utf-8"))
            self.assertEqual(version, {"version": "100", "versionName": "1.0.0"})
            blob = (game / "_index" / "100").read_bytes()
            self.assertEqual(manifest.decrypt_index(blob), text)
            self.assertEqual(json.loads((game / "_index" / "paths").read_text("utf-8")), {})
            # 覆盖已有索引前必须留备份
            second = journal.Entry("finalize", game)
            indexfile.write(game, "100", "1.0.0", csv_path, journal=second)
            self.assertEqual(len(second.backups), 2)

    def test_paths_template_is_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "index.csv"
            csv_path.write_text("1,2,3\n", "utf-8")
            base = root / "base"
            (base / "_index").mkdir(parents=True)
            (base / "_index" / "paths").write_text('{"a": 1}', "utf-8")
            game = root / "game"
            indexfile.write(game, "100", "1.0.0", csv_path, paths_from=base)
            self.assertEqual(json.loads((game / "_index" / "paths").read_text("utf-8")),
                             {"a": 1})


class ManifestParseTest(unittest.TestCase):
    def test_type2_files_dirs_and_header(self):
        text = ("100,2,1\n"
                "0,0,0,SomeDir\n"
                "0,3,900150983CD24FB0D6963F7D28E17F72,a/b.bin,http://x/a\n")
        parsed = manifest.parse_index(text, 2)
        self.assertEqual(parsed["dirs"], ["SomeDir"])
        info = parsed["files"]["a/b.bin"]
        self.assertEqual(info["size"], 3)
        self.assertEqual(info["md5"], "900150983cd24fb0d6963f7d28e17f72")
        self.assertEqual(info["url"], "http://x/a")

    def test_type2_skips_malformed_rows(self):
        text = "0,3,md5,a\\b.bin,u\nshort,row\n0,notint,md5,bad,u\n\n"
        parsed = manifest.parse_index(text, 2)
        self.assertIn("a/b.bin", parsed["files"])  # 反斜杠统一成正斜杠
        self.assertNotIn("bad", parsed["files"])   # 大小不是整数就跳过

    def test_type1_slices_are_numbered(self):
        parsed = manifest.parse_index("0,0,3,ABC,u\n0,0,4,DEF,u\n", 1)
        self.assertEqual(list(parsed["files"]), ["slice-000001", "slice-000002"])
        self.assertEqual(parsed["files"]["slice-000002"]["md5"], "def")


class CryptoTest(unittest.TestCase):
    def test_roundtrip_with_padding(self):
        key, iv = b"0" * 16, b"1" * 16
        for data in (b"", b"azp", b"x" * 16, b"y" * 33):
            self.assertEqual(crypto.decrypt(key, iv, crypto.encrypt(key, iv, data)), data)

    def test_rejects_bad_key_and_iv(self):
        with self.assertRaises(ValueError):
            crypto.encrypt(b"short", b"1" * 16, b"x")
        with self.assertRaises(ValueError):
            crypto.encrypt(b"0" * 16, b"1" * 8, b"x")


class JournalRollbackTest(unittest.TestCase):
    def _write_entry(self, root: Path, target: Path, created: list, backups: list) -> Path:
        path = root / "entry.json"
        path.write_text(json.dumps({
            "cmd": "test", "target": str(target), "time": time.time() + 3600,
            "created": created, "backups": backups,
        }), "utf-8")
        return path

    def test_rollback_removes_created_restores_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            new_file = target / "sub" / "new.bin"
            new_file.parent.mkdir()
            new_file.write_bytes(b"new")
            original = target / "index"
            original.write_bytes(b"changed")
            saved = root / "saved-index"
            saved.write_bytes(b"orig")
            path = self._write_entry(
                root, target,
                [{"path": str(new_file), "size": 3}],
                [{"orig": str(original), "saved": str(saved)}],
            )
            removed, restored, skipped, notes = journal.rollback(path)
            self.assertEqual((removed, restored, skipped), (1, 1, 0))
            self.assertEqual(notes, [])
            self.assertFalse(new_file.exists())
            self.assertFalse(new_file.parent.exists())  # 空目录顺手收掉
            self.assertEqual(original.read_bytes(), b"orig")

    def test_rollback_keeps_changed_and_outside_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            changed = target / "changed.bin"
            changed.write_bytes(b"longer-than-before")
            outside = root / "outside.bin"
            outside.write_bytes(b"x")
            path = self._write_entry(root, target, [
                {"path": str(changed), "size": 3},
                {"path": str(outside), "size": 1},
                {"path": str(target / "gone.bin"), "size": 1},
            ], [])
            removed, restored, skipped, notes = journal.rollback(path)
            self.assertEqual(removed, 0)
            self.assertEqual(skipped, 2)
            self.assertTrue(changed.exists())
            self.assertTrue(outside.exists())
            self.assertTrue(any("不在目标目录内" in n for n in notes))
            self.assertTrue(any("大小已变" in n for n in notes))

    def test_rollback_dry_run_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            new_file = target / "new.bin"
            new_file.write_bytes(b"new")
            path = self._write_entry(root, target,
                                     [{"path": str(new_file), "size": 3}],
                                     [{"orig": str(target / "x"), "saved": str(root / "none")}])
            removed, restored, skipped, notes = journal.rollback(path, dry=True)
            self.assertEqual(removed, 1)
            self.assertTrue(new_file.exists())
            self.assertTrue(any("备份缺失" in n for n in notes))

    def test_available_filters_by_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(journal, "JOURNAL_DIR", root):
                target = root / "t"
                entry = journal.Entry("link", target)
                entry.add_created(root / "a.bin", 1)
                saved = entry.save()
                self.assertIsNotNone(saved)
                (root / "broken.json").write_text("{not json", "utf-8")
                self.assertEqual(journal.available(target), [saved])
                self.assertEqual(journal.available(root / "other"), [])
                self.assertEqual(journal.find(target, None), saved)
                self.assertEqual(journal.find(None, saved.name), saved)
                self.assertIsNone(journal.find(None, "nope.json"))


class NativeBatchTest(unittest.TestCase):
    def test_crc64_file_zero_is_a_valid_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(b"")
            # 修复前 `table.get(...) or table.get(...)` 会把合法的 0 当成没算到
            with mock.patch.object(native, "crc64_dir", return_value={"a.bin": 0}):
                self.assertEqual(native.crc64_file(path, exe=Path("7za")), 0)

    def test_crc64_file_without_7za_returns_none(self):
        with mock.patch.object(native, "find", return_value=None):
            self.assertIsNone(native.crc64_file(Path("a.bin")))

    def test_large_group_is_batched_not_recursive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rels = [f"d/f{i:03d}.bin" for i in range(450)]
            for rel in rels:
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x")
            calls: list[list[str]] = []

            def fake_dir(exe, folder, names=None):
                self.assertIsNotNone(names)  # 不允许退化成整目录递归
                calls.append(list(names))
                return {n.lower(): 7 for n in names}

            with mock.patch.object(native, "crc64_dir", side_effect=fake_dir):
                out = native.crc64_for(root, rels, exe=Path("7za"))
            self.assertEqual(len(out), 450)
            self.assertEqual(set(out.values()), {7})
            self.assertEqual(sum(len(c) for c in calls), 450)
            self.assertTrue(all(len(c) <= 200 for c in calls))

    def test_cache_hit_skips_hashing_and_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.bin").write_bytes(b"abc")
            cache = root / "cache.json"
            with mock.patch.object(native, "crc64_dir",
                                   return_value={"a.bin": 123}) as fake:
                first = native.crc64_for(root, ["a.bin", "gone.bin"], cache, exe=Path("7za"))
            self.assertEqual(first, {"a.bin": 123})
            self.assertTrue(cache.exists())
            with mock.patch.object(native, "crc64_dir") as fake:
                second = native.crc64_for(root, ["a.bin"], cache, exe=Path("7za"))
            self.assertEqual(second, {"a.bin": 123})
            fake.assert_not_called()  # 大小和 mtime 没变就不该重算

    def test_missing_7za_is_actionable(self):
        with mock.patch.object(native, "find", return_value=None):
            with self.assertRaises(native.NativeError) as ctx:
                native.crc64_for(Path("."), ["a.bin"])
            self.assertIn("AZP_7ZA", str(ctx.exception))

    def test_crc64_dir_parses_output(self):
        exe = Path("7za")
        output = b"\r\n".join([
            b"7-Zip (a) 24.01", b"",
            b"0123456789ABCDEF 3 a.bin",
            b"FFFFFFFFFFFFFFFF 10 sub dir/b.bin",
            b"not a hash line",
        ]) + b"\r\n"
        proc = mock.Mock(stdout=output, returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with mock.patch.object(native.subprocess, "run", return_value=proc) as run:
                table = native.crc64_dir(exe, folder, ["a.bin"])
            self.assertEqual(table["a.bin"], 0x0123456789ABCDEF)
            self.assertEqual(table["sub dir/b.bin"], 0xFFFFFFFFFFFFFFFF)
            self.assertEqual(run.call_args.kwargs["cwd"], str(folder))
            self.assertEqual(native.crc64_dir(exe, folder / "nope"), {})

    def test_crc64_dir_failure_raises_native_error(self):
        proc = mock.Mock(stdout=b"ERROR: boom\n", returncode=2)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(native.subprocess, "run", return_value=proc):
                with self.assertRaises(native.NativeError) as ctx:
                    native.crc64_dir(Path("7za"), Path(tmp), ["a.bin"])
            self.assertIn("boom", str(ctx.exception))


class RepairScanTest(unittest.TestCase):
    def _manifest(self, root: Path) -> dict:
        good = hashes.crc64_of(root / "a.bin")
        return {"hash_kind": "crc64", "files": {
            "a.bin": {"size": 3, "md5": good},
            "b.bin": {"size": 3, "md5": good},
        }}

    def test_crc64_not_in_table_falls_back_to_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.bin").write_bytes(b"abc")
            man = self._manifest(root)
            for table in ({}, {"other": 1}):
                with mock.patch.object(native, "crc64_for", return_value=table):
                    found = repair.scan(root, man, full=True)
                # 修复前 table.get(path, -2) 会把没核对的好文件判成坏文件
                self.assertEqual(found["bad"], [])
                self.assertEqual(found["missing"], ["b.bin"])
                self.assertEqual(found["checked"], 2)

    def test_crc64_mismatch_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.bin").write_bytes(b"abc")
            (root / "b.bin").write_bytes(b"abc")
            man = self._manifest(root)
            with mock.patch.object(native, "crc64_for", return_value={"a.bin": 999, "b.bin": 1}):
                found = repair.scan(root, man, full=True)
            self.assertEqual(sorted(found["bad"]), ["a.bin", "b.bin"])

    def test_md5_size_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.bin").write_bytes(b"abc")
            (root / "wrong.bin").write_bytes(b"zz")
            man = {"files": {
                "a.bin": {"size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72"},
                "wrong.bin": {"size": 3, "md5": "x"},
                "gone.bin": {"size": 1, "md5": "x"},
            }}
            found = repair.scan(root, man, full=True)
            self.assertEqual(found["missing"], ["gone.bin"])
            self.assertEqual(found["wrong"], ["wrong.bin"])
            self.assertEqual(found["bad"], [])
            # 不抽样就只看大小，不看内容
            quiet = repair.scan(root, man)
            self.assertEqual(quiet["checked"], 0)

    def test_md5_bad_content_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.bin").write_bytes(b"xyz")
            man = {"files": {"a.bin": {"size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72"}}}
            self.assertEqual(repair.scan(root, man, sample=5)["bad"], ["a.bin"])

    def test_repair_dry_run_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            man = {"files": {"a.bin": {"size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72",
                                       "url": "a"}}}
            with contextlib.redirect_stdout(io.StringIO()) as out:
                stats = repair.run(target, man, dry=True)
            self.assertEqual(stats["bytes"], 3)
            self.assertFalse((target / "a.bin").exists())
            self.assertIn("需要下载 1 个", out.getvalue())

    def test_repair_relinks_from_base_instead_of_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            base.mkdir()
            (base / "a.bin").write_bytes(b"abc")
            target = root / "target"
            target.mkdir()
            man = {"files": {"a.bin": {"size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72",
                                       "url": "a"}}}
            with mock.patch.object(fetch, "download_one") as dl:
                with contextlib.redirect_stdout(io.StringIO()):
                    stats = repair.run(target, man, base=base)
            self.assertEqual(stats["linked"], 1)
            self.assertEqual(stats["failed"], [])
            dl.assert_not_called()  # 源区有同内容文件就不该走下载
            self.assertEqual((target / "a.bin").read_bytes(), b"abc")

    def test_repair_nothing_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.bin").write_bytes(b"abc")
            man = {"files": {"a.bin": {"size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72"}}}
            with contextlib.redirect_stdout(io.StringIO()) as out:
                stats = repair.run(root, man)
            self.assertEqual(stats["linked"], 0)
            self.assertIn("没有需要修的", out.getvalue())


    def test_scan_ignores_patch_suffix(self):
        rel = "AzurPromilia_Data/.res/default_package/BundleFiles/p.r"
        suffix = ".res/default_package/BundleFiles/p.r"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / rel).parent.mkdir(parents=True)
            (root / rel).write_bytes(b"xyz")
            man = {"files": {rel: {"size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72"}}}
            self.assertEqual(repair.scan(root, man, sample=5)["bad"], [rel])
            found = repair.scan(root, man, sample=5, ignore={suffix})
            self.assertEqual(found["bad"], [])
            self.assertEqual(found["checked"], 0)


class _FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes):
        super().__init__(body)
        self.status = status


class _FakePool:
    """模拟 fetch.Connections，只实现 download_one 用到的接口。"""

    def __init__(self, payload: bytes, support_range: bool = True):
        self.payload = payload
        self.support_range = support_range
        self.ranges: list[str | None] = []
        self.dropped = 0

    def get(self, url):
        return self, url

    def request(self, method, path, headers):
        self.ranges.append(headers.get("Range"))

    def getresponse(self):
        rng = self.ranges[-1]
        if rng and self.support_range:
            start = int(rng.split("=", 1)[1].rstrip("-"))
            return _FakeResponse(206, self.payload[start:])
        return _FakeResponse(200, self.payload)

    def drop(self, url):
        self.dropped += 1


class FetchDownloadTest(unittest.TestCase):
    PAYLOAD = b"0123456789abcdef"
    MD5 = hashlib.md5(PAYLOAD).hexdigest()

    def test_download_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "sub" / "a.bin"
            pool = _FakePool(self.PAYLOAD)
            ok, msg = fetch.download_one(pool, "https://x/a", dest, len(self.PAYLOAD), self.MD5)
            self.assertEqual((ok, msg), (True, "ok"))
            self.assertEqual(dest.read_bytes(), self.PAYLOAD)
            self.assertFalse(dest.with_suffix(".bin.part").exists())

    def test_existing_valid_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            dest.write_bytes(self.PAYLOAD)
            pool = _FakePool(self.PAYLOAD)
            ok, msg = fetch.download_one(pool, "https://x/a", dest, len(self.PAYLOAD), self.MD5)
            self.assertEqual((ok, msg), (True, "skip"))
            self.assertEqual(pool.ranges, [])  # 一个请求都不该发

    def test_resume_sends_range_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            dest.with_suffix(".bin.part").write_bytes(self.PAYLOAD[:4])
            pool = _FakePool(self.PAYLOAD)
            ok, _ = fetch.download_one(pool, "https://x/a", dest, len(self.PAYLOAD), self.MD5)
            self.assertTrue(ok)
            self.assertEqual(pool.ranges, ["bytes=4-"])
            self.assertEqual(dest.read_bytes(), self.PAYLOAD)

    def test_server_ignoring_range_restarts_from_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            dest.with_suffix(".bin.part").write_bytes(self.PAYLOAD[:4])
            pool = _FakePool(self.PAYLOAD, support_range=False)
            ok, _ = fetch.download_one(pool, "https://x/a", dest, len(self.PAYLOAD), self.MD5)
            self.assertTrue(ok)
            self.assertEqual(dest.read_bytes(), self.PAYLOAD)

    def test_hash_mismatch_fails_and_removes_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            pool = _FakePool(self.PAYLOAD)
            with mock.patch.object(fetch.time, "sleep"):
                ok, msg = fetch.download_one(pool, "https://x/a", dest, len(self.PAYLOAD),
                                             "0" * 32, retries=2)
            self.assertFalse(ok)
            self.assertIn("不符", msg)
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_suffix(".bin.part").exists())
            self.assertEqual(pool.dropped, 2)

    def test_wrong_size_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            pool = _FakePool(self.PAYLOAD)
            with mock.patch.object(fetch.time, "sleep"):
                ok, msg = fetch.download_one(pool, "https://x/a", dest, 99, self.MD5, retries=1)
            self.assertFalse(ok)
            self.assertIn("大小不符", msg)

    def test_run_without_downloads(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            stats = fetch.run([{"action": "link"}], Path("."))
        self.assertEqual(stats.total_files, 0)
        self.assertIn("没有需要下载", out.getvalue())
        self.assertIn("0/0", stats.report())

    def test_run_downloads_and_writes_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            entry = journal.Entry("fetch", target)
            row = {"action": "download", "size": len(self.PAYLOAD), "md5": self.MD5,
                   "path": "a.bin", "src_or_url": "https://x/a", "hash_kind": "md5"}
            with mock.patch.object(fetch, "Connections", return_value=_FakePool(self.PAYLOAD)):
                with contextlib.redirect_stdout(io.StringIO()):
                    stats = fetch.run([row], target, workers=2, journal=entry)
            self.assertEqual(stats.done_files, 1)
            self.assertEqual(stats.failed, [])
            self.assertEqual((target / "a.bin").read_bytes(), self.PAYLOAD)
            self.assertEqual(len(entry.created), 1)


class RemoveSafetyTest(unittest.TestCase):
    def _client(self, root: Path, name: str) -> Path:
        game = root / name
        (game / "AzurPromilia_Data").mkdir(parents=True)
        return game

    def test_check_target_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self._client(root, "AzurPromilia_Game")
            self.assertIn("不存在", remove.check_target(root / "nope", None, False))
            self.assertIn("源区目录，不能删", remove.check_target(base, base, False))
            inner = base / "AzurPromilia_Data"
            self.assertIn("会破坏源区", remove.check_target(inner, base, True))
            other = self._client(root / "other", "AzurPromilia_TC_game")
            self.assertEqual(remove.check_target(other, base, False), "")
            plain = root / "plain"
            plain.mkdir()
            self.assertIn("不像客户端目录", remove.check_target(plain, None, False))
            self.assertEqual(remove.check_target(plain, None, True), "")

    def test_scan_separates_shared_from_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self._client(root, "AzurPromilia_Game")
            (base / "shared.bin").write_bytes(b"abc")
            target = self._client(root / "t", "AzurPromilia_TC_game")
            os.link(base / "shared.bin", target / "shared.bin")
            (target / "own.bin").write_bytes(b"zz")
            stats = remove.scan(target, base)
            self.assertEqual(stats.files, 2)
            self.assertEqual((stats.base_files, stats.base_bytes), (1, 3))
            self.assertEqual((stats.own_files, stats.own_bytes), (1, 2))
            self.assertEqual(stats.other_files, 0)
            # 不给 base 时，有硬链接的文件只能算「与别处共用」
            blind = remove.scan(target)
            self.assertEqual(blind.other_files, 1)

    def test_dry_run_and_cancel_keep_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._client(root, "AzurPromilia_TC_game")
            (target / "a.bin").write_bytes(b"abc")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(remove.run(target, dry=True), 0)
            self.assertIn("试运行", out.getvalue())
            self.assertTrue((target / "a.bin").exists())
            with mock.patch.object(remove, "_confirm", return_value=False):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(remove.run(target), 1)
            self.assertTrue((target / "a.bin").exists())

    def test_actual_delete_only_removes_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self._client(root, "AzurPromilia_Game")
            (base / "shared.bin").write_bytes(b"abc")
            target = self._client(root / "t", "AzurPromilia_TC_game")
            os.link(base / "shared.bin", target / "shared.bin")
            (target / "own.bin").write_bytes(b"zz")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(remove.run(target, base=base, yes=True), 0)
            self.assertFalse(target.exists())
            self.assertEqual((base / "shared.bin").read_bytes(), b"abc")  # 源区不动
            self.assertIn("已删除 2 个文件", out.getvalue())

    def test_non_interactive_needs_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._client(Path(tmp), "AzurPromilia_TC_game")
            with mock.patch.object(remove.sys.stdin, "isatty", return_value=False):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(remove.run(target), 1)
            self.assertIn("--yes", out.getvalue())


class ExportTest(unittest.TestCase):
    ROW = {"action": "download", "size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72",
           "path": "a/b.bin", "src_or_url": "u", "hash_kind": "md5"}

    def test_incremental_export_writes_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            (source / "a").mkdir(parents=True)
            (source / "a" / "b.bin").write_bytes(b"abc")
            dest = root / "dest"
            stats = export.run([self.ROW], source, dest)
            self.assertEqual(stats["copied"], 1)
            self.assertEqual(stats["missing"], [])
            self.assertEqual((dest / "a" / "b.bin").read_bytes(), b"abc")
            with (dest / "_handoff.csv").open(encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            self.assertEqual(lines[0], "path,size,md5")
            self.assertIn("a/b.bin,3,900150983cd24fb0d6963f7d28e17f72", lines[1])
            # 再跑一次：大小一致就跳过，仍然计入 copied
            again = export.run([self.ROW], source, dest)
            self.assertEqual(again["copied"], 1)

    def test_incremental_dry_run_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            source.mkdir()
            dest = root / "dest"
            with contextlib.redirect_stdout(io.StringIO()) as out:
                stats = export.run([self.ROW], source, dest, dry=True)
            self.assertEqual(stats["files"], 1)
            self.assertFalse(dest.exists())
            self.assertIn("试运行", out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                missing = export.run([self.ROW], source, dest)
            self.assertEqual(missing["missing"], ["a/b.bin"])
            self.assertEqual(missing["copied"], 0)

    def test_export_rejects_nested_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            source.mkdir()
            with self.assertRaises(ValueError):
                export.run([], source, source / "inside")
            with self.assertRaises(ValueError):
                export.run_full(source, source)

    def test_full_export_copy_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            (source / "_index").mkdir(parents=True)
            (source / "a.bin").write_bytes(b"abc")
            (source / "_index" / "version").write_text("{}", "utf-8")
            dest = root / "dest"
            with contextlib.redirect_stdout(io.StringIO()) as out:
                dry = export.run_full(source, dest, dry=True)
            self.assertEqual(dry["files"], 2)
            self.assertEqual(dry["bytes"], 5)
            self.assertFalse(dest.exists())
            self.assertIn("复制", out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                stats = export.run_full(source, dest)
            self.assertEqual(stats["copied"], 2)
            self.assertEqual((dest / "_index" / "version").read_text("utf-8"), "{}")
            with self.assertRaises(FileNotFoundError):
                export.run_full(root / "nope", root / "x")

    def test_full_export_hardlink(self):
        if os.name != "nt":
            self.skipTest("硬链接计数只在 NTFS 上断言")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            source.mkdir()
            (source / "a.bin").write_bytes(b"abc")
            dest = root / "dest"
            with contextlib.redirect_stdout(io.StringIO()):
                stats = export.run_full(source, dest, hardlink=True)
            self.assertEqual(stats["fell_back"], 0)
            self.assertEqual((dest / "a.bin").stat().st_nlink, 2)


class PreflightTest(unittest.TestCase):
    def test_run_on_real_temp_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            base.mkdir()
            target = root / "not-created" / "target"
            manifest_data = {"files": {"a.bin": {"size": 3}}}
            ok, items = preflight.run(base, target, need_bytes=1, manifest=manifest_data)
            names = {i.name: i for i in items}
            self.assertEqual(names["Python"].status, preflight.OK)
            self.assertIn(names["AES 后端"].status, (preflight.OK, preflight.FAIL))
            self.assertIn("目标目录硬链接", names)
            self.assertIn("同卷", names)
            self.assertIn("长路径", names)
            if os.name == "nt":
                self.assertEqual(names["源目录硬链接"].status, preflight.OK)
                self.assertEqual(names["同卷"].status, preflight.OK)
            text = preflight.render(items)
            self.assertIn("[ok]", text)
            self.assertEqual(preflight.report(items),
                             f"{sum(1 for i in items if i.status == preflight.FAIL)} 项致命，"
                             f"{sum(1 for i in items if i.status == preflight.WARN)} 项警告")
            self.assertIsInstance(ok, bool)

    def test_pending_bytes_ignores_existing_files(self):
        rows = [
            {"action": "download", "size": 3, "path": "have.bin"},
            {"action": "download", "size": 5, "path": "want.bin"},
            {"action": "link", "size": 100, "path": "link.bin"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "have.bin").write_bytes(b"abc")
            self.assertEqual(preflight.pending_bytes(rows, root), 5)

    def test_long_path_warning(self):
        long_name = "d/" + "x" * 300
        with mock.patch.object(preflight, "long_paths_enabled", return_value=False):
            item = preflight._long_path_item(Path("C:/game"), {"files": {long_name: {}}})
        self.assertEqual(item.status, preflight.WARN)
        self.assertIn("超过 255", item.detail)
        short = preflight._long_path_item(Path("C:/g"), {"files": {"a": {}}})
        self.assertEqual(short.status, preflight.OK)
        self.assertIsNone(preflight._long_path_item(Path("C:/g"), {"files": {}}))

    def test_insufficient_space_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = preflight._path_items("目标目录", Path(tmp), need_bytes=1 << 62)
            space = [i for i in items if i.name.endswith("可用空间")][0]
            self.assertEqual(space.status, preflight.FAIL)


class ApiTest(unittest.TestCase):
    def _channel(self, domains=("https://api.invalid",)) -> channels.Channel:
        return channels.Channel("354", "C000", "l1", "g1", secret="s3cret",
                                domains=domains, label="国服", launcher_version="1.2.3")

    def test_headers_and_sign(self):
        api = Api(self._channel())
        headers = api._headers("a=1", 1700000000000)
        self.assertEqual(headers["grp-app-id"], "354")
        self.assertEqual(headers["grp-channel-code"], "C000")
        self.assertEqual(headers["timestamp"], "1700000000000")
        self.assertNotIn("s3cret", json.dumps(headers))
        self.assertEqual(api._sign("a=1", 1700000000000),
                         api._sign("a=1", 1700000000000))
        self.assertNotEqual(api._sign("a=1", 1700000000000),
                            api._sign("a=2", 1700000000000))
        client_info = json.loads(headers["grp-client-info"])
        self.assertEqual(client_info["device_country"], "CN")

    def test_get_without_domains_is_actionable(self):
        api = Api(self._channel(domains=()))
        with self.assertRaises(Exception) as ctx:
            api.get("/x", {})
        self.assertIn("regions.json", str(ctx.exception))

    def test_get_success_and_gzip(self):
        import gzip
        api = Api(self._channel())
        payload = gzip.compress(json.dumps({"code": 0, "data": {"ok": 1}}).encode())
        resp = mock.Mock()
        # _read_limited 分块读到空串为止，mock 必须能返回 EOF
        resp.read.side_effect = [payload, b""]
        resp.headers = {"Content-Encoding": "gzip"}
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            data = api.get("/pc-launcher/x", {"b": "2", "a": "1"})
        self.assertEqual(data["data"], {"ok": 1})
        url = urlopen.call_args.args[0].full_url
        self.assertTrue(url.startswith("https://api.invalid/pc-launcher/x?"))
        self.assertIn("a=1", url)

    def test_get_error_code_raises_after_retries(self):
        api = Api(self._channel())
        body = json.dumps({"code": 100, "message": "bad sign"}).encode()
        resp = mock.Mock()
        resp.read.side_effect = [body, b"", body, b""]  # 每次重试都要能读到 EOF
        resp.headers = {}
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        with mock.patch("urllib.request.urlopen", return_value=resp), \
             mock.patch("time.sleep"):
            with self.assertRaises(Exception) as ctx:
                api.get("/x", {}, retries=2)
        self.assertIn("100", str(ctx.exception))

    def test_http_get_bytes_failure(self):
        from azp.api import http_get_bytes
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")), \
             mock.patch("time.sleep"):
            with self.assertRaises(Exception) as ctx:
                http_get_bytes("https://api.invalid/x", retries=2)
        self.assertIn("down", str(ctx.exception))


class ChannelsTest(unittest.TestCase):
    KEY = b"0123456789abcdef0123456789abcdef"

    def _config_blob(self, root: Path) -> Path:
        cfg = {"appId": "354", "channelCode": "C000", "launcherId": "L1",
               "gameId": "G1", "secretKey": "top-secret",
               "channelExtraParams": {"lang": "zh-Hans"}}
        iv = b"1" * 16
        blob = iv + crypto.encrypt(self.KEY, iv, json.dumps(cfg).encode())
        launcher = root / "1.0.0"
        (launcher / "res").mkdir(parents=True)
        (launcher / "res" / "app_config.dat").write_bytes(blob)
        return launcher

    def test_decrypt_with_env_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = self._config_blob(Path(tmp))
            with mock.patch.dict(os.environ, {"AZP_APPCONFIG_KEY": self.KEY.decode()}):
                cfg = channels.read_app_config(launcher)
            self.assertEqual(cfg["appId"], "354")
            self.assertEqual(cfg["channelCode"], "C000")

    def test_load_maps_to_builtin_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = self._config_blob(Path(tmp))
            with mock.patch.dict(os.environ, {"AZP_APPCONFIG_KEY": self.KEY.decode()}):
                ch = channels.load(launcher / "launcher.exe")
            self.assertEqual(ch.app_id, "354")
            self.assertEqual(ch.label, "国服")
            self.assertEqual(ch.lang, "zh-Hans")
            self.assertEqual(regions.key_for(ch.app_id, ch.channel_code), "cn")
            public = ch.public()
            self.assertNotIn("secret", public)
            self.assertNotIn("top-secret", repr(public))
            self.assertNotIn("top-secret", repr(ch))  # dataclass repr 也不能泄密

    def test_undecryptable_config_raises_with_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = self._config_blob(Path(tmp))
            with mock.patch.dict(os.environ, {"AZP_APPCONFIG_KEY": "f" * 32}):
                with self.assertRaises(ValueError) as ctx:
                    channels.read_app_config(launcher)
            self.assertIn("AZP_APPCONFIG_KEY", str(ctx.exception))

    def test_missing_app_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                channels.read_app_config(Path(tmp))

    def test_candidate_ranking_prefers_native_modules(self):
        self.assertLess(channels._rank(Path("a.node")), channels._rank(Path("a.dll")))
        self.assertLess(channels._rank(Path("a.dll")), channels._rank(Path("app.asar")))
        self.assertLess(channels._rank(Path("app.asar")), channels._rank(Path("readme.txt")))


class CliTest(unittest.TestCase):
    def test_missing_manifest_is_clean_error_not_traceback(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["verify", "--manifest", "no/such.json", "--dir", "."])
        self.assertEqual(code, 1)
        self.assertIn("路径不对", out.getvalue())

    def test_invalid_json_is_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", "utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["verify", "--manifest", str(bad), "--dir", "."])
        self.assertEqual(code, 1)
        self.assertIn("JSON 解析失败", out.getvalue())

    def test_value_error_is_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "plan.csv"
            plan.write_csv([{"action": "link", "size": 1, "md5": "x", "path": "../evil",
                             "src_or_url": "a", "hash_kind": "md5"}], csv_path)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["link", "--plan", str(csv_path), "--base", str(root),
                                 "--target", str(root / "t")])
        self.assertEqual(code, 1)
        self.assertIn("非法相对路径", out.getvalue())

    def test_regions_command(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["regions"]), 0)
        text = out.getvalue()
        for key in ("cn", "bili", "global", "jp"):
            self.assertIn(key, text)

    def test_check_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["check", "--target", str(Path(tmp) / "t"), "--need", "0.001"])
        self.assertIn("Python", out.getvalue())
        self.assertIn(code, (0, 2))

    def test_remove_command_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp) / "AzurPromilia_TC_game"
            (game / "AzurPromilia_Data").mkdir(parents=True)
            (game / "a.bin").write_bytes(b"abc")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["remove", "--target", str(game), "--dry-run"]), 0)
            self.assertTrue((game / "a.bin").exists())
            self.assertIn("试运行", out.getvalue())

    def test_export_and_import_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            source.mkdir()
            csv_path = root / "plan.csv"
            plan.write_csv([{"action": "download", "size": 3, "md5": "x", "path": "a.bin",
                             "src_or_url": "u", "hash_kind": "md5"}], csv_path)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["export", "--plan", str(csv_path),
                                           "--source", str(source), "--dest", str(root / "d"),
                                           "--dry-run"]), 0)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["import", "--plan", str(csv_path),
                                           "--source", str(source), "--target", str(root / "t"),
                                           "--dry-run"]), 0)
            self.assertFalse((root / "t").exists())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["export", "--source", str(source),
                                           "--dest", str(root / "d2")]), 1)
            self.assertIn("--plan", out.getvalue())

    def test_fetch_with_nothing_to_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "plan.csv"
            plan.write_csv([{"action": "link", "size": 3, "md5": "x", "path": "a.bin",
                             "src_or_url": "a.bin", "hash_kind": "md5"}], csv_path)
            with mock.patch.object(journal, "JOURNAL_DIR", root / "journal"):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.main(["fetch", "--plan", str(csv_path),
                                               "--target", str(root / "t")]), 0)
            self.assertIn("没有需要下载", out.getvalue())

    def test_manifests_without_launchers(self):
        with mock.patch.object(cli.detect, "find_installs", return_value=[]), \
             mock.patch.object(cli.yostar, "fetch",
                               side_effect=yostar.YostarError("找不到日服签名盐")), \
             mock.patch.object(cli.regions, "load", return_value=list(regions.BUILTIN)):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["manifests", "--region", "jp", "--region", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("区服表里没有 nope", out.getvalue())
        self.assertIn("找不到日服签名盐", out.getvalue())

    def test_nofade_cli_delegates(self):
        with mock.patch.object(cli.nofade, "status", return_value=0) as status:
            self.assertEqual(cli.main(["nofade", "status", "--target", "."]), 0)
            status.assert_called_once()
        with mock.patch.object(cli.nofade, "restore",
                               side_effect=nofade.NofadeError("不像国服客户端")):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["nofade", "restore", "--target", "."]), 1)
        self.assertIn("不像国服客户端", out.getvalue())


class CoreFlowTest(unittest.TestCase):
    """核心流程端到端：plan → link → finalize → verify → rollback，全程离线。"""

    def test_full_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "cn" / "AzurPromilia_Game"
            base.mkdir(parents=True)
            (base / "a.bin").write_bytes(b"abc")
            md5 = hashes.md5_of(base / "a.bin")
            out = root / "out"
            for key, folder in (("cn", "AzurPromilia_Game"), ("global", "AzurPromilia_TC_game")):
                sub = out / key
                sub.mkdir(parents=True)
                man = {"label": key, "version_code": "100", "version_name": "1.0.0",
                       "game_folder_name": folder, "total_size": 3, "cdn_host": "https://x",
                       "files": {"a.bin": {"size": 3, "md5": md5, "url": "a"}}}
                (sub / "manifest.json").write_text(json.dumps(man), "utf-8")
                (sub / "index.csv").write_text(f"0,0,3,{md5},a.bin,u\n", "utf-8")
            target = root / "global" / "AzurPromilia_TC_game"
            with mock.patch.object(journal, "JOURNAL_DIR", out / "journal"), \
                 mock.patch.object(journal, "BACKUP_DIR", out / "backup"), \
                 contextlib.redirect_stdout(io.StringIO()) as log:
                self.assertEqual(cli.main(["plan", "--base", "cn", "--target", "global",
                                           "--out", str(out)]), 0)
                csv_path = out / "plan_cn_to_global.csv"
                self.assertEqual([r["action"] for r in plan.read_csv(csv_path)], ["link"])
                self.assertEqual(cli.main(["link", "--plan", str(csv_path), "--base", str(base),
                                           "--target", str(target)]), 0)
                self.assertEqual((target / "a.bin").read_bytes(), b"abc")
                # 修复前首次 finalize 的账本是空的，rollback 无账可查
                self.assertEqual(cli.main([
                    "finalize", "--target", str(target),
                    "--manifest", str(out / "global" / "manifest.json"),
                    "--index-csv", str(out / "global" / "index.csv")]), 0)
                self.assertTrue((target / "_index" / "100").exists())
                self.assertEqual(cli.main([
                    "verify", "--manifest", str(out / "global" / "manifest.json"),
                    "--dir", str(target), "--sample", "5"]), 0)
                books = journal.available(target)
                self.assertEqual(len(books), 2)  # link 与 finalize 各一笔
                for book in reversed(books):
                    self.assertEqual(cli.main(["rollback", "--target", str(target),
                                               "--id", book.name, "--yes"]), 0)
                self.assertIn("回退会删掉这些新建文件", log.getvalue())
            self.assertFalse((target / "a.bin").exists())
            self.assertFalse((target / "_index" / "100").exists())
            self.assertEqual((base / "a.bin").read_bytes(), b"abc")  # 源区始终没动
            self.assertEqual(journal.available(target), [])

    def test_rollback_partial_failure_keeps_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            created = target / "a.bin"
            created.write_bytes(b"abc")
            with mock.patch.object(journal, "JOURNAL_DIR", root / "journal"):
                entry = journal.Entry("link", target)
                entry.add_created(created, 999)  # 大小对不上 → 保留文件
                book = entry.save()
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = cli.main(["rollback", "--target", str(target), "--yes"])
                self.assertEqual(code, 2)
                self.assertTrue(book.exists())  # 没退干净就不能把账本作废
                self.assertTrue(created.exists())
                self.assertIn("账本保留", out.getvalue())

    def test_rollback_without_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(journal, "JOURNAL_DIR", Path(tmp) / "journal"):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.main(["rollback", "--target", str(Path(tmp))]), 1)
            self.assertIn("没有可回退的记录", out.getvalue())


class QuickstartTest(unittest.TestCase):
    """向导全流程用假清单驱动，验证「一条命令」确实能装出目标区。"""

    def setUp(self):
        # 本机真开着游戏也不影响离线测试；真实进程检测另有 PreflightMoreTest
        game = mock.patch.object(cli.preflight, "running_games", return_value=[])
        game.start()
        self.addCleanup(game.stop)

    def _run(self, root: Path, extra: list[str], interactive: bool = False,
             answers: list[str] | None = None) -> tuple[int, str, Path]:
        base_dir = root / "cn" / "AzurPromilia_Game"
        (base_dir / "AzurPromilia_Data").mkdir(parents=True, exist_ok=True)
        (base_dir / "a.bin").write_bytes(b"abc")
        target_dir = root / "global" / "AzurPromilia_TC_game"
        md5 = hashes.md5_of(base_dir / "a.bin")
        installs = [
            detect.Install(root=root / "cn", launcher_exe=root / "cn" / "1.0.0" / "launcher.exe",
                           game_dirs=[base_dir]),
            detect.Install(root=root / "global",
                           launcher_exe=root / "global" / "1.0.0" / "launcher.exe"),
        ]
        chs = {
            "cn": channels.Channel("354", "C000", "l1", "g1", secret="s", label="国服"),
            "global": channels.Channel("434", "C500", "l2", "g2", secret="s", label="台服"),
        }

        def fake_load(exe):
            return chs["cn"] if "cn" in {p.name for p in Path(exe).parents} else chs["global"]

        def fake_fetch(api, out_dir, force=False):
            out_dir.mkdir(parents=True, exist_ok=True)
            key = "cn" if api.ch.app_id == "354" else "global"
            folder = "AzurPromilia_Game" if key == "cn" else "AzurPromilia_TC_game"
            (out_dir / "index.csv").write_text(f"0,0,3,{md5},a.bin,u\n", "utf-8")
            return {"label": key, "version_code": "100", "version_name": "1.0.0",
                    "game_folder_name": folder, "total_size": 3, "cdn_host": "https://x",
                    "files": {"a.bin": {"size": 3, "md5": md5, "url": "a"}}}

        if interactive:
            argv = ["quickstart", "--out", str(root / "out"), *extra]
        else:
            argv = ["quickstart", "--base", "cn", "--target", "global",
                    "--target-dir", str(target_dir), "--out", str(root / "out"), *extra]
        patches = [
            mock.patch.object(cli.detect, "find_installs", return_value=installs),
            mock.patch.object(cli.channels, "load", side_effect=fake_load),
            mock.patch.object(cli.manifest, "fetch", side_effect=fake_fetch),
            mock.patch.object(journal, "JOURNAL_DIR", root / "journal"),
            mock.patch.object(journal, "BACKUP_DIR", root / "backup"),
        ]
        if answers is not None:
            patches.append(mock.patch.object(cli, "_ask", side_effect=answers))
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            with contextlib.redirect_stdout(out):
                code = cli.main(argv)
        return code, out.getvalue(), target_dir

    def test_quickstart_builds_target_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, log, target_dir = self._run(Path(tmp), ["--yes"])
            self.assertEqual(code, 0, log)
            self.assertEqual((target_dir / "a.bin").read_bytes(), b"abc")
            self.assertTrue((target_dir / "_index" / "100").exists())
            self.assertIn("完成", log)
            self.assertIn("缺失 0", log)

    def test_quickstart_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, log, target_dir = self._run(Path(tmp), ["--dry-run"])
            self.assertEqual(code, 0, log)
            self.assertFalse(target_dir.exists())  # dry-run 不能建目标目录
            self.assertIn("--dry-run", log)

    def test_quickstart_rejects_same_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_dir = root / "cn" / "AzurPromilia_Game"
            (base_dir / "AzurPromilia_Data").mkdir(parents=True)
            inst = detect.Install(root=root / "cn",
                                  launcher_exe=root / "cn" / "1.0.0" / "launcher.exe",
                                  game_dirs=[base_dir])
            ch = channels.Channel("354", "C000", "l1", "g1", secret="s", label="国服")
            with mock.patch.object(cli.detect, "find_installs", return_value=[inst]), \
                 mock.patch.object(cli.channels, "load", return_value=ch), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["quickstart", "--base", "cn", "--target", "cn"])
            self.assertEqual(code, 1)
            self.assertIn("同一个区", out.getvalue())

    def test_quickstart_rejects_missing_launcher_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_dir = root / "cn" / "AzurPromilia_Game"
            (base_dir / "AzurPromilia_Data").mkdir(parents=True)
            inst = detect.Install(root=root / "cn",
                                  launcher_exe=root / "cn" / "1.0.0" / "launcher.exe",
                                  game_dirs=[base_dir])
            ch = channels.Channel("354", "C000", "l1", "g1", secret="s", label="国服")
            with mock.patch.object(cli.detect, "find_installs", return_value=[inst]), \
                 mock.patch.object(cli.channels, "load", return_value=ch), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["quickstart", "--base", "cn", "--target", "global"])
            self.assertEqual(code, 1)
            self.assertIn("没找到对应的启动器", out.getvalue())

    def test_quickstart_interactive_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 不给 --base/--target：基线只有一个可自动定，目标区靠编号选
            code, log, target_dir = self._run(Path(tmp), ["--yes"], interactive=True,
                                              answers=["1"])
            self.assertEqual(code, 0, log)
            self.assertEqual((target_dir / "a.bin").read_bytes(), b"abc")
            self.assertIn("加哪个区", log)

    def test_quickstart_cancel_when_target_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_dir = root / "global" / "AzurPromilia_TC_game"
            target_dir.mkdir(parents=True)
            (target_dir / "old.bin").write_bytes(b"old")
            code, log, _ = self._run(root, [], interactive=True, answers=["1", "3"])
            self.assertEqual(code, 1)
            self.assertEqual((target_dir / "old.bin").read_bytes(), b"old")

    def test_quickstart_reinstall_goes_to_recycle_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_dir = root / "global" / "AzurPromilia_TC_game"
            target_dir.mkdir(parents=True)
            (target_dir / "old.bin").write_bytes(b"old")
            with mock.patch.object(cli.remove, "run", return_value=0) as rm:
                code, log, _ = self._run(root, [], interactive=True, answers=["1", "2", "y"])
            self.assertEqual(code, 0, log)
            self.assertTrue(rm.called)
            self.assertEqual(rm.call_args.kwargs["trash"], True)
            self.assertEqual(rm.call_args.kwargs["fallback"], False)

    def test_baseline_list_shown_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cn_dir = root / "cn" / "AzurPromilia_Game"
            gl_dir = root / "global" / "AzurPromilia_TC_game"
            for d in (cn_dir, gl_dir):
                (d / "AzurPromilia_Data").mkdir(parents=True)
                (d / "a.bin").write_bytes(b"abc")
            md5 = hashes.md5_of(cn_dir / "a.bin")
            installs = [
                detect.Install(root=root / "cn", launcher_exe=root / "cn" / "l.exe",
                               game_dirs=[cn_dir]),
                detect.Install(root=root / "global", launcher_exe=root / "global" / "l.exe",
                               game_dirs=[gl_dir]),
            ]
            chs = {
                "cn": channels.Channel("354", "C000", "l1", "g1", secret="s", label="国服"),
                "global": channels.Channel("434", "C500", "l2", "g2", secret="s", label="台服"),
            }

            def fake_load(exe):
                return chs["cn"] if "cn" in {p.name for p in Path(exe).parents} else chs["global"]

            def fake_fetch(api, out_dir, force=False):
                out_dir.mkdir(parents=True, exist_ok=True)
                key = "cn" if api.ch.app_id == "354" else "global"
                folder = "AzurPromilia_Game" if key == "cn" else "AzurPromilia_TC_game"
                (out_dir / "index.csv").write_text(f"0,0,3,{md5},a.bin,u\n", "utf-8")
                return {"label": key, "version_code": "100", "version_name": "1.0.0",
                        "game_folder_name": folder, "total_size": 3, "cdn_host": "https://x",
                        "files": {"a.bin": {"size": 3, "md5": md5, "url": "a"}}}

            out = io.StringIO()
            with mock.patch.object(cli.detect, "find_installs", return_value=installs), \
                 mock.patch.object(cli.channels, "load", side_effect=fake_load), \
                 mock.patch.object(cli.manifest, "fetch", side_effect=fake_fetch), \
                 mock.patch.object(cli, "_ask", side_effect=["1", "1"]), \
                 mock.patch.object(journal, "JOURNAL_DIR", root / "journal"), \
                 mock.patch.object(journal, "BACKUP_DIR", root / "backup"), \
                 contextlib.redirect_stdout(out):
                code = cli.main(["quickstart", "--out", str(root / "out"), "--dry-run"])
            log = out.getvalue()
            self.assertEqual(code, 0, log)
            # 同两份安装只列一次，不再先打「检测到以下安装」再打一遍选择列表
            self.assertEqual(log.count("1. 国服"), 1, log)
            self.assertNotIn("检测到以下安装", log)

    def test_quickstart_counts_installed_files_as_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_dir = root / "cn" / "AzurPromilia_Game"
            (base_dir / "AzurPromilia_Data").mkdir(parents=True)
            (base_dir / "a.bin").write_bytes(b"abc")
            target_dir = root / "global" / "AzurPromilia_TC_game"
            (target_dir / "AzurPromilia_Data").mkdir(parents=True)
            (target_dir / "a.bin").write_bytes(b"xyz")
            base_md5 = hashes.md5_of(base_dir / "a.bin")
            target_md5 = hashes.md5_of(target_dir / "a.bin")
            installs = [
                detect.Install(root=root / "cn", launcher_exe=root / "cn" / "l.exe",
                               game_dirs=[base_dir]),
                detect.Install(root=root / "global", launcher_exe=root / "global" / "l.exe"),
            ]
            chs = {
                "cn": channels.Channel("354", "C000", "l1", "g1", secret="s", label="国服"),
                "global": channels.Channel("434", "C500", "l2", "g2", secret="s", label="台服"),
            }

            def fake_load(exe):
                return chs["cn"] if "cn" in {p.name for p in Path(exe).parents} else chs["global"]

            def fake_fetch(api, out_dir, force=False):
                out_dir.mkdir(parents=True, exist_ok=True)
                is_cn = api.ch.app_id == "354"
                key = "cn" if is_cn else "global"
                md5 = base_md5 if is_cn else target_md5
                folder = "AzurPromilia_Game" if is_cn else "AzurPromilia_TC_game"
                (out_dir / "index.csv").write_text(f"0,0,3,{md5},a.bin,u\n", "utf-8")
                return {"label": key, "version_code": "100", "version_name": "1.0.0",
                        "game_folder_name": folder, "total_size": 3, "cdn_host": "https://x",
                        "files": {"a.bin": {"size": 3, "md5": md5, "url": "a"}}}

            out = io.StringIO()
            with mock.patch.object(cli.detect, "find_installs", return_value=installs), \
                 mock.patch.object(cli.channels, "load", side_effect=fake_load), \
                 mock.patch.object(cli.manifest, "fetch", side_effect=fake_fetch), \
                 mock.patch.object(journal, "JOURNAL_DIR", root / "journal"), \
                 mock.patch.object(journal, "BACKUP_DIR", root / "backup"), \
                 contextlib.redirect_stdout(out):
                code = cli.main(["quickstart", "--base", "cn", "--target", "global",
                                 "--target-dir", str(target_dir),
                                 "--out", str(root / "out"), "--yes"])
            log = out.getvalue()
            self.assertEqual(code, 0, log)
            self.assertIn("已就绪 1 个", log)
            self.assertIn("下载 0 个", log)
            self.assertEqual((target_dir / "a.bin").read_bytes(), b"xyz")

    def test_quickstart_remembers_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, log, target_dir = self._run(root, ["--yes"])
            self.assertEqual(code, 0, log)
            prefs = json.loads((root / "out" / "quickstart.json").read_text("utf-8"))
            self.assertEqual(prefs["base"], "cn")
            self.assertEqual(prefs["target"], "global")
            self.assertEqual(Path(prefs["target_dir"]), target_dir)

            # 第二次不带 --base/--target/--target-dir：回车沿用记忆值
            with mock.patch("builtins.input", side_effect=["", ""]):
                code, log2, target2 = self._run(root, ["--yes"], interactive=True)
            self.assertEqual(code, 0, log2)
            self.assertIn("上次：国服 → 台服", log2)
            self.assertEqual(target2, target_dir)


class NofadeNoticeTest(unittest.TestCase):
    REL = ".res/default_package/BundleFiles/p.r"

    def _patch(self):
        return nofade.Patch(name="t", file=self.REL, size=3,
                            original_md5=hashlib.md5(b"abc").hexdigest(),
                            patched_md5=hashlib.md5(b"xyz").hexdigest(),
                            resource_version="1")

    def _client(self, root: Path, content: bytes) -> Path:
        game = root / "AzurPromilia_Game"
        dest = game / "AzurPromilia_Data" / Path(self.REL)
        dest.parent.mkdir(parents=True)
        dest.write_bytes(content)
        return game

    def test_target_patched_kept_without_asking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self._client(root / "base", b"abc")
            target = self._client(root / "target", b"xyz")
            with mock.patch.object(cli.nofade, "load", return_value=self._patch()), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                ignore = cli._nofade_notice(base, target, False, False, False, False)
            self.assertEqual(ignore, {self.REL})
            self.assertIn("已保留补丁", out.getvalue())

    def test_target_patched_can_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self._client(root / "base", b"abc")
            target = self._client(root / "target", b"xyz")
            with mock.patch.object(cli.nofade, "load", return_value=self._patch()), \
                 mock.patch.object(cli, "_ask", return_value="2"), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                ignore = cli._nofade_notice(base, target, False, False, True, False)
            self.assertEqual(ignore, set())
            self.assertIn("覆盖补丁", out.getvalue())

    def test_source_patched_warns_about_hardlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self._client(root / "base", b"xyz")
            with mock.patch.object(cli.nofade, "load", return_value=self._patch()), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                cli._nofade_notice(base, root / "target", False, False, False, False)
            self.assertIn("硬链接会把这个文件带到目标区", out.getvalue())


class ChannelSwapTest(unittest.TestCase):
    def _client(self, root: Path, bilibili: bool) -> Path:
        game = root / "AzurPromilia_Game"
        sdk = game / "AzurPromilia_Data" / "Plugins" / "mj_sdk"
        sdk.mkdir(parents=True)
        (game / "mj_sdk" / "res").mkdir(parents=True)
        (game / "mj_sdk" / "res" / "app_config.dat").write_bytes(b"cfg")
        if bilibili:
            (sdk / "BLPlatform64").mkdir()
        return game

    def test_current_detects_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(channels_swap.current(self._client(root / "a", False)), "official")
            self.assertEqual(channels_swap.current(self._client(root / "b", True)), "bilibili")

    def test_switch_requires_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = self._client(root / "game", False)
            with mock.patch.object(channels_swap.preflight, "running_games", return_value=[]):
                with self.assertRaises(channels_swap.ChannelError):
                    channels_swap.switch(game, "official", "bilibili",
                                         source_dir=root / "src", backup_dir=root / "bak")

    def test_extract_and_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official = self._client(root / "official", False)
            bili = self._client(root / "bili", True)
            src = root / "src"
            bak = root / "bak"
            channels_swap.extract(official, "official", source_dir=src)
            channels_swap.extract(bili, "bilibili", source_dir=src)
            with mock.patch.object(channels_swap.preflight, "running_games", return_value=[]):
                channels_swap.switch(official, "official", "bilibili",
                                     source_dir=src, backup_dir=bak)
            self.assertEqual(channels_swap.current(official), "bilibili")
            self.assertTrue((bak / "official").is_dir())


class InstallerScanTest(unittest.TestCase):
    def test_maps_installer_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AzurPromilia_manjuu_installer_1.0.1.1_35_SC000.exe").write_bytes(b"x")
            (root / "AzurPromilia_jimi_installer.exe").write_bytes(b"x")
            (root / "AzurPromilia_yostar_installer.exe").write_bytes(b"x")
            found = backend._scan_installers([root], min_bytes=0)
        self.assertIn("cn", found)
        self.assertIn("global", found)
        self.assertIn("jp", found)
        self.assertTrue(found["cn"].endswith("AzurPromilia_manjuu_installer_1.0.1.1_35_SC000.exe"))

    def test_small_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AzurPromilia_manjuu_installer.exe").write_bytes(b"x")
            found = backend._scan_installers([root], min_bytes=1024)
        self.assertEqual(found, {})


class RegionsInfoTest(unittest.TestCase):
    def test_returns_builtin_regions(self):
        data = backend.regions_info({})
        keys = {r["key"] for r in data["regions"]}
        for key in ("cn", "bili", "global", "jp"):
            self.assertIn(key, keys)
        for item in data["regions"]:
            self.assertIn("label", item)
            self.assertIn("folder", item)
            self.assertIn("ready", item)

    def test_cn_has_bili_variant(self):
        with mock.patch.object(backend.detect, "find_installs", return_value=[]):
            data = backend.detect_all({})
        keys = {r["key"] for r in data["installs"]}
        self.assertIn("cn", keys)
        self.assertNotIn("bili", keys)
        cn = next(r for r in data["installs"] if r["key"] == "cn")
        variant_keys = [v["key"] for v in cn["variants"]]
        self.assertIn("bili", variant_keys)


class JournalListTest(unittest.TestCase):
    def test_lists_entries_for_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "AzurPromilia_Game"
            target.mkdir()
            jdir = root / "journal"
            jdir.mkdir()
            entry = {
                "cmd": "quickstart",
                "target": str(target.absolute()),
                "time": 1,
                "created": [{"path": str(target / "a.bin"), "size": 3}],
                "backups": [{"orig": str(target / "b.bin"), "saved": str(root / "b.bin")}],
            }
            (jdir / "20260101-000000-quickstart.json").write_text(
                json.dumps(entry), "utf-8")
            with mock.patch.object(journal, "JOURNAL_DIR", jdir):
                data = backend.journal_list({"target": str(target)})
            self.assertEqual(len(data["entries"]), 1)
            item = data["entries"][0]
            self.assertEqual(item["cmd"], "quickstart")
            self.assertEqual(item["created_files"], 1)
            self.assertEqual(item["created_bytes"], 3)
            self.assertEqual(item["backup_files"], 1)

    def test_missing_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = backend.journal_list({"target": str(Path(tmp) / "nope")})
        self.assertEqual(data["entries"], [])


class VerifyDirIgnoreTest(unittest.TestCase):
    REL = "AzurPromilia_Data/.res/default_package/BundleFiles/p.r"

    def test_ignored_patch_passes_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / self.REL).parent.mkdir(parents=True)
            (root / self.REL).write_bytes(b"xyz")
            data = {"files": {self.REL: {"size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72"}}}
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(cli._verify_dir(data, root, sample=5))
                self.assertTrue(cli._verify_dir(
                    data, root, sample=5,
                    ignore={".res/default_package/BundleFiles/p.r"}))


class YostarLocalTest(unittest.TestCase):
    SALT = "0123456789abcdef0123456789abcdef"

    def tearDown(self):
        yostar._salt = None

    def test_salt_from_launcher_asar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            res = root / "1.0.0" / "resources"
            res.mkdir(parents=True)
            (res / "app.asar").write_bytes(
                b'junk\n  salt: "' + self.SALT.encode() + b'"\nmore')
            inst = detect.Install(root=root, launcher_exe=root / "1.0.0" / "AZUPRO_Gamelauncher.exe",
                                  kind="yostar")
            other = detect.Install(root=root, launcher_exe=root / "x" / "launcher.exe")
            with mock.patch.object(detect, "find_installs", return_value=[other, inst]):
                yostar._salt = None
                self.assertEqual(yostar._salt_from_launcher(), self.SALT)
                self.assertEqual(yostar.salt(), self.SALT)  # 走启动器而不是环境变量

    def test_salt_not_found_anywhere(self):
        with mock.patch.object(detect, "find_installs", return_value=[]):
            self.assertIsNone(yostar._salt_from_launcher())
        with mock.patch.object(detect, "find_installs", side_effect=OSError("boom")):
            self.assertIsNone(yostar._salt_from_launcher())

    def test_game_config_and_manifest_url(self):
        def fake_get(path, timeout=30):
            if path == "/api/launcher/game/config":
                return {"game_latest_version": "1.0.0", "game_latest_file_path": "/pkg/",
                        "game_start_exe_name": "AzurPromiliaJP", "game_start_params": ["-x"],
                        "game_uninstall_script": "un.cmd", "decompression_size": "80 GB"}
            if path == "/api/launcher/advanced/game/download/cdn":
                return {"primary_cdn": "https://cdn.invalid/", "back_up_cdn": "https://b.invalid"}
            if path.startswith("/api/launcher/game/config/json"):
                self.assertIn("version=1.0.0", path)
                return {"url": "https://cdn.invalid/manifest.json"}
            raise AssertionError(path)

        with mock.patch.object(yostar, "_get", side_effect=fake_get):
            cfg = yostar.game_config()
            self.assertEqual(cfg["version"], "1.0.0")
            self.assertEqual(cfg["cdn"], "https://cdn.invalid")
            self.assertEqual(cfg["entry"], "AzurPromiliaJP")
            self.assertEqual(yostar.manifest_url("1.0.0", "/pkg/"),
                             "https://cdn.invalid/manifest.json")

    def test_get_error_paths(self):
        # _get 要先拿盐签名，所以两个分支都得把盐准备好
        with mock.patch.dict(os.environ, {"AZP_YOSTAR_SALT": self.SALT}), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            with self.assertRaises(yostar.YostarError) as ctx:
                yostar._get("/x")
        self.assertIn("接口请求失败", str(ctx.exception))
        resp = mock.Mock()
        resp.read.return_value = json.dumps({"code": 500, "msg": "bad"}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        with mock.patch.dict(os.environ, {"AZP_YOSTAR_SALT": self.SALT}), \
             mock.patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(yostar.YostarError) as ctx:
                yostar._get("/x")
        self.assertIn("500", str(ctx.exception))

    def test_fetch_builds_manifest(self):
        raw = {"source": "/pkg/", "file": [
            {"path": "/AzurPromiliaJP_Data/a.bin", "size": 3, "hash": "123"},
            {"path": "", "size": 0, "hash": "0"},
        ]}
        cfg = {"version": "1.0.0", "basis": "/pkg/", "entry": "AzurPromiliaJP",
               "params": [], "uninstall": "u", "size_text": "80 GB",
               "cdn": "https://cdn.invalid", "cdn_backup": ""}
        resp = mock.Mock()
        resp.read.return_value = json.dumps(raw).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with mock.patch.object(yostar, "game_config", return_value=cfg), \
                 mock.patch.object(yostar, "manifest_url",
                                   return_value="https://cdn.invalid/m.json"), \
                 mock.patch("urllib.request.urlopen", return_value=resp):
                with contextlib.redirect_stdout(io.StringIO()):
                    man = yostar.fetch(out, force=True)
            self.assertEqual(man["hash_kind"], "crc64")
            self.assertEqual(man["game_folder_name"], "AZUPRO_JP")
            self.assertEqual(man["launch_entry"], "AzurPromiliaJP.exe")
            files = man["files"]
            self.assertEqual(list(files), ["AzurPromiliaJP_Data/a.bin"])
            self.assertEqual(files["AzurPromiliaJP_Data/a.bin"]["url"],
                             "https://cdn.invalid/pkg/AzurPromiliaJP_Data/a.bin")
            self.assertEqual(man["total_size"], 3)
            self.assertTrue((out / "manifest.raw.json").exists())
            with mock.patch.object(yostar, "game_config", return_value=cfg), \
                 mock.patch.object(yostar, "manifest_url",
                                   side_effect=AssertionError("版本未变，不该重抓清单")):
                cached = yostar.fetch(out)
            self.assertEqual(cached["file_count"], 1)

            updated = {**cfg, "version": "1.0.1"}
            with mock.patch.object(yostar, "game_config", return_value=updated), \
                 mock.patch.object(yostar, "manifest_url",
                                   return_value="https://cdn.invalid/new.json") as url, \
                 mock.patch("urllib.request.urlopen", return_value=resp):
                refreshed = yostar.fetch(out)
            self.assertEqual(refreshed["version_code"], "1.0.1")
            url.assert_called_once_with("1.0.1", "/pkg/")

    def test_fetch_rejects_unknown_structure(self):
        cfg = {"version": "1", "basis": "/p/", "entry": "e", "params": [], "uninstall": "",
               "size_text": "", "cdn": "https://c", "cdn_backup": ""}
        resp = mock.Mock()
        resp.read.return_value = json.dumps({"source": "", "file": []}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(yostar, "game_config", return_value=cfg), \
                 mock.patch.object(yostar, "manifest_url", return_value="https://c/m"), \
                 mock.patch("urllib.request.urlopen", return_value=resp):
                with self.assertRaises(yostar.YostarError):
                    yostar.fetch(Path(tmp), force=True)

    def test_finalize_writes_two_json(self):
        man = {"version_code": "1.0.0", "basis": "/pkg/", "launch_entry": "AzurPromiliaJP.exe",
               "launch_params": ["-x"], "uninstall_script": "u.cmd",
               "files": {"AzurPromiliaJP_Data/a.bin": {"size": 3, "md5": "123"}}}
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "AZUPRO_JP"
            written = yostar.finalize(target, man)
            self.assertEqual({p.name for p in written},
                             {"manifest.json", "game-launcher-config.json"})
            doc = json.loads((target / "manifest.json").read_text("utf-8"))
            self.assertEqual(doc["name"], "AZUPRO_JP")
            self.assertEqual(doc["version"], "1.0.0")
            self.assertTrue(doc["vc"])
            item = doc["files"][0]
            self.assertEqual(item["path"], "/AzurPromiliaJP_Data/a.bin")
            self.assertEqual(item["hash"], "123")
            self.assertEqual(item["size"], "3")
            self.assertTrue(item["vc"])
            config = json.loads((target / "game-launcher-config.json").read_text("utf-8"))
            self.assertEqual(config["name"], "AzurPromiliaJP")  # 去掉 .exe
            self.assertEqual(config["params"], ["-x"])
            self.assertTrue(config["vc"])

    def test_finalize_rejects_wrong_folder_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "azupro_jp"  # 小写也不行，启动器按精确名字认
            with self.assertRaises(yostar.YostarError):
                yostar.finalize(target, {"files": {}})

    def test_js_str_rules(self):
        self.assertEqual(yostar._js_str(["a", 1, True]), "a,1,true")
        self.assertEqual(yostar._js_str(None), "null")
        self.assertEqual(yostar._js_str(False), "false")


class ManifestFetchTest(unittest.TestCase):
    def _api(self) -> Api:
        ch = channels.Channel("354", "C000", "L1", "G1", secret="s", label="国服",
                              domains=("https://api.invalid",), launcher_version="1.0.0")
        return Api(ch)

    def _config(self, raw_url="http://cdn.invalid/index.csv", package_type=2) -> dict:
        return {"data": {"released": {
            "versionCode": "100", "versionName": "1.0.0",
            "gameFolderName": "AzurPromilia_Game", "launchEntry": "AzurPromilia.exe",
            "gamePackageList": [{"packageType": package_type, "downloadUrl": raw_url,
                                 "cdnBaseUrls": ["https://cdn.invalid"]}],
        }}}

    def test_fetch_plaintext_index(self):
        api = self._api()
        text = "100,1,0\n0,3,900150983CD24FB0D6963F7D28E17F72,a.bin,http://cdn.invalid/a\n"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with mock.patch.object(api, "get", return_value=self._config()), \
                 mock.patch.object(manifest, "http_get_bytes",
                                   return_value=text.encode()) as get:
                man = manifest.fetch(api, out, force=True)
            self.assertEqual(man["version_code"], "100")
            self.assertEqual(man["game_folder_name"], "AzurPromilia_Game")
            self.assertEqual(man["cdn_host"], "http://cdn.invalid")
            self.assertEqual(man["total_size"], 3)
            self.assertEqual(man["files"]["a.bin"]["md5"],
                             "900150983cd24fb0d6963f7d28e17f72")
            self.assertTrue((out / "index.raw").exists())
            self.assertTrue((out / "packageconfig.json").exists())
            get.assert_called_once()
            with mock.patch.object(api, "get", return_value=self._config()), \
                 mock.patch.object(manifest, "http_get_bytes",
                                   side_effect=AssertionError("版本未变，不该重复下载")):
                cached = manifest.fetch(api, out)
            self.assertEqual(cached["file_count"], 1)

            updated = self._config()
            updated["data"]["released"]["versionCode"] = "101"
            with mock.patch.object(api, "get", return_value=updated), \
                 mock.patch.object(manifest, "http_get_bytes",
                                   return_value=text.encode()) as get:
                refreshed = manifest.fetch(api, out)
            self.assertEqual(refreshed["version_code"], "101")
            get.assert_called_once()

    def test_fetch_encrypted_index(self):
        api = self._api()
        text = "100,1,0\n0,3,900150983cd24fb0d6963f7d28e17f72,a.bin,u\n"
        blob = manifest.encrypt_index(text)  # base64 里没有逗号 → 走解密分支
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(api, "get", return_value=self._config()), \
                 mock.patch.object(manifest, "http_get_bytes", return_value=blob):
                man = manifest.fetch(api, Path(tmp), force=True)
        self.assertEqual(list(man["files"]), ["a.bin"])
        self.assertEqual(man.decrypt_index(blob) if hasattr(man, "decrypt_index")
                         else manifest.decrypt_index(blob), text)

    def test_fetch_relative_url_uses_cdn_base(self):
        api = self._api()
        text = "0,3,900150983cd24fb0d6963f7d28e17f72,a.bin,u\n"
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(api, "get", return_value=self._config(raw_url="index.csv")), \
                 mock.patch.object(manifest, "http_get_bytes",
                                   return_value=text.encode()) as get:
                manifest.fetch(api, Path(tmp), force=True)
        self.assertEqual(get.call_args.args[0], "https://cdn.invalid/index.csv")

    def test_fetch_without_package_or_download(self):
        api = self._api()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(api, "get", return_value={"data": {"released": {}}}):
                with self.assertRaises(Exception) as ctx:
                    manifest.fetch(api, Path(tmp), force=True)
            self.assertIn("packageType", str(ctx.exception))
            with mock.patch.object(api, "get",
                                   return_value=self._config(raw_url="")), \
                 mock.patch.object(manifest, "http_get_bytes", side_effect=OSError("down")), \
                 mock.patch("time.sleep"):
                with self.assertRaises(Exception):
                    manifest.fetch(api, Path(tmp), force=True)


class DetectMoreTest(unittest.TestCase):
    def test_yostar_under_program_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "Program Files" / "YostarGames"
            launcher = base / "AZUPRO_JP_Gamelauncher" / "AZUPRO_JP_Gamelauncher.exe"
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes(b"")

            roots = detect._scan_roots(Path(tmp))
            self.assertIn(base, roots)
            installs = detect._installs_in(base)
            self.assertTrue(installs)
            self.assertEqual(installs[0].root, base)
            self.assertEqual(installs[0].launcher_exe, launcher)
            self.assertEqual(installs[0].kind, "yostar")
            self.assertIsNone(installs[0].game_dir)

            manual = detect.from_path(launcher.parent)
            self.assertEqual(manual.root, launcher.parent)
            self.assertEqual(manual.launcher_exe, launcher)
            manual = detect.from_path(base)
            self.assertEqual(manual.root, base)
            self.assertEqual(manual.launcher_exe, launcher)

            game = base / "AZUPRO_JP"
            (game / "AzurPromiliaJP_Data").mkdir(parents=True)
            installs = detect._installs_in(base)
            self.assertEqual(installs[0].game_dir, game)
            self.assertEqual(detect.from_path(base).game_dir, game)

    def test_manual_launcher_persists_for_find_installs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "自定义位置" / "YostarGames"
            launcher = root / "AZUPRO_JP_Gamelauncher" / "AZUPRO_JP_Gamelauncher.exe"
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes(b"")
            config = Path(tmp) / "state" / "launchers.json"
            with mock.patch.object(detect, "MANUAL_FILE", config), \
                 mock.patch.object(detect, "_registry_game_dirs", return_value=[]), \
                 mock.patch.object(detect.string, "ascii_uppercase", ""):
                added = detect.add_manual(launcher)
                self.assertEqual(added.launcher_exe, launcher)
                self.assertTrue(config.is_file())
                installs = detect.find_installs()
            self.assertEqual(len(installs), 1)
            self.assertEqual(installs[0].launcher_exe, launcher)
            self.assertEqual(installs[0].kind, "yostar")

    def test_version_key_ordering(self):
        self.assertEqual(detect._version_key("1.2.3"), (1, 2, 3))
        self.assertEqual(detect._version_key(""), (0,))
        self.assertGreater(detect._version_key("1.0.0.1"), detect._version_key("1.0.0"))
        self.assertGreater(detect._version_key("2.0"), detect._version_key("1.9.9"))

    def test_looks_like_yostar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(detect.looks_like_yostar(root / "AZUPRO_JP_Gamelauncher.exe"))
            plain = root / "launcher.exe"
            self.assertFalse(detect.looks_like_yostar(plain))
            res = root / "resources"
            res.mkdir()
            (res / "app-update.yml").write_text("owner: yo-star\n", "utf-8")
            self.assertTrue(detect.looks_like_yostar(plain))

    def test_from_path_picks_highest_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for version in ("1.0.0", "1.2.0"):
                (root / version).mkdir()
                (root / version / "launcher.exe").write_bytes(b"")
            install = detect.from_path(root)
            self.assertEqual(install.launcher_exe, root / "1.2.0" / "launcher.exe")
            self.assertEqual(install.root, root)

    def test_from_path_flat_yostar_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "azurpromilia-jp"
            root.mkdir()
            exe = root / "AZUPRO_JP_Gamelauncher.exe"
            exe.write_bytes(b"")
            install = detect.from_path(exe)
            self.assertEqual(install.root, root)  # 安装根直接放 exe 时不能再往上一层
            self.assertEqual(install.kind, "yostar")

    def test_from_path_without_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty"
            root.mkdir()
            with self.assertRaises(FileNotFoundError):
                detect.from_path(root)

    def test_find_installs_scans_extra_roots_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "1.0.0").mkdir()
            (root / "1.0.0" / "launcher.exe").write_bytes(b"")
            (root / "2.0.0").mkdir()
            (root / "2.0.0" / "launcher.exe").write_bytes(b"")
            game = root / "AzurPromilia_Game"
            (game / "AzurPromilia_Data").mkdir(parents=True)
            with mock.patch.object(detect, "_registry_game_dirs", return_value=[]), \
                 mock.patch.object(detect.string, "ascii_uppercase", ""):
                installs = detect.find_installs([str(root), str(root)])
            self.assertEqual(len(installs), 1)
            self.assertEqual(installs[0].version, "2.0.0")  # 同 root 只留最高版本
            self.assertEqual(installs[0].game_dirs, [game])
            self.assertEqual(installs[0].game_dir, game)
            self.assertIsNone(detect.Install(root, root / "x.exe").game_dir)

    def test_install_root_of(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / "AzurPromilia_Game"
            game.mkdir()
            self.assertEqual(detect._install_root_of(game), root)
            self.assertEqual(detect._install_root_of(root), root)

    def test_installs_in_handles_missing_root(self):
        self.assertEqual(detect._installs_in(Path("C:/definitely/not/here")), [])


class PlanMoreTest(unittest.TestCase):
    def test_download_url_and_stats_render(self):
        base = {"files": {}}
        target = {"cdn_host": "https://cdn.invalid", "total_size": 3, "files": {
            "a.bin": {"size": 3, "md5": "x", "url": "/pkg/a.bin"},
            "b.bin": {"size": 5, "md5": "y", "url": "https://other.invalid/b"},
        }}
        with contextlib.redirect_stdout(io.StringIO()):
            rows, stats = plan.build(base, target)
        by_path = {r["path"]: r for r in rows}
        self.assertEqual(by_path["a.bin"]["src_or_url"], "https://cdn.invalid/pkg/a.bin")
        self.assertEqual(by_path["b.bin"]["src_or_url"], "https://other.invalid/b")
        self.assertEqual(stats.download_files, 2)
        self.assertEqual(stats.download_bytes, 8)
        text = stats.render(8)
        self.assertIn("下载 2 个", text)
        self.assertIn("新增占用", text)

    def test_link_local_reuses_matching_extra_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stash").mkdir()
            (root / "stash" / "a.bin").write_bytes(b"abc")
            base = {"files": {}}
            target = {"files": {"data/a.bin": {
                "size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72", "url": "a"}}}
            with contextlib.redirect_stdout(io.StringIO()):
                rows, stats = plan.build(base, target, local_dir=root)
            self.assertEqual(rows[0]["action"], "link_local")
            self.assertEqual(rows[0]["src_or_url"], "stash/a.bin")
            self.assertEqual(stats.reuse_files, 1)
            self.assertEqual(stats.download_files, 0)
            self.assertIn("复用 1 个", stats.render(3))

    def test_existing_same_size_is_ready_not_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp) / "game"
            (target_dir / "data").mkdir(parents=True)
            (target_dir / "data" / "a.bin").write_bytes(b"xyz")
            base = {"files": {"data/a.bin": {
                "size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72", "url": "a"}}}
            target = {"files": {"data/a.bin": {
                "size": 3, "md5": "d16fb36f0911f878998c136191af705e", "url": "a"}}}
            with contextlib.redirect_stdout(io.StringIO()):
                rows, stats = plan.build(base, target, existing=target_dir)
            self.assertEqual(rows[0]["action"], "ready")
            self.assertEqual(stats.ready_files, 1)
            self.assertEqual(stats.download_files, 0)
            self.assertIn("已就绪 1 个", stats.render(3))

            # 大小不符的文件仍要下载
            (target_dir / "data" / "a.bin").write_bytes(b"xy")
            with contextlib.redirect_stdout(io.StringIO()):
                rows, stats = plan.build(base, target, existing=target_dir)
            self.assertEqual(rows[0]["action"], "download")
            self.assertEqual(stats.ready_files, 0)

    def test_cross_kind_without_base_dir_downloads(self):
        base = {"hash_kind": "crc64", "files": {"a.bin": {"size": 3, "md5": "1", "url": "a"}}}
        target = {"files": {"a.bin": {"size": 3, "md5": "x", "url": "a"}}}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rows, stats = plan.build(base, target)
        self.assertEqual(rows[0]["action"], "download")
        self.assertIn("按下载处理", out.getvalue())

    def test_cross_kind_trust_names(self):
        base = {"hash_kind": "crc64", "files": {"a.bin": {"size": 3, "md5": "1", "url": "a"}}}
        target = {"files": {"a.bin": {"size": 3, "md5": "x", "url": "a"}}}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rows, stats = plan.build(base, target, trust_names=True)
        self.assertEqual(rows[0]["action"], "link")
        self.assertEqual(stats.link_files, 1)
        self.assertIn("未核对内容", out.getvalue())

    def test_cross_kind_crc64_target_uses_native(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.bin").write_bytes(b"abc")
            crc = hashes.crc64_of(root / "a.bin")
            base = {"files": {"a.bin": {"size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72",
                                        "url": "a"}}}
            target = {"hash_kind": "crc64", "files": {"a.bin": {"size": 3, "md5": crc, "url": "a"}}}
            with mock.patch.object(native, "crc64_for", return_value={"a.bin": int(crc)}):
                with contextlib.redirect_stdout(io.StringIO()):
                    rows, stats = plan.build(base, target, base_dir=root)
            self.assertEqual(rows[0]["action"], "link")
            self.assertEqual(rows[0]["hash_kind"], "crc64")
            # 7za 算不出来时不能误判成一致
            with mock.patch.object(native, "crc64_for",
                                   side_effect=native.NativeError("没有 7za")):
                with contextlib.redirect_stdout(io.StringIO()):
                    rows, _ = plan.build(base, target, base_dir=root)
            self.assertEqual(rows[0]["action"], "download")

    def test_size_mismatch_is_not_linked(self):
        base = {"files": {"a.bin": {"size": 9, "md5": "x", "url": "a"}}}
        target = {"files": {"a.bin": {"size": 3, "md5": "x", "url": "a"}}}
        with contextlib.redirect_stdout(io.StringIO()):
            rows, _ = plan.build(base, target)
        self.assertEqual(rows[0]["action"], "download")  # 同名不同大小不能链

    def test_as_int(self):
        self.assertEqual(plan._as_int("12"), 12)
        self.assertIsNone(plan._as_int("x"))
        self.assertIsNone(plan._as_int(None))


class CryptoOpensslTest(unittest.TestCase):
    def test_openssl_fallback_and_missing_backend(self):
        proc = mock.Mock(returncode=0, stdout=b"cipher")
        # sys.modules 里塞 None 会让 import Crypto 抛 ImportError，从而走 openssl
        with mock.patch.dict("sys.modules", {"Crypto": None, "Crypto.Cipher": None,
                                             "Crypto.Util.Padding": None}), \
             mock.patch.object(crypto.shutil, "which", return_value="openssl"), \
             mock.patch.object(crypto.subprocess, "run", return_value=proc) as run:
            self.assertEqual(crypto.encrypt(b"0" * 16, b"1" * 16, b"x"), b"cipher")
        self.assertIn("-aes-128-cbc", run.call_args.args[0])

    def test_openssl_missing_binary(self):
        with mock.patch.dict("sys.modules", {"Crypto": None, "Crypto.Cipher": None,
                                             "Crypto.Util.Padding": None}), \
             mock.patch.object(crypto.shutil, "which", return_value=None):
            with self.assertRaises(crypto.BackendMissing):
                crypto.encrypt(b"0" * 16, b"1" * 16, b"x")

    def test_openssl_nonzero_exit(self):
        proc = mock.Mock(returncode=1, stdout=b"", stderr=b"bad decrypt")
        with mock.patch.dict("sys.modules", {"Crypto": None, "Crypto.Cipher": None,
                                             "Crypto.Util.Padding": None}), \
             mock.patch.object(crypto.shutil, "which", return_value="openssl"), \
             mock.patch.object(crypto.subprocess, "run", return_value=proc):
            with self.assertRaises(RuntimeError) as ctx:
                crypto.decrypt(b"0" * 16, b"1" * 16, b"x")
        self.assertIn("bad decrypt", str(ctx.exception))


class QuickstartJpTest(unittest.TestCase):
    """日服目标：CRC64 跨区比对 + 目录名严格校验 + 写两个 json。"""

    def setUp(self):
        # 本机真开着游戏也不影响离线测试；真实进程检测另有 PreflightMoreTest
        game = mock.patch.object(cli.preflight, "running_games", return_value=[])
        game.start()
        self.addCleanup(game.stop)

    def _setup(self, root: Path):
        base_dir = root / "cn" / "AzurPromilia_Game"
        (base_dir / "AzurPromilia_Data").mkdir(parents=True)
        (base_dir / "AzurPromilia_Data" / "a.bin").write_bytes(b"abc")
        md5 = hashes.md5_of(base_dir / "AzurPromilia_Data" / "a.bin")
        crc = hashes.crc64_of(base_dir / "AzurPromilia_Data" / "a.bin")
        inst = detect.Install(root=root / "cn",
                              launcher_exe=root / "cn" / "1.0.0" / "launcher.exe",
                              game_dirs=[base_dir])
        ch = channels.Channel("354", "C000", "l1", "g1", secret="s", label="国服")
        base_man = {"label": "国服", "version_code": "100", "total_size": 3,
                    "cdn_host": "https://x",
                    "files": {"AzurPromilia_Data/a.bin": {"size": 3, "md5": md5, "url": "a"}}}
        jp_man = {"label": "日服", "version_code": "1.0.0", "basis": "/pkg/",
                  "game_folder_name": "AZUPRO_JP", "launch_entry": "AzurPromiliaJP.exe",
                  "launch_params": [], "uninstall_script": "", "hash_kind": "crc64",
                  "total_size": 3, "cdn_host": "https://cdn.invalid",
                  "path_map": [list(p) for p in yostar.PATH_MAP],
                  "files": {"AzurPromiliaJP_Data/a.bin": {"size": 3, "md5": crc, "url": "a"}}}
        return base_dir, inst, ch, base_man, jp_man, crc

    def _argv(self, root: Path, target_dir: Path) -> list[str]:
        return ["quickstart", "--base", "cn", "--target", "jp",
                "--target-dir", str(target_dir), "--out", str(root / "out"), "--yes"]

    def test_quickstart_to_jp_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _base, inst, ch, base_man, jp_man, crc = self._setup(root)
            target_dir = root / "jp" / "AZUPRO_JP"
            with mock.patch.object(cli.detect, "find_installs", return_value=[inst]), \
                 mock.patch.object(cli.channels, "load", return_value=ch), \
                 mock.patch.object(cli.manifest, "fetch", return_value=base_man), \
                 mock.patch.object(cli.yostar, "fetch", return_value=jp_man), \
                 mock.patch.object(cli.native, "find", return_value=Path("7za")), \
                 mock.patch.object(native, "crc64_for",
                                   side_effect=lambda b, rels, cache=None:
                                   {r: int(crc) for r in rels}), \
                 mock.patch.object(journal, "JOURNAL_DIR", root / "journal"), \
                 mock.patch.object(journal, "BACKUP_DIR", root / "backup"), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(self._argv(root, target_dir))
            self.assertEqual(code, 0, out.getvalue())
            self.assertEqual((target_dir / "AzurPromiliaJP_Data" / "a.bin").read_bytes(), b"abc")
            self.assertTrue((target_dir / "manifest.json").exists())
            self.assertTrue((target_dir / "game-launcher-config.json").exists())
            self.assertIn("校验方式不同", out.getvalue())

    def test_quickstart_jp_rejects_lowercase_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _base, inst, ch, base_man, jp_man, _crc = self._setup(root)
            with mock.patch.object(cli.detect, "find_installs", return_value=[inst]), \
                 mock.patch.object(cli.channels, "load", return_value=ch), \
                 mock.patch.object(cli.manifest, "fetch", return_value=base_man), \
                 mock.patch.object(cli.yostar, "fetch", return_value=jp_man), \
                 mock.patch.object(cli.native, "find", return_value=Path("7za")), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(self._argv(root, root / "jp" / "azupro_jp"))
            self.assertEqual(code, 1)
            self.assertIn("大小写也要一致", out.getvalue())

    def test_quickstart_jp_requires_7za(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _base, inst, ch, base_man, jp_man, _crc = self._setup(root)
            with mock.patch.object(cli.detect, "find_installs", return_value=[inst]), \
                 mock.patch.object(cli.channels, "load", return_value=ch), \
                 mock.patch.object(cli.manifest, "fetch", return_value=base_man), \
                 mock.patch.object(cli.yostar, "fetch", return_value=jp_man), \
                 mock.patch.object(cli.native, "find", return_value=None), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(self._argv(root, root / "jp" / "AZUPRO_JP"))
            self.assertEqual(code, 1)
            self.assertIn("7za", out.getvalue())


class CliDetectTest(unittest.TestCase):
    def test_detect_no_installs(self):
        with mock.patch.object(cli.detect, "find_installs", return_value=[]):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["detect"]), 1)
        self.assertIn("没有找到启动器", out.getvalue())

    def test_detect_prints_channels_and_yostar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cn = detect.Install(root=root / "cn",
                                launcher_exe=root / "cn" / "1.0.0" / "launcher.exe",
                                game_dirs=[root / "cn" / "AzurPromilia_Game"])
            jp = detect.Install(root=root / "jp",
                                launcher_exe=root / "jp" / "AZUPRO_Gamelauncher.exe",
                                kind="yostar")
            ch = channels.Channel("354", "C000", "l1", "g1", secret="s", label="国服",
                                  domains=("https://api-grp.manjuu.com",))
            with mock.patch.object(cli.detect, "find_installs", return_value=[cn, jp]), \
                 mock.patch.object(cli.channels, "load", return_value=ch), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["detect"]), 0)
            text = out.getvalue()
            self.assertIn("国服", text)
            self.assertIn("appId=354", text)
            self.assertIn("Yostar", text)

    def test_detect_channel_read_failure(self):
        inst = detect.Install(root=Path("C:/x"), launcher_exe=Path("C:/x/1/launcher.exe"))
        with mock.patch.object(cli.detect, "find_installs", return_value=[inst]), \
             mock.patch.object(cli.channels, "load",
                               side_effect=FileNotFoundError("找不到 app_config.dat")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["detect"]), 0)
        self.assertIn("渠道读取失败", out.getvalue())

    def test_region_hint_for_unknown_channel(self):
        unknown = channels.Channel("999", "X", "l", "g", secret="s")
        self.assertTrue(any("regions.json" in line for line in cli._region_hint(unknown)))
        known = channels.Channel("354", "C000", "l", "g", secret="s")
        self.assertEqual(cli._region_hint(known), [])


class CliRepairCommandTest(unittest.TestCase):
    def _manifest(self, root: Path) -> Path:
        man = root / "manifest.json"
        man.write_text(json.dumps({"files": {"a.bin": {
            "size": 3, "md5": "900150983cd24fb0d6963f7d28e17f72", "url": "a"}}}), "utf-8")
        return man

    def test_repair_command_relinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            base.mkdir()
            (base / "a.bin").write_bytes(b"abc")
            target = root / "target"
            target.mkdir()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["repair", "--target", str(target),
                                           "--manifest", str(self._manifest(root)),
                                           "--base", str(base), "--sample", "0"]), 0)
            self.assertEqual((target / "a.bin").read_bytes(), b"abc")
            self.assertIn("重建链接 1 个", out.getvalue())

    def test_repair_command_reports_download_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            with mock.patch.object(cli.fetch, "download_one", return_value=(False, "HTTP 404")):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = cli.main(["repair", "--target", str(target),
                                     "--manifest", str(self._manifest(root)),
                                     "--sample", "0", "--workers", "1"])
            self.assertEqual(code, 2)


class CliRollbackListTest(unittest.TestCase):
    def test_lists_available_journals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(journal, "JOURNAL_DIR", root / "journal"):
                entry = journal.Entry("link", root / "t")
                entry.add_created(root / "a.bin", 1)
                entry.save()
                (root / "journal" / "broken.json").write_text("{bad", "utf-8")
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.main(["rollback", "--id", "nope.json"]), 0)
            text = out.getvalue()
            self.assertIn("可回退的记录", text)
            self.assertIn("新建 1 个", text)


class RegionsMoreTest(unittest.TestCase):
    def test_broken_json_and_missing_regions(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", "utf-8")
            with self.assertRaises(ValueError) as ctx:
                regions.load(bad)
            self.assertIn("读不出来", str(ctx.exception))
            empty = Path(tmp) / "empty.json"
            empty.write_text("{}", "utf-8")
            with self.assertRaises(ValueError):
                regions.load(empty)
            notobj = Path(tmp) / "notobj.json"
            notobj.write_text(json.dumps({"regions": {"xx": "oops"}}), "utf-8")
            with self.assertRaises(ValueError):
                regions.load(notobj)

    def test_override_builtin_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "regions.json"
            path.write_text(json.dumps({"regions": {
                "cn": {"label": "国服（自定义）"},
                "new": {"app_id": "1"},
            }}), "utf-8")
            self.assertEqual(regions.find("cn", path).label, "国服（自定义）")
            self.assertEqual(regions.find("cn", path).app_id, "354")  # 未写字段继承内置
            new = regions.find("new", path)
            self.assertEqual(new.label, "new")
            self.assertFalse(new.ready)
            self.assertIsNone(regions.find("nope", path))

    def test_config_path_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "custom.json"
            with mock.patch.dict(os.environ, {regions.CONFIG_ENV: str(custom)}):
                self.assertEqual(regions.config_path(), custom)
            with mock.patch.dict(os.environ, {regions.CONFIG_ENV: ""}):
                self.assertEqual(regions.config_path().name, "regions.json")


class NativeFindTest(unittest.TestCase):
    def tearDown(self):
        native._CACHED = None
        native._LOOKED = False

    def test_find_prefers_env_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "7za.exe"
            exe.write_bytes(b"")
            with mock.patch.dict(os.environ, {"AZP_7ZA": str(exe)}):
                self.assertEqual(native.find(), exe)
                self.assertEqual(native.find(), exe)  # 第二次走缓存
            self.assertIsNotNone(native._CACHED)

    def test_find_looks_into_launcher_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = root / "1.0.0" / "tools" / "7za.exe"
            tool.parent.mkdir(parents=True)
            tool.write_bytes(b"")
            inst = detect.Install(root=root, launcher_exe=root / "1.0.0" / "launcher.exe")
            with mock.patch.dict(os.environ, {"AZP_7ZA": ""}), \
                 mock.patch.object(native, "bundled_7za", return_value=root / "nope.exe"), \
                 mock.patch.object(native.shutil, "which", return_value=None), \
                 mock.patch.object(detect, "find_installs", return_value=[inst]):
                self.assertEqual(native.find(), tool)

    def test_find_returns_none_when_nothing_exists(self):
        with mock.patch.dict(os.environ, {"AZP_7ZA": ""}), \
             mock.patch.object(native, "bundled_7za",
                               return_value=Path("_no_such_7za_.exe")), \
             mock.patch.object(native.shutil, "which", return_value=None), \
             mock.patch.object(detect, "find_installs", return_value=[]):
            self.assertIsNone(native.find())

    def test_load_tolerates_broken_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c.json"
            self.assertEqual(native._load(cache), {})
            cache.write_text("{bad", "utf-8")
            self.assertEqual(native._load(cache), {})
            cache.write_text("[1,2]", "utf-8")
            self.assertEqual(native._load(cache), {})


class RemoveTrashTest(unittest.TestCase):
    def _client(self, root: Path) -> Path:
        game = root / "AzurPromilia_TC_game"
        (game / "AzurPromilia_Data").mkdir(parents=True)
        (game / "a.bin").write_bytes(b"abc")
        return game

    def test_trash_success_returns_without_deleting_here(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._client(Path(tmp))
            with mock.patch.object(remove, "_to_recycle_bin", return_value=(True, "")) as trash:
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(remove.run(target, yes=True, trash=True), 0)
            trash.assert_called_once()
            self.assertIn("回收站", out.getvalue())

    def test_trash_failure_with_fallback_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._client(Path(tmp))
            with mock.patch.object(remove, "_to_recycle_bin", return_value=(False, "配额不够")):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(remove.run(target, yes=True, trash=True), 0)
            self.assertFalse(target.exists())  # 回退成直接删除
            self.assertIn("配额不够", out.getvalue())

    def test_trash_failure_without_fallback_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._client(Path(tmp))
            with mock.patch.object(remove, "_to_recycle_bin", return_value=(False, "配额不够")):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(remove.run(target, yes=True, trash=True, fallback=False), 3)
            self.assertTrue((target / "a.bin").exists())  # 一个文件都不能少

    def test_recycle_bin_on_non_windows(self):
        target = Path("C:/x")  # 先构造路径：mock 掉 os.name 后 Windows 上不能再 new Path
        with mock.patch.object(remove.os, "name", "posix"):
            ok, why = remove._to_recycle_bin(target)
        self.assertFalse(ok)
        self.assertIn("Windows", why)

    def test_delete_tree_reports_occupied_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._client(Path(tmp))
            with mock.patch.object(remove.os, "walk",
                                   return_value=[(str(target), [], ["a.bin"])]), \
                 mock.patch.object(Path, "unlink", side_effect=OSError("被占用")), \
                 mock.patch.object(Path, "chmod", side_effect=OSError("被占用")):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = remove.run(target, yes=True)
            self.assertEqual(code, 2)
            self.assertIn("占用", out.getvalue())

    def test_confirm_accepts_y(self):
        with mock.patch.object(remove.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value="y"):
            self.assertTrue(remove._confirm(Path("C:/x")))
        with mock.patch.object(remove.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value="n"):
            self.assertFalse(remove._confirm(Path("C:/x")))


class NofadeRelinkTest(unittest.TestCase):
    def _patch(self) -> nofade.Patch:
        return nofade.Patch(name="t", file="sub/test.r", size=3,
                            original_md5="900150983cd24fb0d6963f7d28e17f72",
                            patched_md5="x", resource_version="v")

    def test_relink_from_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "AzurPromilia_Game"
            src = base / "AzurPromilia_Data" / "sub" / "test.r"
            src.parent.mkdir(parents=True)
            src.write_bytes(b"abc")
            dest = root / "target" / "sub" / "test.r"
            dest.parent.mkdir(parents=True)
            dest.write_bytes(b"patched")
            self.assertTrue(nofade._relink(dest, base, self._patch()))
            self.assertEqual(dest.read_bytes(), b"abc")
            if os.name == "nt":
                self.assertEqual(dest.stat().st_nlink, 2)

    def test_relink_refuses_wrong_base_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "AzurPromilia_Game"
            src = base / "AzurPromilia_Data" / "sub" / "test.r"
            src.parent.mkdir(parents=True)
            src.write_bytes(b"different")  # 不是原版就不能拿它恢复
            dest = root / "target" / "test.r"
            dest.parent.mkdir(parents=True)
            dest.write_bytes(b"patched")
            self.assertFalse(nofade._relink(dest, base, self._patch()))
            self.assertEqual(dest.read_bytes(), b"patched")

    def test_relink_without_base_or_missing_file(self):
        self.assertFalse(nofade._relink(Path("x"), None, self._patch()))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(nofade._relink(Path(tmp) / "d", Path(tmp), self._patch()))


class FetchConnectionsTest(unittest.TestCase):
    def test_connections_are_cached_per_thread(self):
        pool = fetch.Connections()
        with mock.patch.object(fetch.http.client, "HTTPSConnection") as conn:
            first, path = pool.get("https://cdn.invalid/pkg/a.bin?x=1")
            second, _ = pool.get("https://cdn.invalid/pkg/b.bin")
            self.assertIs(first, second)  # 同 host 复用连接
            self.assertEqual(path, "/pkg/a.bin?x=1")
            self.assertEqual(conn.call_count, 1)
            pool.drop("https://cdn.invalid/pkg/a.bin")
            self.assertEqual(first.close.call_count, 1)
            pool.get("https://cdn.invalid/pkg/c.bin")
            self.assertEqual(conn.call_count, 2)

    def test_connections_reject_plain_http(self):
        with self.assertRaises(ValueError) as ctx:
            fetch.Connections().get("http://cdn.invalid/a")
        self.assertIn("只支持 https", str(ctx.exception))

    def test_http_416_resets_partial_file(self):
        pool = mock.Mock()
        pool.get.return_value = (pool, "u")
        resp = mock.Mock()
        resp.status = 416
        resp.read.return_value = b""
        pool.getresponse.return_value = resp
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            part = dest.with_suffix(".bin.part")
            part.write_bytes(b"stale")
            with mock.patch.object(fetch.time, "sleep"):
                ok, msg = fetch.download_one(pool, "https://x/a", dest, 16, "0" * 32, retries=1)
            self.assertFalse(ok)
            self.assertIn("416", msg)
            self.assertFalse(part.exists())  # 越界的分片必须清掉

    def test_match_crc64_uses_native_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(b"abc")
            with mock.patch.object(native, "crc64_file", return_value=123):
                self.assertTrue(fetch._match(path, "123", hashes.CRC64))
                self.assertFalse(fetch._match(path, "124", hashes.CRC64))
            with mock.patch.object(native, "crc64_file", return_value=None):
                self.assertTrue(fetch._match(path, hashes.crc64_of(path), hashes.CRC64))

    def test_run_collects_failures(self):
        row = {"action": "download", "size": 3, "md5": "x", "path": "a.bin",
               "src_or_url": "https://x/a", "hash_kind": "md5"}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(fetch, "download_one", return_value=(False, "HTTP 404")):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    stats = fetch.run([row], Path(tmp), workers=1)
            self.assertEqual(stats.failed, [("a.bin", "HTTP 404")])
            self.assertIn("失败 a.bin", out.getvalue())


class ExportMoreTest(unittest.TestCase):
    ROW = {"action": "download", "size": 3, "md5": "m", "path": "a.bin",
           "src_or_url": "u", "hash_kind": "md5"}

    def test_incremental_hardlink_and_include_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            source.mkdir()
            (source / "a.bin").write_bytes(b"abc")
            (source / "l.bin").write_bytes(b"xyz")
            linked = dict(self.ROW, action="link", path="l.bin", size=3)
            dest = root / "dest"
            stats = export.run([self.ROW, linked], source, dest,
                               include_links=True, hardlink=True)
            self.assertEqual(stats["copied"], 2)
            self.assertEqual(stats["missing"], [])
            if os.name == "nt":
                self.assertEqual((dest / "a.bin").stat().st_nlink, 2)
            # 不含 link 时只导 download
            only = export.run([self.ROW, linked], source, root / "d2")
            self.assertEqual(only["files"], 1)

    def test_hardlink_falls_back_to_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            source.mkdir()
            (source / "a.bin").write_bytes(b"abc")
            with mock.patch.object(export.os, "link", side_effect=OSError("跨卷")):
                stats = export.run([self.ROW], source, root / "dest", hardlink=True)
            self.assertEqual(stats["copied"], 1)
            self.assertEqual(stats["fell_back"], 1)
            self.assertEqual((root / "dest" / "a.bin").read_bytes(), b"abc")


class PreflightMoreTest(unittest.TestCase):
    def test_running_games_parses_tasklist(self):
        out = ('"AzurPromilia.exe","1234","Console","1","100,000 K"\r\n'
               '"notepad.exe","222","Console","1","10,000 K"\r\n')
        proc = mock.Mock(stdout=out)
        with mock.patch.object(preflight.os, "name", "nt"), \
             mock.patch("subprocess.run", return_value=proc):
            self.assertEqual(preflight.running_games(), ["azurpromilia.exe"])

    def test_running_games_tolerates_failure(self):
        with mock.patch.object(preflight.os, "name", "nt"), \
             mock.patch("subprocess.run", side_effect=OSError("no tasklist")):
            self.assertEqual(preflight.running_games(), [])
        with mock.patch.object(preflight.os, "name", "posix"):
            self.assertEqual(preflight.running_games(), [])

    def test_volume_info_off_windows(self):
        path = Path("/tmp")  # 同上：先把路径建好再 mock 平台
        with mock.patch.object(preflight.os, "name", "posix"):
            self.assertEqual(preflight.volume_info(path), ("", ""))
            self.assertIsNone(preflight.long_paths_enabled())

    def test_aes_item_reports_missing_backend(self):
        with mock.patch.dict("sys.modules", {"Crypto": None}), \
             mock.patch.object(preflight.shutil, "which", return_value=None):
            item = preflight._aes_item()
        self.assertEqual(item.status, preflight.FAIL)
        self.assertIn("openssl", item.detail)

    def test_probe_root_walks_up_to_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "a" / "b" / "c"
            self.assertEqual(preflight._probe_root(deep), root)


class JournalBackupTest(unittest.TestCase):
    def test_backup_name_collision_gets_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            original = target / "_index" / "version"
            original.parent.mkdir()
            original.write_bytes(b"v1")
            with mock.patch.object(journal, "BACKUP_DIR", root / "backup"):
                entry = journal.Entry("finalize", target)
                entry.backup(original)
                original.write_bytes(b"v2")
                entry.backup(original)  # 同名再备一次，不能覆盖前一份
                self.assertEqual(len(entry.backups), 2)
                saved = [Path(b["saved"]) for b in entry.backups]
                self.assertNotEqual(saved[0], saved[1])
                self.assertEqual(saved[0].read_bytes(), b"v1")
                self.assertEqual(saved[1].read_bytes(), b"v2")
                # 不存在的文件不用备
                entry.backup(target / "nope")
                self.assertEqual(len(entry.backups), 2)


class AskTest(unittest.TestCase):
    def test_ask_returns_default_on_empty(self):
        with mock.patch("builtins.input", return_value="  "):
            self.assertEqual(cli._ask("选", "1"), "1")
        with mock.patch("builtins.input", return_value="3"):
            self.assertEqual(cli._ask("选", "1"), "3")

    def test_ask_eof_cancels(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(SystemExit):
                cli._ask("选")

    def test_ask_yes(self):
        with mock.patch("builtins.input", return_value="y"):
            self.assertTrue(cli._ask_yes("确认"))
        with mock.patch("builtins.input", return_value=""):
            self.assertFalse(cli._ask_yes("确认"))
            self.assertTrue(cli._ask_yes("确认", default=True))


class SecurityGuardTest(unittest.TestCase):
    """每个能拿到远端/可编辑数据的入口，都要各自挡住越界路径。"""

    def test_plan_rejects_escape_in_base_manifest(self):
        base = {"files": {"../read-outside": {"size": 1, "md5": "x", "url": "a"}}}
        with self.assertRaises(ValueError):
            plan.build(base, {"files": {}})

    def test_repair_rejects_escape_before_touching_disk(self):
        man = {"files": {"../../victim": {"size": 1, "md5": "x", "url": "a"}}}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                repair.scan(Path(tmp), man)

    def test_parse_index_rejects_escape(self):
        with self.assertRaises(ValueError):
            manifest.parse_index("0,1,md5,../../evil,u\n", 2)

    def test_yostar_fetch_rejects_escape(self):
        raw = {"source": "/pkg/", "file": [{"path": "/../../evil", "size": 1, "hash": "1"}]}
        cfg = {"version": "1", "basis": "/pkg/", "entry": "e", "params": [], "uninstall": "",
               "size_text": "", "cdn": "https://c", "cdn_backup": ""}
        resp = mock.Mock()
        resp.read.return_value = json.dumps(raw).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(yostar, "game_config", return_value=cfg), \
                 mock.patch.object(yostar, "manifest_url", return_value="https://c/m"), \
                 mock.patch("urllib.request.urlopen", return_value=resp):
                with self.assertRaises(ValueError):
                    yostar.fetch(Path(tmp), force=True)

    def test_indexfile_rejects_bad_version_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "index.csv"
            csv_path.write_text("1,2,3\n", "utf-8")
            for bad in ("../100", "a/b", "..", ""):
                with self.assertRaises(ValueError, msg=bad):
                    indexfile.write(root / "game", bad, "1.0.0", csv_path)
            self.assertFalse((root / "game" / "_index").exists())

    def test_nofade_rejects_escaping_patch_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "patch.json").write_text(json.dumps({
                "file": "../outside.r", "size": 1,
                "original_md5": "x", "patched_md5": "y"}), "utf-8")
            with mock.patch.object(nofade, "PATCH_DIR", root):
                with self.assertRaises(ValueError):
                    nofade.load()

    def test_journal_id_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(journal, "JOURNAL_DIR", Path(tmp) / "journal"):
                for bad in ("../x.json", "..\\x.json", "a/b.json", ".."):
                    self.assertIsNone(journal.find(None, bad), msg=bad)

    def test_rollback_refuses_to_restore_outside_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            saved = root / "saved"
            saved.write_bytes(b"payload")
            victim = root / "victim"
            victim.write_bytes(b"original")
            book = root / "entry.json"
            book.write_text(json.dumps({
                "cmd": "t", "target": str(target), "time": time.time(),
                "created": [], "backups": [{"orig": str(victim), "saved": str(saved)}],
            }), "utf-8")
            removed, restored, skipped, notes = journal.rollback(book)
            self.assertEqual(restored, 0)
            self.assertEqual(skipped, 1)
            self.assertEqual(victim.read_bytes(), b"original")  # 目录外一个字节都不能动
            self.assertTrue(any("不在目标目录内" in n for n in notes))

    def test_http_get_bytes_rejects_non_http_scheme(self):
        from azp.api import ApiError, http_get_bytes
        for url in ("file:///etc/passwd", "ftp://x/a", "\\\\host\\share\\a"):
            with self.assertRaises(ApiError, msg=url):
                http_get_bytes(url)


class RegressionGuardTest(unittest.TestCase):
    """本轮修复的防回归用例：都是离线临时目录，不碰真实客户端。"""

    def test_preflight_never_writes_source_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            base.mkdir()
            target = root / "target"
            with mock.patch.object(preflight, "tempfile") as fake:
                preflight.run(base, target)
            for call in fake.TemporaryDirectory.call_args_list:
                self.assertNotEqual(Path(call.kwargs.get("dir", "")).resolve(),
                                    base.resolve())
            self.assertEqual(list(base.iterdir()), [])

    def test_dry_run_skips_hardlink_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            base.mkdir()
            target = root / "target"
            target.mkdir()
            before = set(target.iterdir())
            with mock.patch.object(
                preflight, "hardlink_probe_same_volume",
                return_value=(True, "卷支持硬链接（执行时确认目标目录权限）"),
            ) as volume_probe:
                preflight.run(base, target, probe_links=False)
            # 计划阶段不写目标目录，只在同卷临时目录做卷级预检
            volume_probe.assert_called_once()
            self.assertEqual(set(target.iterdir()), before)

    def test_zero_byte_file_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "empty.bin"
            pool = _FakePool(b"")
            ok, msg = fetch.download_one(pool, "https://x/empty", dest, 0,
                                         hashlib.md5(b"").hexdigest())
            self.assertTrue(ok, msg)
            self.assertEqual(dest.read_bytes(), b"")
            self.assertEqual(len(pool.ranges), 1)

    def test_zero_byte_file_with_bad_part_still_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "empty.bin"
            dest.with_suffix(".bin.part").write_bytes(b"junk")
            pool = _FakePool(b"")
            ok, msg = fetch.download_one(pool, "https://x/empty", dest, 0,
                                         hashlib.md5(b"").hexdigest())
            self.assertTrue(ok, msg)
            self.assertEqual(dest.read_bytes(), b"")

    def test_remove_resolves_edge_paths_before_guarding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outer = root / "outer"
            client = outer / "AzurPromilia_TC_game"
            (client / "AzurPromilia_Data").mkdir(parents=True)
            (outer / "keep.bin").write_bytes(b"keep")
            # '..' 会先被展平成外层目录，所以必须按展平后的路径判断
            self.assertIn("不像客户端目录",
                          remove.check_target(client / "..", None, False))
            # 单段相对路径不能因为 parts 少就被当成盘符根
            rel = client.relative_to(Path.cwd()) if client.is_relative_to(Path.cwd()) else None
            if rel is not None:
                self.assertEqual(remove.check_target(rel, None, True), "")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(remove.run(client / "..", yes=True), 1)
            self.assertTrue((outer / "keep.bin").exists())
            self.assertTrue((client / "AzurPromilia_Data").exists())

    @unittest.skipUnless(os.name == "nt", "junction 只在 Windows 上有")
    def test_remove_does_not_follow_junction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            (outside / "sentinel.bin").write_bytes(b"do not delete")
            client = root / "AzurPromilia_TC_game"
            (client / "AzurPromilia_Data").mkdir(parents=True)
            (client / "own.bin").write_bytes(b"own")
            target = client / "AzurPromilia_Data" / "link"
            made = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target), str(outside)],
                capture_output=True, text=True,
            )
            if made.returncode != 0:
                self.skipTest(f"本机无法建 junction：{made.stderr or made.stdout}")
            self.assertTrue(remove._is_reparse_point(target))
            with contextlib.redirect_stdout(io.StringIO()):
                remove.run(client, yes=True)
            self.assertFalse(client.exists())
            self.assertEqual((outside / "sentinel.bin").read_bytes(), b"do not delete")

    def test_fetch_saves_journal_when_interrupted(self):
        row = {"action": "download", "size": 3, "md5": "x", "path": "a.bin",
               "src_or_url": "https://x/a", "hash_kind": "md5"}
        started = threading.Event()

        def boom(*_args, **_kwargs):
            started.set()
            return False, "down"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = journal.Entry("fetch", root / "target")
            entry.add_created(root / "target" / "done.bin", 3)
            with mock.patch.object(journal, "JOURNAL_DIR", root / "journal"), \
                 mock.patch.object(journal, "BACKUP_DIR", root / "backup"), \
                 mock.patch.object(fetch, "download_one", side_effect=boom), \
                 contextlib.redirect_stdout(io.StringIO()):
                stats = fetch.run([row], root / "target", workers=1, journal=entry)
                self.assertTrue(started.is_set())
                self.assertEqual(len(stats.failed), 1)
                # 没等到零头周期也要落一次账本，否则中断后就找不回这批文件
                self.assertEqual(len(list((root / "journal").glob("*.json"))), 1)
                self.assertEqual(len(journal.available(root / "target")), 1)

    def test_crc_cache_is_namespaced_by_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("a", "b"):
                (root / name / "d").mkdir(parents=True)
                (root / name / "d" / "x.bin").write_bytes(b"abc")
            cache = root / "cache.json"
            stamp = (root / "a" / "d" / "x.bin").stat()
            os.utime(root / "b" / "d" / "x.bin", ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            exe = Path("7za")
            with mock.patch.object(native, "crc64_dir", return_value={"x.bin": 111}):
                first = native.crc64_for(root / "a", ["d/x.bin"], cache, exe=exe)
            with mock.patch.object(native, "crc64_dir",
                                   return_value={"x.bin": 222}) as again:
                second = native.crc64_for(root / "b", ["d/x.bin"], cache, exe=exe)
            self.assertEqual(first, {"d/x.bin": 111})
            self.assertEqual(second, {"d/x.bin": 222})
            again.assert_called_once()

    def test_quickstart_reinstall_deletes_only_after_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_dir = root / "cn" / "AzurPromilia_Game"
            (base_dir / "AzurPromilia_Data").mkdir(parents=True)
            (base_dir / "a.bin").write_bytes(b"abc")
            md5 = hashes.md5_of(base_dir / "a.bin")
            target_dir = root / "global" / "AzurPromilia_TC_game"
            target_dir.mkdir(parents=True)
            (target_dir / "old.bin").write_bytes(b"old")
            inst = detect.Install(root=root / "cn",
                                  launcher_exe=root / "cn" / "1.0.0" / "launcher.exe",
                                  game_dirs=[base_dir])
            global_inst = detect.Install(
                root=root / "global",
                launcher_exe=root / "global" / "1.0.0" / "launcher.exe")
            ch = channels.Channel("354", "C000", "l1", "g1", secret="s", label="国服")
            global_ch = channels.Channel("434", "C500", "l2", "g2", secret="s",
                                         label="台服")

            def fake_load(exe):
                return ch if "cn" in {p.name for p in Path(exe).parents} else global_ch

            def fake_fetch(api, out_dir, force=False):
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "index.csv").write_text(f"0,0,3,{md5},a.bin,u\n", "utf-8")
                return {"label": "国服", "version_code": "100", "version_name": "1.0.0",
                        "game_folder_name": "AzurPromilia_TC_game", "total_size": 3,
                        "cdn_host": "https://x",
                        "files": {"a.bin": {"size": 3, "md5": md5, "url": "a"}}}

            calls: list[str] = []

            real_preflight = preflight.run

            def spy_remove(target, base=None, yes=False, dry=False, force=False,
                           trash=False, fallback=True):
                calls.append("remove")
                self.assertTrue(trash)
                self.assertTrue(yes)
                # 临时目录在部分系统上进不了回收站，这里只验证顺序和参数，真删逻辑另有用例
                shutil.rmtree(target, ignore_errors=True)
                return 0

            def spy_preflight(*args, **kwargs):
                calls.append("preflight")
                return real_preflight(*args, **kwargs)

            argv = ["quickstart", "--base", "cn", "--target", "global",
                    "--target-dir", str(target_dir), "--out", str(root / "out"),
                    "--reinstall", "--yes"]
            with mock.patch.object(cli.detect, "find_installs",
                                   return_value=[inst, global_inst]), \
                 mock.patch.object(cli.channels, "load", side_effect=fake_load), \
                 mock.patch.object(cli.manifest, "fetch", side_effect=fake_fetch), \
                 mock.patch.object(cli.preflight, "running_games", return_value=[]), \
                 mock.patch.object(cli.remove, "run", side_effect=spy_remove), \
                 mock.patch.object(cli.preflight, "run", side_effect=spy_preflight), \
                 mock.patch.object(journal, "JOURNAL_DIR", root / "journal"), \
                 mock.patch.object(journal, "BACKUP_DIR", root / "backup"), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(argv)
            self.assertEqual(code, 0, out.getvalue())
            self.assertEqual(calls.index("preflight") < calls.index("remove"), True)
            self.assertEqual((target_dir / "a.bin").read_bytes(), b"abc")
            self.assertFalse((target_dir / "old.bin").exists())

    def test_quickstart_reinstall_cancel_keeps_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_dir = root / "cn" / "AzurPromilia_Game"
            (base_dir / "AzurPromilia_Data").mkdir(parents=True)
            (base_dir / "a.bin").write_bytes(b"abc")
            md5 = hashes.md5_of(base_dir / "a.bin")
            target_dir = root / "global" / "AzurPromilia_TC_game"
            target_dir.mkdir(parents=True)
            (target_dir / "old.bin").write_bytes(b"old")
            inst = detect.Install(root=root / "cn",
                                  launcher_exe=root / "cn" / "1.0.0" / "launcher.exe",
                                  game_dirs=[base_dir])
            global_inst = detect.Install(
                root=root / "global",
                launcher_exe=root / "global" / "1.0.0" / "launcher.exe")
            ch = channels.Channel("354", "C000", "l1", "g1", secret="s", label="国服")
            global_ch = channels.Channel("434", "C500", "l2", "g2", secret="s",
                                         label="台服")

            def fake_load(exe):
                return ch if "cn" in {p.name for p in Path(exe).parents} else global_ch

            def fake_fetch(api, out_dir, force=False):
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "index.csv").write_text(f"0,0,3,{md5},a.bin,u\n", "utf-8")
                return {"label": "国服", "version_code": "100", "version_name": "1.0.0",
                        "game_folder_name": "AzurPromilia_TC_game", "total_size": 3,
                        "cdn_host": "https://x",
                        "files": {"a.bin": {"size": 3, "md5": md5, "url": "a"}}}

            argv = ["quickstart", "--base", "cn", "--target", "global",
                    "--target-dir", str(target_dir), "--out", str(root / "out"),
                    "--reinstall"]
            with mock.patch.object(cli.detect, "find_installs",
                                   return_value=[inst, global_inst]), \
                 mock.patch.object(cli.channels, "load", side_effect=fake_load), \
                 mock.patch.object(cli.manifest, "fetch", side_effect=fake_fetch), \
                 mock.patch.object(cli.preflight, "running_games", return_value=[]), \
                 mock.patch.object(cli.remove, "run") as fake, \
                 mock.patch.object(cli, "_ask_yes", return_value=False), \
                 mock.patch.object(journal, "JOURNAL_DIR", root / "journal"), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(argv)
            self.assertEqual(code, 0, out.getvalue())
            fake.assert_not_called()  # 最终确认取消，就不能清空
            self.assertEqual((target_dir / "old.bin").read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
