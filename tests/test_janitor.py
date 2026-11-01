"""本地垃圾清理（bridge/janitor.py）。

这个模块删的是用户磁盘上的文件，所以测试的重点不是「能不能删掉」，
而是**「会不会多删」**：业务数据库、收下来的附件、还没发出去的 outbox，
一条都不许被误伤。下面每条断言都对应一个真实存在的误删路径。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bridge import janitor  # noqa: E402

DAY = 86400.0


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "data"
        self.root.mkdir()
        self.now = time.time()

    def tearDown(self):
        self._td.cleanup()

    # -- 造数据 ------------------------------------------------------
    def make(self, rel: str, *, days_old: float = 0.0, size: int = 10) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        ts = self.now - days_old * DAY
        os.utime(p, (ts, ts))
        return p


class ScanTests(_Tmp):
    def test_picks_old_table_png_and_keeps_fresh(self):
        old = self.make("table-png/table-old.png", days_old=9, size=1000)
        new = self.make("table-png/table-new.png", days_old=1, size=2000)
        plan = janitor.scan(self.root, now=self.now)
        got = {it.path for it in plan.items}
        self.assertIn(old, got)
        self.assertNotIn(new, got, "6 天前的图不该删 —— 回查最近几天是刚需")

    def test_logs_respect_seven_days(self):
        old = self.make("logs/gui-20261001.log", days_old=15)
        new = self.make("logs/gui-20261101.log", days_old=0)
        plan = janitor.scan(self.root, now=self.now)
        got = {it.path for it in plan.items}
        self.assertIn(old, got)
        self.assertNotIn(new, got)

    def test_debug_dumps_only_at_root_level(self):
        old = self.make("screen-20260101_010101.xml", days_old=30)
        # 子目录里同名文件不属于「调试转储」规则（根目录规则只看一层）
        nested = self.make("attachments/screen-20260101_010101.xml", days_old=30)
        plan = janitor.scan(self.root, now=self.now)
        got = {it.path for it in plan.items}
        self.assertIn(old, got)
        self.assertNotIn(nested, got)

    def test_backup_keeps_thirty_days(self):
        old = self.make("backup/config-20260901-000000.yaml", days_old=40)
        mid = self.make("backup/config-20261025-000000.yaml", days_old=10)
        plan = janitor.scan(self.root, now=self.now)
        got = {it.path for it in plan.items}
        self.assertIn(old, got)
        self.assertNotIn(mid, got, "配置备份保留 30 天，10 天前的还在窗口内")

    def test_root_config_backup(self):
        old = self.make("config.yaml.bak_174035", days_old=60)
        plan = janitor.scan(self.root, now=self.now)
        self.assertIn(old, {it.path for it in plan.items})

    def test_future_mtime_is_never_deleted(self):
        # 手机拍的图、同步盘下来的文件经常带未来时间戳，绝不能按「很老」处理
        weird = self.make("table-png/table-future.png")
        ts = self.now + 5 * DAY
        os.utime(weird, (ts, ts))
        plan = janitor.scan(self.root, now=self.now)
        self.assertNotIn(weird, {it.path for it in plan.items})

    def test_keep_days_override(self):
        p = self.make("logs/a.log", days_old=3)
        self.assertNotIn(p, {it.path for it in janitor.scan(self.root, now=self.now).items})
        tight = janitor.scan(self.root, keep_days=1, now=self.now)
        self.assertIn(p, {it.path for it in tight.items})

    def test_missing_dir_is_empty_plan(self):
        plan = janitor.scan(self.root / "nope", now=self.now)
        self.assertEqual(plan.total_count, 0)
        self.assertEqual(plan.total_size, 0)

    def test_summary_lines(self):
        self.make("table-png/a.png", days_old=9, size=1024)
        plan = janitor.scan(self.root, now=self.now)
        text = "\n".join(janitor.summary_lines(plan))
        self.assertIn("表格图片", text)
        self.assertIn("合计", text)


class ProtectedTests(_Tmp):
    """宁可少删，不可错删 —— 这些文件在任何情况下都不许出现在计划里。"""

    def test_business_files_never_selected(self):
        guarded = [
            "bridge.db",
            "bridge.db-wal",
            "bridge.db-shm",
            "config.yaml",
            "attachments/export.xlsx",
            "attachments/old_export.xlsx",
            "outbox/pending.xlsx",
            "logs/bridge.db",              # 名字撞上保护名单，即使在 logs 下也不删
        ]
        for rel in guarded:
            self.make(rel, days_old=365)
        plan = janitor.scan(self.root, now=self.now)
        picked = {it.path.relative_to(self.root).as_posix() for it in plan.items}
        for rel in guarded:
            self.assertNotIn(rel, picked, f"{rel} 被误列入清理计划")

    def test_protected_dirs_untouched_after_execute(self):
        self.make("TABLE-png/keep.txt", days_old=365)  # 大小写不同的干扰项
        db = self.make("bridge.db", days_old=999, size=500)
        att = self.make("attachments/export.xlsx", days_old=999, size=500)
        out = self.make("outbox/pending.xlsx", days_old=999, size=500)
        janitor.execute(janitor.scan(self.root, now=self.now))
        for p in (db, att, out):
            self.assertTrue(p.is_file(), f"{p.name} 被删了")

    def test_symlink_escape_blocked(self):
        """data/logs 被换成指向外部目录的符号链接时，不许删到外面去。"""
        outside = Path(self._td.name) / "outside"
        outside.mkdir()
        victim = outside / "important.log"
        victim.write_bytes(b"y" * 10)
        os.utime(victim, (self.now - 30 * DAY,) * 2)
        link = self.root / "logs"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("当前环境不支持创建符号链接")
        plan = janitor.scan(self.root, now=self.now)
        self.assertEqual(plan.total_count, 0, "顺着符号链接删到 data 外面了")
        self.assertTrue(victim.is_file())


class ExecuteTests(_Tmp):
    def test_deletes_and_reports_size(self):
        a = self.make("table-png/a.png", days_old=9, size=2048)
        b = self.make("logs/x.log", days_old=9, size=1024)
        plan = janitor.scan(self.root, now=self.now)
        res = janitor.execute(plan)
        self.assertEqual(res.deleted, 2)
        self.assertEqual(res.freed, 3072)
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())
        self.assertEqual(res.failed, [])

    def test_locked_file_is_reported_not_fatal(self):
        a = self.make("logs/locked.log", days_old=9)
        b = self.make("logs/free.log", days_old=9)
        plan = janitor.scan(self.root, now=self.now)
        # 模拟「log 正被 worker 占着」：Windows 上独占打开后再删会 PermissionError
        fh = None
        try:
            fh = open(a, "rb")
            if os.name != "nt":
                self.skipTest("这个用例依赖 Windows 的独占语义")
            res = janitor.execute(plan)
        finally:
            if fh:
                fh.close()
        if os.name == "nt":
            self.assertFalse(b.exists(), "一个删不掉不该拖累其它文件")
            self.assertEqual(res.deleted + len(res.failed), 2)
            if res.failed:
                self.assertEqual(res.failed[0][0], a)

    def test_missing_file_between_scan_and_delete(self):
        a = self.make("table-png/gone.png", days_old=9)
        plan = janitor.scan(self.root, now=self.now)
        a.unlink()                     # 扫描之后、删除之前被别人删了
        res = janitor.execute(plan)
        self.assertEqual(res.deleted, 0)
        self.assertEqual(res.failed, [], "文件已不存在不算失败")


class SizeTests(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(janitor.human_size(0), "0 B")
        self.assertEqual(janitor.human_size(999), "999 B")
        self.assertEqual(janitor.human_size(1024), "1.0 KB")
        self.assertEqual(janitor.human_size(35 * 1024 * 1024), "35.0 MB")


if __name__ == "__main__":
    unittest.main(verbosity=2)
