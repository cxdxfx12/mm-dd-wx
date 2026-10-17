"""核心链路单元测试（离线运行，不依赖微信或钉钉）。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bridge.config import Config, RouteConfig, WeChatConfig          # noqa: E402
from bridge.formatter import render, render_digest                    # noqa: E402
from bridge.models import Message                                     # noqa: E402
from bridge.router import Router                                      # noqa: E402
from bridge.service import BridgeService                              # noqa: E402
from bridge.storage import Storage                                    # noqa: E402
from bridge.targets.base import SendResult, Target                    # noqa: E402


class FakeTarget(Target):
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, target: str, text: str) -> SendResult:
        self.sent.append((target, text))
        return SendResult(True, target, "fake")


def msg(**kw) -> Message:
    base = dict(
        chat_id="c1", chat_name="供应商群", sender_id="u1", sender_name="张三",
        company="示例科技有限公司", content="测试内容", msg_type="text",
        msg_time=datetime(2026, 10, 15, 9, 30), raw_id="r1",
    )
    base.update(kw)
    return Message(**base)


class TestModels(unittest.TestCase):
    def test_uid_stable_with_raw_id(self):
        self.assertEqual(msg(raw_id="abc").uid, msg(raw_id="abc", content="改了").uid)

    def test_uid_differs_without_raw_id(self):
        a = msg(raw_id="", content="A").uid
        b = msg(raw_id="", content="B").uid
        self.assertNotEqual(a, b)

    def test_uid_falls_back_to_hash_without_raw_id(self):
        m = msg(raw_id="")
        self.assertTrue(m.uid.startswith("dingtalk:"))
        self.assertEqual(m.uid, msg(raw_id="").uid)


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def test_save_returns_true_only_once(self):
        m = msg()
        self.assertTrue(self.storage.save_message(m))
        self.assertFalse(self.storage.save_message(m))
        self.assertEqual(self.storage.stats()["total_messages"], 1)

    def test_is_duplicate(self):
        m = msg()
        self.assertFalse(self.storage.is_duplicate(m.uid))
        self.storage.save_message(m)
        self.assertTrue(self.storage.is_duplicate(m.uid))

    def test_query_filters_pushed_down(self):
        self.storage.save_message(msg(raw_id="r1", company="A公司", content="苹果"))
        self.storage.save_message(msg(raw_id="r2", company="B公司", content="香蕉"))
        self.assertEqual(len(self.storage.query(company="A公司")), 1)
        self.assertEqual(len(self.storage.query(keyword="香蕉")), 1)
        self.assertEqual(len(self.storage.query(company="A公司", keyword="香蕉")), 0)

    def test_delivery_log(self):
        self.storage.log_delivery("dingtalk:r1", "测试群", "ok")
        self.assertEqual(self.storage.stats()["deliveries"].get("ok"), 1)

    def test_export_csv_escapes_content(self):
        self.storage.save_message(msg(content='含逗号,和"引号"'))
        out = self.storage.export_csv(Path(self.tmp.name) / "out.csv")
        text = out.read_text(encoding="utf-8-sig")
        self.assertIn("含逗号", text)
        self.assertEqual(text.count("\n"), 2)


class TestRouter(unittest.TestCase):
    def test_empty_filter_matches_all(self):
        r = Router([RouteConfig(name="all", targets=["群1"])])
        self.assertEqual(r.match(msg()).targets, ["群1"])

    def test_company_filter(self):
        r = Router([RouteConfig(name="x", companies=["示例科技"], targets=["群1"])])
        self.assertEqual(r.match(msg()).targets, ["群1"])
        self.assertEqual(r.match(msg(company="别家公司")).targets, [])

    def test_keyword_filter_is_case_insensitive(self):
        r = Router([RouteConfig(name="x", keywords=["URGENT"], targets=["群1"])])
        self.assertEqual(r.match(msg(content="this is urgent")).targets, ["群1"])
        self.assertEqual(r.match(msg(content="普通消息")).targets, [])

    def test_wildcard_matches_all(self):
        r = Router([RouteConfig(name="x", companies=["*"], targets=["群1"])])
        self.assertEqual(r.match(msg(company="任意公司")).targets, ["群1"])

    def test_targets_dedup_across_routes(self):
        r = Router([
            RouteConfig(name="a", targets=["群1", "群2"]),
            RouteConfig(name="b", targets=["群2", "群3"]),
        ])
        self.assertEqual(r.match(msg()).targets, ["群1", "群2", "群3"])

    def test_template_from_first_hit(self):
        r = Router([
            RouteConfig(name="a", targets=["群1"], template="T1"),
            RouteConfig(name="b", targets=["群2"], template="T2"),
        ])
        self.assertEqual(r.match(msg()).template, "T1")

    def test_no_routes_raises(self):
        with self.assertRaises(ValueError):
            Router([])


class TestFormatter(unittest.TestCase):
    def test_render_default(self):
        text = render(msg(content="你好"))
        self.assertIn("示例科技有限公司", text)
        self.assertIn("张三", text)
        self.assertIn("你好", text)

    def test_render_custom_template(self):
        text = render(msg(content="你好"), "{sender}|{content}|{time}")
        self.assertEqual(text, "张三|你好|09:30")

    def test_render_bad_placeholder_raises(self):
        with self.assertRaises(ValueError):
            render(msg(), "{not_exist}")

    def test_media_message_uses_label(self):
        text = render(msg(content="", msg_type="image"), "{content}")
        self.assertEqual(text, "[图片]")

    def test_digest(self):
        text = render_digest([msg(content="A"), msg(content="B", raw_id="r2")], "日报")
        self.assertIn("共 2 条", text)
        self.assertIn("A", text)
        self.assertIn("B", text)

    def test_digest_empty(self):
        self.assertEqual(render_digest([]), "")


class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            storage=type("S", (), {"db_path": str(Path(self.tmp.name) / "s.db")})(),
            wechat=WeChatConfig(dry_run=True, interval=0.0, targets=["测试群"]),
            routes=[RouteConfig(name="t", companies=["示例科技"], targets=["测试群"],
                                template="{content}")],
        )
        self.cfg._base_dir = Path(self.tmp.name)
        self.target = FakeTarget()
        self.svc = BridgeService(self.cfg, target=self.target)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def test_full_link(self):
        self.assertTrue(self.svc.handle(msg()))
        self.assertEqual(len(self.target.sent), 1)
        self.assertEqual(self.target.sent[0][0], "测试群")
        self.assertEqual(self.svc.counter["sent"], 1)

    def test_duplicate_not_sent_twice(self):
        self.svc.handle(msg())
        self.assertFalse(self.svc.handle(msg()))
        self.assertEqual(len(self.target.sent), 1)
        self.assertEqual(self.svc.counter["duplicate"], 1)

    def test_filtered_still_stored(self):
        self.assertTrue(self.svc.handle(msg(company="别家公司")))
        self.assertEqual(len(self.target.sent), 0)
        self.assertEqual(self.svc.counter["filtered"], 1)
        self.assertEqual(self.svc.storage.stats()["total_messages"], 1)

    def test_summary_no_crash(self):
        self.svc.handle(msg())
        self.assertIn("已发送 1", self.svc.summary())


class TestOwnNoticeNotForwarded(unittest.TestCase):
    """程序自己发在钉钉群里的运维文案，不能被当成群消息转给微信。

    真实事故：采集端切换到手机之后，启动通知「▶️ 转发桥已启动 模式：正式发送
    采集：…」被手机读到并原样发进了「申通五星业务员培训群」。运维文案进业务群
    既难看又误导，所以自动通知统一带【系统】前缀，采集端据此拦掉。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            storage=type("S", (), {"db_path": str(Path(self.tmp.name) / "s.db")})(),
            wechat=WeChatConfig(dry_run=True, interval=0.0, targets=["测试群"]),
            routes=[RouteConfig(name="t", companies=[], targets=["测试群"],
                                template="{content}")],
        )
        self.cfg._base_dir = Path(self.tmp.name)
        self.target = FakeTarget()
        self.svc = BridgeService(self.cfg, target=self.target)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def test_startup_notice_is_stored_but_not_sent(self):
        m = msg(sender_name="服务诊断专员", raw_id="own1",
                content="【系统】▶️ 转发桥已启动\n模式：正式发送")
        self.assertTrue(self.svc.handle(m))
        self.assertEqual(len(self.target.sent), 0, "自身通知被投递出去了")
        self.assertEqual(self.svc.counter["filtered"], 1)
        # 留痕：仍然入库，事后能查到程序在那个时刻发过什么
        self.assertEqual(self.svc.storage.stats()["total_messages"], 1)

    def test_force_does_not_bypass_the_guard(self):
        """手动补发（force=True）也不该把这些运维文案补进业务群。"""
        m = msg(raw_id="own2", content="【系统】⏹ 转发桥已停止")
        self.svc.handle(m, force=True)
        self.assertEqual(len(self.target.sent), 0)

    def test_normal_message_still_goes_through(self):
        """护栏不能误伤正常业务消息 —— 前缀只认行首。"""
        m = msg(raw_id="ok1", content="今天还有1票拦截单没完成退单打印")
        self.svc.handle(m)
        self.assertEqual(len(self.target.sent), 1)

    def test_prefix_not_at_line_start_is_kept(self):
        """正文中间出现【系统】是正常文字，不能当通知拦掉。"""
        m = msg(raw_id="ok2", content="请转告【系统】组，今晚加派两个人")
        self.svc.handle(m)
        self.assertEqual(len(self.target.sent), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
