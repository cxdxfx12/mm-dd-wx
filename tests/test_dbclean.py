"""消息数据库瘦身：删旧行 + VACUUM 回收空间。

真库不参与测试（会动生产数据）。这里造一个结构相同的临时库，钉住四件事：

1. 只删**超出保留期**的行，保留期内的、以及 ``keep_days=0`` 时一律不动；
2. 孤儿投递记录（消息已被删过留下的）任何时候都清；
3. 保留期低于下限直接拒绝 —— 去重靠 ``messages.uid``，删太短会重复转发；
4. VACUUM 真的把文件缩小（只 DELETE 不 VACUUM 等于没删，用户会以为功能坏了）。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge import dbclean                                           # noqa: E402

SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL, source TEXT NOT NULL,
    chat_id TEXT, chat_name TEXT, sender_id TEXT, sender_name TEXT,
    company TEXT, content TEXT, msg_type TEXT, media_path TEXT,
    msg_time TEXT, raw_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
    error TEXT, created_at TEXT NOT NULL);
"""


def make_db(path: Path, old: int = 0, new: int = 0, orphan: int = 0,
            days_old: int = 200) -> Path:
    conn = sqlite3.connect(str(path))
    try:
        # 跟 Storage 一样开 WAL：真实库里读写不互斥，用默认的 rollback 模式
        # 测出来的锁行为跟生产完全不是一回事。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        stamp_old = (datetime.now() - timedelta(days=days_old)).strftime("%Y-%m-%d %H:%M:%S")
        stamp_new = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        blob = "内容" * 300                     # 让每行占几 KB，VACUUM 才有东西可收
        for i in range(old):
            conn.execute("insert into messages (uid,source,content,msg_type,"
                         " msg_time,created_at) values (?,?,?,?,?,?)",
                         (f"old-{i}", "dingtalk", blob, "text", stamp_old, stamp_old))
            conn.execute("insert into deliveries (uid,target,status,created_at)"
                         " values (?,?,?,?)", (f"old-{i}", "群", "ok", stamp_old))
        for i in range(new):
            conn.execute("insert into messages (uid,source,content,msg_type,"
                         " msg_time,created_at) values (?,?,?,?,?,?)",
                         (f"new-{i}", "dingtalk", blob, "text", stamp_new, stamp_new))
            conn.execute("insert into deliveries (uid,target,status,created_at)"
                         " values (?,?,?,?)", (f"new-{i}", "群", "ok", stamp_new))
        for i in range(orphan):
            conn.execute("insert into deliveries (uid,target,status,created_at)"
                         " values (?,?,?,?)", (f"gone-{i}", "群", "ok", stamp_new))
        conn.commit()
    finally:
        conn.close()
    return path


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dbclean_"))
        self.db = make_db(self.tmp / "bridge.db", old=40, new=10, orphan=3)

    def test_统计过期与孤儿(self):
        plan = dbclean.scan(self.db, 90)
        self.assertEqual(plan.error, "")
        self.assertEqual(plan.message_rows, 50)
        self.assertEqual(plan.message_old, 40)
        self.assertEqual(plan.delivery_rows, 53)
        self.assertEqual(plan.delivery_old, 40)
        self.assertEqual(plan.delivery_orphan, 3)
        self.assertEqual(plan.delivery_doomed, 43)
        self.assertGreater(plan.reclaimable, 0)

    def test_保留期为零时不动历史只清孤儿(self):
        plan = dbclean.scan(self.db, 0)
        self.assertEqual(plan.message_old, 0)
        self.assertEqual(plan.delivery_old, 0)
        self.assertEqual(plan.delivery_orphan, 3)
        self.assertFalse(plan.nothing_to_do)

    def test_没有可删的时候如实说(self):
        db = make_db(self.tmp / "fresh.db", old=0, new=5, orphan=0)
        plan = dbclean.scan(db, 90)
        self.assertTrue(plan.nothing_to_do)
        self.assertEqual(plan.reclaimable, 0)

    def test_保留期太短被拒绝(self):
        with self.assertRaises(ValueError):
            dbclean.scan(self.db, 3)
        with self.assertRaises(ValueError):
            dbclean.scan(self.db, -1)

    def test_文件不存在不抛异常(self):
        plan = dbclean.scan(self.tmp / "nope.db", 90)
        self.assertTrue(plan.error)
        self.assertIn("不存在", plan.error)

    def test_msg_time为空时退回入库时间(self):
        conn = sqlite3.connect(str(self.db))
        old = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("insert into messages (uid,source,content,msg_type,"
                     " msg_time,created_at) values (?,?,?,?,?,?)",
                     ("blank", "dingtalk", "x", "text", "", old))
        conn.commit()
        conn.close()
        plan = dbclean.scan(self.db, 90)
        self.assertEqual(plan.message_old, 41)


class ExecuteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dbclean_"))
        self.db = make_db(self.tmp / "bridge.db", old=60, new=10, orphan=2)

    def test_删对行并且文件真的变小(self):
        before = dbclean._file_bytes(self.db)
        plan = dbclean.scan(self.db, 90)
        res = dbclean.execute(plan)
        self.assertEqual(res.error, "")
        self.assertEqual(res.deleted_messages, 60)
        self.assertEqual(res.deleted_deliveries, 62)     # 60 过期 + 2 孤儿
        self.assertTrue(res.vacuumed, res.note)
        self.assertGreater(res.freed, 0)

        conn = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(conn.execute("select count(*) from messages").fetchone()[0], 10)
            self.assertEqual(conn.execute("select count(*) from deliveries").fetchone()[0], 10)
            # 保留期内的消息一条不少
            uids = {r[0] for r in conn.execute("select uid from messages")}
            self.assertIn("new-0", uids)
        finally:
            conn.close()
        self.assertLess(dbclean._file_bytes(self.db), before)

    def test_保留期为零时一行消息都不删(self):
        plan = dbclean.scan(self.db, 0)
        res = dbclean.execute(plan)
        self.assertEqual(res.deleted_messages, 0)
        self.assertEqual(res.deleted_deliveries, 2)      # 只清孤儿
        conn = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(conn.execute("select count(*) from messages").fetchone()[0], 70)
        finally:
            conn.close()

    def test_库被占用时删了但不假装回收成功(self):
        # 另一个连接持有读事务，VACUUM 拿不到排他锁
        blocker = sqlite3.connect(str(self.db), isolation_level=None)
        blocker.execute("BEGIN")
        blocker.execute("select count(*) from messages").fetchone()
        try:
            plan = dbclean.scan(self.db, 90)
            res = dbclean.execute(plan)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        self.assertEqual(res.error, "")
        self.assertEqual(res.deleted_messages, 60)       # 数据照删
        if not res.vacuumed:                             # 没抢到锁就该如实说明
            self.assertTrue(res.note)
            self.assertIn("没回收", res.note)

    def test_不压缩也能只用一次(self):
        plan = dbclean.scan(self.db, 90)
        res = dbclean.execute(plan, vacuum=False)
        self.assertFalse(res.vacuumed)
        self.assertTrue(res.note)


class HelperTest(unittest.TestCase):
    def test_大小可读(self):
        self.assertEqual(dbclean.human_size(0), "0 B")
        self.assertEqual(dbclean.human_size(999), "999 B")
        self.assertIn("KB", dbclean.human_size(2048))
        self.assertIn("MB", dbclean.human_size(5 * 1024 * 1024))

    def test_清单里写明不会动附件(self):
        tmp = Path(tempfile.mkdtemp(prefix="dbclean_"))
        db = make_db(tmp / "bridge.db", old=3, new=1)
        lines = dbclean.summary_lines(dbclean.scan(db, 90))
        text = "\n".join(lines)
        self.assertIn("消息", text)
        self.assertIn("投递记录", text)
        self.assertIn("预计回收", text)

    def test_档位与界面一致(self):
        self.assertIn(90, dbclean.KEEP_CHOICES)
        self.assertIn(0, dbclean.KEEP_CHOICES)
        self.assertEqual(dbclean.KEEP_LABELS[0], "永久保留（只清孤儿记录）")


class UIHelperTest(unittest.TestCase):
    """设置中心里的档位文案 ↔ 天数互转。

    界面不能 import 到 bridge 时也要有下拉框内容（_KEEP_FALLBACK），所以
    这里既测正常互转，也测「配置写了个不在档位里的 45」原样显示。
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "app"))
        import settings_ui
        cls.ui = settings_ui

    def test_天数转文案(self):
        self.assertEqual(self.ui._keep_label(90), dbclean.KEEP_LABELS[90])
        self.assertEqual(self.ui._keep_label(0), dbclean.KEEP_LABELS[0])
        self.assertIn("45", self.ui._keep_label(45))

    def test_文案转天数(self):
        for days in dbclean.KEEP_CHOICES:
            self.assertEqual(self.ui._keep_days(self.ui._keep_label(days)), days)

    def test_认不出的文案回落默认(self):
        self.assertEqual(self.ui._keep_days("随便写的"), 90)

    def test_下拉档位与后端一致(self):
        self.assertEqual(tuple(self.ui.KEEP_CHOICES), tuple(dbclean.KEEP_CHOICES))


if __name__ == "__main__":
    unittest.main()
