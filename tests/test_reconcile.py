"""漏转对账：用假的钉钉库 + 假桥库跑通配对、去重、容错。

这里不碰真库、不需要 pycryptodome：`dingtalk_db.sync` / `available` /
`find_data_dir` 全部被替换，剩下的配对/去重/状态文件是纯逻辑，正是最容易
写错的地方。真库上「解出来对不对」由 tools/probe_dingtalk_db.py 验证。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge import dingtalk_db, reconcile                            # noqa: E402
from bridge.config import Config, ReconcileConfig, RouteConfig       # noqa: E402
from bridge.reconcile import Reconciler, _norm                       # noqa: E402

CHAT = "【数字质控】浙江杭州萧山区大江东公司"
CID = "79617007357"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _card(title: str) -> str:
    """造一条 2950 互动卡片的 content（只有标题，正文不落库）。"""
    return json.dumps({
        "attachments": [{"extension": json.dumps(
            {"interactiveCardLastMessage": title})}],
    }, ensure_ascii=False)


def make_bridge_db(path: Path, rows: list[tuple[datetime, str]]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL, source TEXT NOT NULL,
                chat_id TEXT, chat_name TEXT, sender_id TEXT, sender_name TEXT,
                company TEXT, content TEXT, msg_type TEXT, media_path TEXT,
                msg_time TEXT, raw_id TEXT, created_at TEXT NOT NULL);
        """)
        for when, content in rows:
            conn.execute(
                "insert into messages (uid, source, chat_name, content, msg_type,"
                " msg_time, created_at) values (?,?,?,?,?,?,?)",
                (f"u-{when.timestamp()}", "dingtalk", CHAT, content, "text",
                 when.strftime("%Y-%m-%d %H:%M:%S"),
                 when.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def make_dt_db(path: Path, rows: list[tuple[datetime, str]],
               title: str = CHAT) -> None:
    """假的「钉钉解密库」：只建对账真正会用到的两张表。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("""
            CREATE TABLE tbconversation (cid TEXT, title TEXT, memberCount INT,
                                         lastModify INT, type INT);
            CREATE TABLE tbmsg_000 (cid TEXT, createdAt INT, content TEXT,
                                    contentType INT, senderId TEXT);
        """)
        conn.execute("insert into tbconversation values (?,?,?,?,?)",
                     (CID, title, 86, _ms(datetime.now()), 2))
        for when, content in rows:
            conn.execute("insert into tbmsg_000 values (?,?,?,?,?)",
                         (CID, _ms(when), content, 2950, "s1"))
        conn.commit()
    finally:
        conn.close()


class Harness:
    """一套临时环境：假钉钉目录 + 假解密库 + 假桥库。"""

    def __init__(self, dt_rows, br_rows, title=CHAT):
        self.tmp = Path(tempfile.mkdtemp(prefix="reconcile_test_"))
        self.data_dir = self.tmp / "DingTalk" / "abc_v3"
        (self.data_dir / "DBFiles").mkdir(parents=True)
        (self.data_dir / "DBFiles" / "dingtalk.db").write_bytes(b"x")
        self.dt_db = self.tmp / "dt-out" / "dingtalk.db"
        self.dt_db.parent.mkdir(parents=True, exist_ok=True)
        make_dt_db(self.dt_db, dt_rows, title)
        self.bridge_db = self.tmp / "bridge.db"
        make_bridge_db(self.bridge_db, br_rows)

    def patchers(self):
        return [
            mock.patch.object(dingtalk_db, "available", return_value=(True, "")),
            mock.patch.object(dingtalk_db, "find_data_dir", return_value=self.data_dir),
            mock.patch.object(dingtalk_db, "sync",
                              return_value={"db": self.dt_db, "uid": "1",
                                            "version": 3, "frames": 1,
                                            "method": "native", "wal_lag_min": 0,
                                            "wal_bytes": 0}),
        ]

    def config(self, **rc) -> Config:
        cfg = Config(routes=[RouteConfig(name="r", chat_names=[CHAT],
                                         targets=["群"])])
        cfg.dingtalk.chat_names = [CHAT]
        cfg.reconcile = ReconcileConfig(**rc)
        return cfg

    def reconciler(self, **rc) -> Reconciler:
        return Reconciler(self.config(**rc), self.bridge_db)


class NormTest(unittest.TestCase):
    def test_去标点后标题能在正文里搜到(self):
        card = "拦截任务风险提醒-中心未到件"
        body = "【拦截任务风险提醒-中心未到件】接入 33 票，请及时处理"
        self.assertIn(_norm(card), _norm(body))

    def test_换行与全角空格不影响匹配(self):
        self.assertEqual(_norm("风险 运单\n汇总"), _norm("风险运单汇总"))


class FindCidTest(unittest.TestCase):
    def test_精确优先于包含(self):
        tmp = Path(tempfile.mkdtemp()) / "t.db"
        conn = sqlite3.connect(str(tmp))
        conn.executescript("""
            CREATE TABLE tbconversation (cid TEXT, title TEXT, lastModify INT);
            insert into tbconversation values ('a', '【数字质控】浙江杭州萧山区大江东公司', 1);
            insert into tbconversation values ('b', '【数字质控】浙江杭州萧山区大江东公司二部', 9);
        """)
        self.assertEqual(dingtalk_db.find_cid(conn, "【数字质控】浙江杭州萧山区大江东公司")[0], "a")
        # 关键词只对得上「二部」那条时，退到包含匹配
        self.assertEqual(dingtalk_db.find_cid(conn, "二部")[0], "b")
        conn.close()

    def test_找不到返回None(self):
        tmp = Path(tempfile.mkdtemp()) / "t.db"
        conn = sqlite3.connect(str(tmp))
        conn.execute("CREATE TABLE tbconversation (cid TEXT, title TEXT, lastModify INT)")
        self.assertIsNone(dingtalk_db.find_cid(conn, "不存在的群"))
        conn.close()


class PairingTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now().replace(microsecond=0)

    def _run(self, h: Harness, **rc):
        rec = h.reconciler(**rc)
        ps = h.patchers()
        for p in ps:
            p.start()
        try:
            return rec.run_once(force=True)
        finally:
            for p in reversed(ps):
                p.stop()

    def test_标题对得上就不算漏(self):
        h = Harness(
            dt_rows=[(self.now - timedelta(minutes=5), _card("拦截任务风险提醒-中心未到件"))],
            br_rows=[(self.now - timedelta(minutes=6),
                      "【拦截任务风险提醒-中心未到件】接入 33 票")])
        res = self._run(h)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.cards, 1)
        self.assertEqual(res.missing, [])

    def test_桥完全没收到就是漏(self):
        h = Harness(
            dt_rows=[(self.now - timedelta(minutes=5), _card("按需派送签收时效跟进"))],
            br_rows=[])
        res = self._run(h)
        self.assertEqual(len(res.missing), 1)
        self.assertEqual(res.missing[0].title, "按需派送签收时效跟进")

    def test_时间兜底_标题不同但时间接近也算已转(self):
        # OCR 把标题读花了，但入库时间落在容差内 —— 不该报漏
        h = Harness(
            dt_rows=[(self.now - timedelta(minutes=10), _card("当日签-网点履约通知"))],
            br_rows=[(self.now - timedelta(minutes=12), "今天签收率 98.2%")])
        res = self._run(h, tolerance_min=30)
        self.assertEqual(res.missing, [])

    def test_超出容差才算漏(self):
        h = Harness(
            dt_rows=[(self.now - timedelta(minutes=100), _card("当日签-网点履约通知"))],
            br_rows=[(self.now - timedelta(minutes=5), "无关消息")])
        res = self._run(h, tolerance_min=30, lookback_min=180)
        self.assertEqual(len(res.missing), 1)

    def test_一条桥记录只能认领一张卡片(self):
        # 两张卡片相隔很近，桥只有一条 —— 必须报一条漏，不能双双算「已转」
        h = Harness(
            dt_rows=[(self.now - timedelta(minutes=20), _card("甲通知")),
                     (self.now - timedelta(minutes=19), _card("乙通知"))],
            br_rows=[(self.now - timedelta(minutes=20), "甲通知 内容")])
        res = self._run(h, tolerance_min=30)
        self.assertEqual(len(res.missing), 1)
        self.assertEqual(res.missing[0].title, "乙通知")


class DedupTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now().replace(microsecond=0)

    def _run(self, h: Harness, **rc):
        rec = h.reconciler(**rc)
        ps = h.patchers()
        for p in ps:
            p.start()
        try:
            return rec.run_once(force=True)
        finally:
            for p in reversed(ps):
                p.stop()

    def test_同一张卡片只报一次(self):
        h = Harness(dt_rows=[(self.now - timedelta(minutes=5), _card("漏掉的卡片"))],
                    br_rows=[])
        first = self._run(h)
        self.assertEqual(len(first.missing), 1)
        # 第二轮（新实例，模拟线程反复跑）：窗口仍然覆盖它，但不该再报
        second = self._run(h)
        self.assertEqual(second.missing, [])
        # 状态文件里记下来了
        state = json.loads((h.bridge_db.parent / reconcile.STATE_NAME)
                           .read_text(encoding="utf-8"))
        self.assertTrue(state["reported"])

    def test_状态文件过期条目会被清理(self):
        h = Harness(dt_rows=[], br_rows=[])
        rec = h.reconciler()
        old = time.time() - 30 * 86400
        fresh = time.time() - 3600
        rec._save_state({"last_run": fresh,
                         "reported": {"a:1": old, "b:2": fresh}})
        state = json.loads((h.bridge_db.parent / reconcile.STATE_NAME)
                           .read_text(encoding="utf-8"))
        self.assertNotIn("a:1", state["reported"])
        self.assertIn("b:2", state["reported"])

    def test_窗口按上次运行时间接续(self):
        # 上一轮在 3 小时前跑过：常规 lookback 只有 40 分钟，但停机期间的
        # 卡片必须补上，否则「重启后的第一轮」永远看不见停机时漏掉的消息
        h = Harness(dt_rows=[(self.now - timedelta(minutes=150), _card("停机期间的通知"))],
                    br_rows=[])
        rec = h.reconciler(lookback_min=40, max_gap_hours=12)
        rec._save_state({"last_run": time.time() - 3 * 3600, "reported": {}})
        ps = h.patchers()
        for p in ps:
            p.start()
        try:
            res = rec.run_once(force=True)
        finally:
            for p in reversed(ps):
                p.stop()
        self.assertEqual(len(res.missing), 1)
        self.assertGreater(res.window_min, 120)


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now().replace(microsecond=0)

    def test_缺依赖时不抛异常只返回原因(self):
        h = Harness(dt_rows=[], br_rows=[])
        rec = h.reconciler()
        with mock.patch.object(dingtalk_db, "available",
                               return_value=(False, "缺少 pycryptodome")):
            res = rec.run_once(force=True)
        self.assertFalse(res.ok)
        self.assertIn("pycryptodome", res.reason)

    def test_库里没有这个群要明说(self):
        h = Harness(dt_rows=[(self.now - timedelta(minutes=5), _card("别的群"))],
                    br_rows=[], title="完全不相干的群")
        rec = h.reconciler()
        ps = h.patchers()
        for p in ps:
            p.start()
        try:
            res = rec.run_once(force=True)
        finally:
            for p in reversed(ps):
                p.stop()
        self.assertFalse(res.ok)
        self.assertIn("没有", res.reason)

    def test_解密异常不冒泡(self):
        h = Harness(dt_rows=[], br_rows=[])
        rec = h.reconciler()
        with mock.patch.object(dingtalk_db, "available", return_value=(True, "")), \
                mock.patch.object(dingtalk_db, "find_data_dir", return_value=h.data_dir), \
                mock.patch.object(dingtalk_db, "sync",
                                  side_effect=RuntimeError("盘子忙")):
            res = rec.run_once(force=True)
        self.assertFalse(res.ok)
        self.assertIn("盘子忙", res.reason)

    def test_已停用后不再重复解密(self):
        h = Harness(dt_rows=[], br_rows=[])
        rec = h.reconciler()
        with mock.patch.object(dingtalk_db, "available",
                               return_value=(False, "不可用")):
            rec.run_once(force=True)
        # 第二次连 available 都不该再调用
        with mock.patch.object(dingtalk_db, "available",
                               side_effect=AssertionError("不该被调用")):
            res = rec.run_once(force=True)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "不可用")

    def test_未启用时不起线程(self):
        h = Harness(dt_rows=[], br_rows=[])
        rec = h.reconciler(enabled=False)
        self.assertFalse(rec.start())
        self.assertIsNone(rec._thread)


class ConfigTest(unittest.TestCase):
    def test_默认值(self):
        rc = ReconcileConfig()
        self.assertTrue(rc.enabled)
        self.assertEqual(rc.interval_min, 10.0)
        self.assertEqual(rc.tolerance_min, 30.0)

    def test_坏配置回落默认而不是崩溃(self):
        h = Harness(dt_rows=[], br_rows=[])
        rec = Reconciler(h.config(), h.bridge_db)
        rec.rc.interval_min = "abc"
        self.assertEqual(rec._num("interval_min", reconcile.DEFAULT_INTERVAL_MIN),
                         reconcile.DEFAULT_INTERVAL_MIN)


if __name__ == "__main__":
    unittest.main()
