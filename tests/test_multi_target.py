"""一对多转发（一个钉钉群 → 多个微信群）单元测试。

覆盖：
    1. 一条规则配多个 target → 每个目标都收到，且各自记一条投递日志；
    2. 多条规则命中同一来源 → 目标合并去重，不会重复发送；
    3. 多个来源群 → 多个目标群（多对多）同样成立；
    4. 每个目标之间保持发送间隔（限流对每个目标都生效）。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bridge.config import Config, RouteConfig, WeChatConfig          # noqa: E402
from bridge.models import Message                                     # noqa: E402
from bridge.router import Router                                      # noqa: E402
from bridge.service import BridgeService                              # noqa: E402
from bridge.targets.base import SendResult, Target                    # noqa: E402


class FakeTarget(Target):
    name = "fake"

    def __init__(self, fail_for: set[str] | None = None):
        self.sent: list[tuple[str, str]] = []
        self.fail_for = fail_for or set()

    def send_text(self, target: str, text: str) -> SendResult:
        self.sent.append((target, text))
        if target in self.fail_for:
            return SendResult(False, target, "模拟失败")
        return SendResult(True, target, "fake ok")

    def health_check(self):
        return True, "fake 正常"


def msg(chat="【数字质控】浙江杭州萧山区大江东公司", content="今天到件 30 万单") -> Message:
    return Message(
        chat_id="c1", chat_name=chat, sender_id="u1", sender_name="张三",
        company="申通快递", content=content, msg_type="text",
        msg_time=datetime(2026, 10, 16, 14, 20), raw_id=f"r-{chat}-{content}",
    )


def make_cfg(routes: list[RouteConfig], interval: float = 0.0) -> Config:
    return Config(routes=routes, wechat=WeChatConfig(interval=interval))


class TestMultiTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"

    def service(self, routes, target, interval=0.0) -> BridgeService:
        cfg = make_cfg(routes, interval)
        cfg.storage.db_path = str(self.db)
        svc = BridgeService(cfg, target=target)
        self.addCleanup(svc.close)
        return svc

    def delivery_rows(self) -> list[tuple[str, str, str]]:
        with sqlite3.connect(str(self.db)) as c:
            return c.execute(
                "SELECT target, status, uid FROM deliveries ORDER BY id").fetchall()

    # ---- 1. 一源多目标 ----

    def test_one_route_many_targets(self):
        routes = [RouteConfig(
            name="大江东 → 三个群",
            chat_names=["数字质控"],
            targets=["申通五星业务员培训群", "大江东转运中心", "质控管理群"],
        )]
        tgt = FakeTarget()
        svc = self.service(routes, tgt)
        self.assertTrue(svc.handle(msg()))

        got = [t for t, _ in tgt.sent]
        self.assertEqual(got, ["申通五星业务员培训群", "大江东转运中心", "质控管理群"])
        self.assertEqual(svc.counter["sent"], 3)
        self.assertEqual(svc.counter["failed"], 0)
        # 每个目标各记一条投递日志
        rows = self.delivery_rows()
        self.assertEqual([r[0] for r in rows],
                         ["申通五星业务员培训群", "大江东转运中心", "质控管理群"])
        self.assertTrue(all(r[1] == "ok" for r in rows))

    def test_all_targets_get_same_text(self):
        routes = [RouteConfig(name="r", chat_names=["数字质控"],
                              targets=["群A", "群B"], template="{content}")]
        tgt = FakeTarget()
        svc = self.service(routes, tgt)
        svc.handle(msg(content="内容X"))
        texts = dict(tgt.sent)
        self.assertEqual(texts["群A"], texts["群B"])
        self.assertIn("内容X", texts["群A"])

    def test_partial_failure_counted_per_target(self):
        routes = [RouteConfig(name="r", chat_names=["数字质控"],
                              targets=["群A", "群B"])]
        tgt = FakeTarget(fail_for={"群B"})
        svc = self.service(routes, tgt)
        svc.handle(msg())
        self.assertEqual(svc.counter["sent"], 1)
        self.assertEqual(svc.counter["failed"], 1)
        status = dict((t, s) for t, s, _ in self.delivery_rows())
        self.assertEqual(status, {"群A": "ok", "群B": "fail"})

    # ---- 2. 多规则命中同一来源：目标合并去重 ----

    def test_rules_merged_and_deduped(self):
        routes = [
            RouteConfig(name="r1", chat_names=["数字质控"], targets=["群A", "群B"]),
            RouteConfig(name="r2", chat_names=["大江东"], targets=["群B", "群C"]),
        ]
        matched = Router(routes).match(msg())
        self.assertEqual(matched.targets, ["群A", "群B", "群C"])

        tgt = FakeTarget()
        svc = self.service(routes, tgt)
        svc.handle(msg())
        # 群B 只发一次（跨规则去重）
        self.assertEqual([t for t, _ in tgt.sent], ["群A", "群B", "群C"])

    # ---- 3. 多对多 ----

    def test_many_to_many(self):
        routes = [
            RouteConfig(name="a", chat_names=["数字质控"], targets=["群1", "群2"]),
            RouteConfig(name="b", chat_names=["大江东"], targets=["群2", "群3"]),
        ]
        r = Router(routes)
        self.assertEqual(
            r.match(msg(chat="【数字质控】浙江杭州萧山区大江东公司")).targets,
            ["群1", "群2", "群3"])
        self.assertEqual(r.match(msg(chat="大江东转运中心群")).targets, ["群2", "群3"])
        self.assertEqual(r.match(msg(chat="无关的群")).targets, [])

    # ---- 4. 限流：每个目标之间都保持间隔 ----

    def test_throttle_between_targets(self):
        import time
        routes = [RouteConfig(name="r", chat_names=["数字质控"],
                              targets=["群A", "群B", "群C"])]
        tgt = FakeTarget()
        svc = self.service(routes, tgt, interval=0.25)
        t0 = time.time()
        svc.handle(msg())
        elapsed = time.time() - t0
        # 3 个目标之间至少 2 次间隔
        self.assertGreaterEqual(elapsed, 0.45)
        self.assertEqual(len(tgt.sent), 3)

    # ---- 5. 去重不受目标数影响 ----

    def test_duplicate_only_sent_once_per_target(self):
        routes = [RouteConfig(name="r", chat_names=["数字质控"],
                              targets=["群A", "群B"])]
        tgt = FakeTarget()
        svc = self.service(routes, tgt)
        m = msg()
        svc.handle(m)
        svc.handle(m)                     # 同一条再来一次
        self.assertEqual(len(tgt.sent), 2)
        self.assertEqual(svc.counter["duplicate"], 1)

    # ---- 6. 新增目标后，同一条消息仍能被投递（去重键与目标数无关）----

    def test_new_target_added_later(self):
        tgt = FakeTarget()
        svc1 = self.service(
            [RouteConfig(name="r", chat_names=["数字质控"], targets=["群A"])], tgt)
        svc1.handle(msg())

        tgt2 = FakeTarget()
        svc2 = self.service(
            [RouteConfig(name="r", chat_names=["数字质控"],
                         targets=["群A", "群B"])], tgt2)
        # 同一条消息在第二个服务里算重复，不会重发（幂等）
        self.assertFalse(svc2.handle(msg()))
        self.assertEqual(tgt2.sent, [])


if __name__ == "__main__":
    unittest.main()
