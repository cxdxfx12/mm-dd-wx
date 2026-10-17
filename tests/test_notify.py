"""钉钉通知（故障告警 / 运行报告）单元测试。

覆盖：
    1. webhook 脱敏 —— 日志和界面里不能泄露 access_token；
    2. 加签算法 —— 与钉钉文档一致，可被独立重算复现；
    3. 关键词补全 —— 机器人开了「自定义关键词」时消息才不会白丢；
    4. 日志 → 告警的映射规则，以及「投递失败不重复告警」的例外；
    5. 冷却去重 —— 窗口被切走时每轮 OCR 都报错，不能把群刷爆；
    6. AlertHandler 只碰 bridge.*、且不会自己告警自己形成回环；
    7. 配置解析与环境变量覆盖。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bridge.config import NotifyConfig, load_config                      # noqa: E402
from bridge.notify import (DEFAULT_TEST_TEXT, AlertHandler,              # noqa: E402
                           DingTalkNotifier, in_hhmm_window,
                           install_alert_handler, mask_webhook, match_alert)

SECRET = "SECtestsecret0123456789"


class _Resp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Recorder:
    """假的 urlopen：记下每次请求，按脚本返回成功/失败。"""

    def __init__(self, payload: dict | None = None, error: Exception | None = None):
        self.payload = payload if payload is not None else {"errcode": 0, "errmsg": "ok"}
        self.error = error
        self.requests: list = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        return _Resp(self.payload)

    # --- 便捷读取 ---
    @property
    def count(self) -> int:
        return len(self.requests)

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].data.decode("utf-8"))

    def url(self, i: int = -1) -> str:
        return self.requests[i].full_url


def notifier(rec: _Recorder, **kw) -> DingTalkNotifier:
    base = dict(webhook="https://oapi.dingtalk.com/robot/send?access_token=tok123",
                opener=rec, retries=0, cooldown=300.0)
    base.update(kw)
    return DingTalkNotifier(**base)


# ══════════════════════════════════════════════════════════════════════

class TestMaskWebhook(unittest.TestCase):
    def test_token_is_masked(self):
        url = "https://oapi.dingtalk.com/robot/send?access_token=" + "a" * 64
        out = mask_webhook(url)
        self.assertNotIn("a" * 64, out)
        self.assertIn("…", out)
        self.assertTrue(out.startswith("https://oapi.dingtalk.com/robot/send"))

    def test_keeps_head_and_tail_for_identification(self):
        # 用合成的假 token：这里绝不能用真实凭证，否则会被打进安装包分发出去
        tok = "0123456789abcdef" * 4
        out = mask_webhook(f"...access_token={tok}")
        self.assertIn(tok[:6], out)
        self.assertIn(tok[-4:], out)

    def test_empty_and_plain(self):
        self.assertEqual(mask_webhook(""), "")
        self.assertEqual(mask_webhook("https://example.com/hook"), "https://example.com/hook")


class TestSign(unittest.TestCase):
    def test_no_secret_keeps_url_untouched(self):
        nf = DingTalkNotifier("https://x/y?access_token=a")
        self.assertEqual(nf._signed_url(now_ms=1700000000000), "https://x/y?access_token=a")

    def test_signature_matches_dingtalk_spec(self):
        """独立重算一遍签名，确保和钉钉文档的算法逐字节一致。"""
        nf = DingTalkNotifier("https://x/y?access_token=a", secret=SECRET)
        ts = 1700000000000
        url = nf._signed_url(now_ms=ts)

        string_to_sign = f"{ts}\n{SECRET}"
        expect = urllib.parse.quote_plus(base64.b64encode(hmac.new(
            SECRET.encode("utf-8"), string_to_sign.encode("utf-8"),
            hashlib.sha256).digest()))

        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["timestamp"][0], str(ts))
        self.assertEqual(q["sign"][0], urllib.parse.unquote_plus(expect))

    def test_uses_ampersand_when_url_already_has_query(self):
        nf = DingTalkNotifier("https://x/y?access_token=a", secret=SECRET)
        self.assertIn("&timestamp=", nf._signed_url(now_ms=1))

    def test_uses_question_mark_when_no_query(self):
        nf = DingTalkNotifier("https://x/y", secret=SECRET)
        self.assertIn("?timestamp=", nf._signed_url(now_ms=1))


class TestKeyword(unittest.TestCase):
    def test_appends_when_missing(self):
        nf = DingTalkNotifier("https://x/y", keyword="转发桥")
        self.assertTrue(nf._finalize("窗口最小化了").endswith("[转发桥]"))

    def test_keeps_as_is_when_present(self):
        nf = DingTalkNotifier("https://x/y", keyword="转发桥")
        self.assertEqual(nf._finalize("转发桥：窗口最小化了"), "转发桥：窗口最小化了")

    def test_no_keyword_configured(self):
        nf = DingTalkNotifier("https://x/y")
        self.assertEqual(nf._finalize("原文"), "原文")


class TestSend(unittest.TestCase):
    def test_text_payload(self):
        rec = _Recorder()
        ok, detail = notifier(rec).send_text("你好")
        self.assertTrue(ok)
        self.assertEqual(detail, "ok")
        self.assertEqual(rec.body(), {"msgtype": "text", "text": {"content": "你好"}})

    def test_text_with_at(self):
        rec = _Recorder()
        nf = notifier(rec, at_mobiles=["13800000000"], at_all=False)
        nf.send_text("告警", at=True)
        self.assertEqual(rec.body()["at"],
                         {"atMobiles": ["13800000000"], "isAtAll": False})

    def test_no_at_block_when_not_requested(self):
        rec = _Recorder()
        notifier(rec).send_text("普通消息")
        self.assertNotIn("at", rec.body())

    def test_markdown_payload(self):
        rec = _Recorder()
        notifier(rec).send_markdown("标题", "### 正文")
        body = rec.body()
        self.assertEqual(body["msgtype"], "markdown")
        self.assertEqual(body["markdown"]["title"], "标题")

    def test_keyword_is_applied_to_both_fields(self):
        rec = _Recorder()
        notifier(rec, keyword="转发桥").send_markdown("标题", "正文")
        body = rec.body()
        self.assertIn("转发桥", body["markdown"]["title"])
        self.assertIn("转发桥", body["markdown"]["text"])

    def test_business_error_is_reported_with_hint(self):
        rec = _Recorder({"errcode": 310000, "errmsg": "keywords not in content"})
        ok, detail = notifier(rec, retries=2).send_text("x")
        self.assertFalse(ok)
        self.assertIn("310000", detail)
        self.assertIn("关键词", detail)          # 附了人话解释
        self.assertEqual(rec.count, 1)           # 业务错误不重试

    def test_network_error_retries_then_fails(self):
        rec = _Recorder(error=urllib.error.URLError("断网"))
        ok, _ = notifier(rec, retries=2).send_text("x")
        self.assertFalse(ok)
        self.assertEqual(rec.count, 3)           # 1 次 + 2 次重试

    def test_retry_succeeds_on_second_attempt(self):
        state = {"n": 0}

        def opener(req, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                raise urllib.error.URLError("瞬时抖动")
            return _Resp({"errcode": 0})

        nf = DingTalkNotifier("https://x/y", opener=opener, retries=2)
        ok, _ = nf.send_text("x")
        self.assertTrue(ok)
        self.assertEqual(state["n"], 2)

    def test_non_json_response(self):
        class _Bad(_Resp):
            def __init__(self):
                self._body = b"<html>502</html>"

        nf = DingTalkNotifier("https://x/y", opener=lambda req, timeout=None: _Bad(),
                              retries=0)
        ok, detail = nf.send_text("x")
        self.assertFalse(ok)
        self.assertIn("JSON", detail)


class TestSendCustom(unittest.TestCase):
    """设置中心和主界面都靠 send_custom 发「用户自己写的内容」。"""

    def test_custom_text_goes_out_as_is(self):
        rec = _Recorder()
        ok, detail = notifier(rec).send_custom("今晚 18:00 交接班")
        self.assertTrue(ok)
        self.assertEqual(detail, "ok")
        self.assertEqual(rec.body()["text"]["content"], "今晚 18:00 交接班")

    def test_surrounding_blank_lines_are_trimmed(self):
        rec = _Recorder()
        notifier(rec).send_custom("\n  交班了  \n\n")
        self.assertEqual(rec.body()["text"]["content"], "交班了")

    def test_inner_newlines_are_preserved(self):
        rec = _Recorder()
        notifier(rec).send_custom("第一行\n第二行")
        self.assertEqual(rec.body()["text"]["content"], "第一行\n第二行")

    def test_empty_is_rejected_without_hitting_network(self):
        for blank in ("", "   ", "\n\t\n"):
            rec = _Recorder()
            ok, detail = notifier(rec).send_custom(blank)
            self.assertFalse(ok, f"{blank!r} 不该发出去")
            self.assertIn("空", detail)
            self.assertEqual(rec.count, 0, "空内容不该产生任何请求")

    def test_keyword_still_appended_for_custom_text(self):
        rec = _Recorder()
        notifier(rec, keyword="转发桥").send_custom("随便写的通知")
        self.assertIn("转发桥", rec.body()["text"]["content"])

    def test_default_test_text_is_shared_with_cli(self):
        # --notify-test 与设置里的「填入测试文案」用的是同一份常量，
        # 否则两边会慢慢漂移
        rec = _Recorder()
        notifier(rec).test()
        self.assertEqual(rec.body()["text"]["content"], DEFAULT_TEST_TEXT)
        self.assertIn("喵喵钉钉", DEFAULT_TEST_TEXT)


class TestAlertCooldown(unittest.TestCase):
    def test_same_key_suppressed_within_cooldown(self):
        rec = _Recorder()
        nf = notifier(rec, cooldown=300.0)
        self.assertTrue(nf.alert("k", "第一次"))
        self.assertFalse(nf.alert("k", "第二次"))
        self.assertEqual(rec.count, 1)

    def test_different_keys_are_independent(self):
        rec = _Recorder()
        nf = notifier(rec)
        self.assertTrue(nf.alert("a", "A"))
        self.assertTrue(nf.alert("b", "B"))
        self.assertEqual(rec.count, 2)

    def test_cooldown_expires(self):
        rec = _Recorder()
        nf = notifier(rec, cooldown=0.0)
        self.assertTrue(nf.alert("k", "一"))
        self.assertTrue(nf.alert("k", "二"))
        self.assertEqual(rec.count, 2)

    def test_reset_cooldown(self):
        rec = _Recorder()
        nf = notifier(rec)
        nf.alert("k", "一")
        nf.reset_cooldown("k")
        self.assertTrue(nf.alert("k", "二"))
        nf.reset_cooldown()
        self.assertTrue(nf.alert("k", "三"))

    def test_failed_send_releases_cooldown(self):
        """发送失败不该把冷却占了，否则下一轮就再也发不出去。"""
        rec = _Recorder(error=urllib.error.URLError("断网"))
        nf = notifier(rec)
        self.assertFalse(nf.alert("k", "一"))
        rec.error = None                      # 网络恢复
        self.assertTrue(nf.alert("k", "二"))


class TestMatchAlert(unittest.TestCase):
    def test_known_rules(self):
        cases = [
            ("未找到钉钉主窗口", "ocr-no-window"),
            ("钉钉窗口处于最小化状态，截不到内容", "ocr-minimized"),
            ("定位不到钉钉消息区控件", "ocr-no-area"),
            ("PrintWindow 失败", "ocr-print-fail"),
            ("锚点丢失（聊天区可能被滚动）：本帧不转发", "ocr-anchor-lost"),
            ("OCR 轮询异常：TimeoutError", "ocr-exception"),
        ]
        for text, key in cases:
            with self.subTest(text=text):
                hit = match_alert(logging.WARNING, text)
                self.assertIsNotNone(hit)
                self.assertEqual(hit[0], key)

    def test_title_mismatch_is_info_level_but_still_alerts(self):
        hit = match_alert(logging.INFO, "会话标题校验：不通过——本帧不转发（相似度 0.21）")
        self.assertEqual(hit[0], "ocr-title-mismatch")

    def test_delivery_failure_is_left_to_service(self):
        """投递失败由 service 的「连续 N 次」阈值逻辑负责，日志层不重复告警。"""
        self.assertIsNone(match_alert(logging.ERROR, "投递失败 → 申通五星业务员培训群 | 找不到群"))

    def test_quiet_infos_do_not_alert(self):
        for text in ("识别到 2 条新消息（丢弃跨天 0 条）", "基线帧建立：当前可见 12 条消息",
                     "已投递 → 申通五星业务员培训群"):
            with self.subTest(text=text):
                self.assertIsNone(match_alert(logging.INFO, text))

    def test_unknown_error_falls_back(self):
        self.assertEqual(match_alert(logging.ERROR, "从未见过的错误")[0], "generic-error")

    def test_unknown_warning_is_ignored(self):
        self.assertIsNone(match_alert(logging.WARNING, "某个无关的警告"))


class TestInHHMMWindow(unittest.TestCase):
    def _at(self, h, m=30):
        return datetime(2026, 10, 16, h, m)

    def test_normal_range(self):
        self.assertTrue(in_hhmm_window(self._at(9), "08:00-21:00"))
        self.assertFalse(in_hhmm_window(self._at(3), "08:00-21:00"))
        self.assertFalse(in_hhmm_window(self._at(23), "08:00-21:00"))

    def test_cross_midnight(self):
        self.assertTrue(in_hhmm_window(self._at(23), "22:00-06:00"))
        self.assertTrue(in_hhmm_window(self._at(3), "22:00-06:00"))
        self.assertFalse(in_hhmm_window(self._at(12), "22:00-06:00"))

    def test_boundary_is_half_open(self):
        self.assertTrue(in_hhmm_window(self._at(8, 0), "08:00-21:00"))
        self.assertFalse(in_hhmm_window(self._at(21, 0), "08:00-21:00"))

    def test_bad_input_means_always_on(self):
        """配置写错时宁可多告警，也不要静默丢掉所有告警。"""
        for spec in ("", "   ", "坏值", "08:00"):
            with self.subTest(spec=spec):
                self.assertTrue(in_hhmm_window(self._at(3), spec))

    def test_equal_bounds_means_always_on(self):
        self.assertTrue(in_hhmm_window(self._at(3), "08:00-08:00"))


class _SpyNotifier(DingTalkNotifier):
    """只记录 alert 调用，不真发。"""

    def __init__(self):
        super().__init__("https://x/y")
        self.alerts: list[tuple[str, str]] = []

    def alert(self, key, content, at=True):        # type: ignore[override]
        self.alerts.append((key, content))
        return True


class TestAlertHandler(unittest.TestCase):
    def setUp(self):
        self.spy = _SpyNotifier()
        self.handler = AlertHandler(self.spy)

    def _emit(self, name, levelno, text):
        rec = logging.LogRecord(name, levelno, "f.py", 1, text, None, None)
        self.handler.emit(rec)

    def test_matches_bridge_records(self):
        self._emit("bridge.source.ocr", logging.WARNING, "未找到钉钉主窗口")
        self.assertEqual(len(self.spy.alerts), 1)
        self.assertEqual(self.spy.alerts[0][0], "ocr-no-window")
        self.assertIn("采集中断", self.spy.alerts[0][1])

    def test_ignores_foreign_loggers(self):
        self._emit("root", logging.ERROR, "未找到钉钉主窗口")
        self._emit("urllib3", logging.ERROR, "未找到钉钉主窗口")
        self.assertEqual(self.spy.alerts, [])

    def test_ignores_own_logger_to_avoid_loop(self):
        """通知模块自己的日志绝不能再触发通知，否则会无限回环。"""
        self._emit("bridge.notify", logging.ERROR, "钉钉通知发送失败：errcode=310000")
        self.assertEqual(self.spy.alerts, [])

    def test_quiet_records_produce_nothing(self):
        self._emit("bridge.source.ocr", logging.INFO, "识别到 2 条新消息")
        self.assertEqual(self.spy.alerts, [])

    def test_handler_never_raises(self):
        class _Boom(_SpyNotifier):
            def alert(self, key, content, at=True):
                raise RuntimeError("炸了")

        h = AlertHandler(_Boom())
        rec = logging.LogRecord("bridge.source.ocr", logging.WARNING, "f.py", 1,
                                "未找到钉钉主窗口", None, None)
        h.emit(rec)          # 不抛异常 = 通过

    def test_install_returns_none_when_disabled(self):
        self.assertIsNone(install_alert_handler(None))
        self.assertIsNone(install_alert_handler(DingTalkNotifier("")))

    def test_install_and_detach(self):
        h = install_alert_handler(self.spy)
        self.assertIsNotNone(h)
        try:
            self.assertIn(h, logging.getLogger("bridge").handlers)
        finally:
            logging.getLogger("bridge").removeHandler(h)
        self.assertNotIn(h, logging.getLogger("bridge").handlers)


class TestFromConfig(unittest.TestCase):
    def test_disabled_returns_none(self):
        self.assertIsNone(DingTalkNotifier.from_config(NotifyConfig(enabled=False,
                                                                    webhook="https://x")))

    def test_enabled_without_webhook_returns_none(self):
        self.assertIsNone(DingTalkNotifier.from_config(NotifyConfig(enabled=True,
                                                                    webhook="  ")))

    def test_enabled_with_webhook(self):
        nf = DingTalkNotifier.from_config(NotifyConfig(
            enabled=True, webhook="https://x/y?access_token=a", secret="s",
            keyword="k", at_mobiles=["138"], at_all=True, cooldown=60))
        self.assertIsNotNone(nf)
        self.assertTrue(nf.enabled)
        self.assertEqual(nf.at_mobiles, ["138"])
        self.assertTrue(nf.at_all)
        desc = nf.describe()
        self.assertIn("关键词", desc)
        self.assertIn("加签", desc)
        self.assertIn("@所有人", desc)

    def test_none_config(self):
        self.assertIsNone(DingTalkNotifier.from_config(None))


CONFIG_TEMPLATE = """\
storage:
  db_path: "data/bridge.db"

dingtalk:
  mode: "ocr"

wechat:
  dry_run: true

notify:
  enabled: true
  webhook: "{webhook}"
  secret: ""
  keyword: ""
  cooldown: 120
  fail_threshold: 5
  idle_minutes: 90
  idle_window: "07:30-19:30"
  report_hourly: true
  report_daily_at: "18:00"

routes:
  - name: "r"
    chat_names: ["数字质控"]
    targets: ["申通五星业务员培训群"]
    template: "{{content}}"
"""


class TestConfigParsing(unittest.TestCase):
    def _write(self, webhook: str = "") -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="ddwx_notify_"))
        p = tmp / "config.yaml"
        p.write_text(CONFIG_TEMPLATE.format(webhook=webhook), encoding="utf-8")
        return p

    def test_notify_section_parsed(self):
        cfg = load_config(self._write("https://x/y?access_token=t"))
        n = cfg.notify
        self.assertTrue(n.enabled)
        self.assertEqual(n.cooldown, 120)
        self.assertEqual(n.fail_threshold, 5)
        self.assertEqual(n.idle_minutes, 90)
        self.assertEqual(n.idle_window, "07:30-19:30")
        self.assertTrue(n.report_hourly)
        self.assertEqual(n.report_daily_at, "18:00")
        # 关键：notify 段插在 routes 之前，routes 仍要能正常解析
        self.assertEqual(cfg.routes[0].targets, ["申通五星业务员培训群"])

    def test_env_overrides_webhook_and_secret(self):
        path = self._write("https://原值/y")
        os.environ["DINGTALK_ROBOT_WEBHOOK"] = "https://环境变量/y"
        os.environ["DINGTALK_ROBOT_SECRET"] = "SECfromenv"
        try:
            cfg = load_config(path)
        finally:
            os.environ.pop("DINGTALK_ROBOT_WEBHOOK", None)
            os.environ.pop("DINGTALK_ROBOT_SECRET", None)
        self.assertEqual(cfg.notify.webhook, "https://环境变量/y")
        self.assertEqual(cfg.notify.secret, "SECfromenv")

    def test_missing_section_uses_defaults(self):
        """老版本配置文件没有 notify 段时，要用默认值兜住，不能崩。"""
        tmp = Path(tempfile.mkdtemp(prefix="ddwx_nonotify_"))
        p = tmp / "config.yaml"
        p.write_text("routes:\n  - name: r\n    targets: ['群A']\n", encoding="utf-8")
        cfg = load_config(p)
        self.assertFalse(cfg.notify.enabled)
        self.assertEqual(cfg.notify.cooldown, 300.0)
        self.assertTrue(cfg.notify.log_alerts)


class TestServiceAlerts(unittest.TestCase):
    """告警触发点（投递失败阈值、采集停滞、定时报告）落在 service 上。

    这里用替身目标 + 替身通知器，只验证「该不该告警、告警几次」，
    真实发送链路由 TestSend 覆盖。
    """

    CHAT = "【数字质控】浙江杭州萧山区大江东公司"

    def setUp(self):
        from bridge.config import Config, RouteConfig, StorageConfig, WeChatConfig
        from bridge.service import BridgeService

        self._BridgeService = BridgeService
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.cfg_cls = (Config, RouteConfig, StorageConfig, WeChatConfig)

    def _msg(self, content="今天到件 30 万单"):
        from bridge.models import Message
        return Message(chat_id="c1", chat_name=self.CHAT, sender_id="u1",
                       sender_name="张三", company="申通快递", content=content,
                       msg_type="text", msg_time=datetime(2026, 10, 16, 14, 20),
                       raw_id=f"r-{content}")

    def _service(self, notify: NotifyConfig, fail: bool = False):
        from bridge.targets.base import SendResult, Target

        class _T(Target):
            name = "fake"

            def send_text(self, target, text):
                return (SendResult(False, target, "模拟失败") if fail_toggle[0]
                        else SendResult(True, target, "ok"))

            def health_check(self):
                return True, "ok"

        fail_toggle = [fail]
        Config, RouteConfig, StorageConfig, WeChatConfig = self.cfg_cls
        cfg = Config(
            storage=StorageConfig(db_path=str(Path(self.tmp.name) / "t.db")),
            wechat=WeChatConfig(interval=0.0, dry_run=False),
            notify=notify,
            routes=[RouteConfig(name="r", chat_names=["数字质控"],
                                targets=["申通五星业务员培训群"],
                                template="{content}")],
        )
        svc = self._BridgeService(cfg, target=_T())
        self.addCleanup(svc.close)
        spy = _SpyNotifier()
        svc.notifier = spy                     # 换掉真实发送端
        self.addCleanup(svc.close)
        return svc, spy, fail_toggle

    # ---- 投递失败阈值 ----

    def test_alerts_only_after_threshold(self):
        svc, spy, fail = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                    fail_threshold=3))
        fail[0] = True
        svc.handle(self._msg("1"))
        svc.handle(self._msg("2"))
        self.assertEqual(spy.alerts, [], "没到阈值不该告警")
        svc.handle(self._msg("3"))
        self.assertEqual(len(spy.alerts), 1, "到阈值应告警一次")
        self.assertEqual(spy.alerts[0][0], "deliver-fail")
        self.assertIn("连续失败 3 次", spy.alerts[0][1])

    def test_success_resets_consecutive_counter(self):
        svc, spy, fail = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                    fail_threshold=3))
        fail[0] = True
        svc.handle(self._msg("1"))
        svc.handle(self._msg("2"))
        fail[0] = False                        # 恢复一条
        svc.handle(self._msg("3"))
        fail[0] = True
        svc.handle(self._msg("4"))
        svc.handle(self._msg("5"))
        self.assertEqual(spy.alerts, [], "中间成功过就该重新计数")

    def test_alert_can_be_switched_off(self):
        svc, spy, fail = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                    notify_fail=False,
                                                    fail_threshold=1))
        fail[0] = True
        svc.handle(self._msg("1"))
        self.assertEqual(spy.alerts, [])

    def test_no_notifier_means_no_alert(self):
        svc, spy, fail = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                    fail_threshold=1))
        svc.notifier = None
        fail[0] = True
        svc.handle(self._msg("1"))             # 不应崩

    def test_delivery_rows_are_still_logged(self):
        """加了通知不能影响原有的投递日志落库。"""
        import sqlite3
        svc, spy, fail = self._service(NotifyConfig(enabled=True, webhook="https://x"))
        fail[0] = True
        svc.handle(self._msg("失败一条"))
        with sqlite3.connect(str(Path(self.tmp.name) / "t.db")) as c:
            rows = c.execute("SELECT status FROM deliveries").fetchall()
        self.assertEqual(rows, [("fail",)])

    # ---- 采集停滞 ----

    def _aged(self, svc, seconds: float):
        svc._last_activity = svc._last_activity - seconds

    def test_idle_alert_fires_once_per_episode(self):
        svc, spy, _ = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                 notify_idle=True, idle_minutes=60,
                                                 idle_window="00:00-23:59"))
        self._aged(svc, 3601)
        svc._watchdog_tick()
        svc._watchdog_tick()
        self.assertEqual(len(spy.alerts), 1, "同一段停滞只提醒一次")
        self.assertEqual(spy.alerts[0][0], "idle")

    def test_new_message_clears_idle_state(self):
        svc, spy, _ = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                 notify_idle=True, idle_minutes=60,
                                                 idle_window="00:00-23:59"))
        self._aged(svc, 3601)
        svc._watchdog_tick()
        svc.handle(self._msg("回来了"))         # 采集恢复
        self.assertFalse(svc._idle_alerted)
        self._aged(svc, 3601)
        svc._watchdog_tick()
        self.assertEqual(len(spy.alerts), 2, "恢复后再次停滞要再提醒")

    def test_idle_alert_respects_active_window(self):
        svc, spy, _ = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                 notify_idle=True, idle_minutes=60,
                                                 idle_window="00:00-00:01"))
        self._aged(svc, 3601)
        svc._watchdog_tick()
        self.assertEqual(spy.alerts, [], "时段外不该报停滞（夜里本来就没人说话）")

    def test_idle_alert_can_be_switched_off(self):
        svc, spy, _ = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                 notify_idle=False, idle_minutes=1,
                                                 idle_window="00:00-23:59"))
        self._aged(svc, 3601)
        svc._watchdog_tick()
        self.assertEqual(spy.alerts, [])

    def test_below_idle_threshold_is_quiet(self):
        svc, spy, _ = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                 notify_idle=True, idle_minutes=120,
                                                 idle_window="00:00-23:59"))
        self._aged(svc, 60)
        svc._watchdog_tick()
        self.assertEqual(spy.alerts, [])

    # ---- 生命周期与报告 ----

    def test_lifecycle_and_report_go_through_notifier(self):
        svc, spy, _ = self._service(NotifyConfig(enabled=True, webhook="https://x"))
        svc.sent: list[str] = []
        flags: list[bool] = []

        def _capture(content, at=False, system=False):  # type: ignore
            svc.sent.append(content)
            flags.append(system)          # 生命周期/报告必须标 system=True
            return True, "ok"

        spy.send_text = _capture
        svc.announce_start("钉钉OCR")
        svc.handle(self._msg("一条"))
        svc.send_report("每小时")
        svc.announce_stop("Ctrl+C")
        self.assertEqual(len(svc.sent), 3)
        self.assertIn("已启动", svc.sent[0])
        self.assertIn("📊 每小时", svc.sent[1])
        self.assertIn("已停止", svc.sent[2])
        self.assertIn("收到 1", svc.sent[2])
        # 三条都是程序自己的运维通知：必须带标记，否则会被采集端当群消息
        # 转发到微信业务群（真机事故，见 test_core.TestOwnNoticeNotForwarded）
        self.assertEqual(flags, [True, True, True])

    def test_lifecycle_can_be_switched_off(self):
        svc, spy, _ = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                 notify_start=False))
        svc.sent: list[str] = []
        spy.send_text = lambda content, at=False, system=False: (
            svc.sent.append(content), (True, "ok"))[1]  # type: ignore
        svc.announce_start()
        svc.announce_stop()
        self.assertEqual(svc.sent, [])

    def test_watchdog_tick_survives_broken_notifier(self):
        class _Boom(_SpyNotifier):
            def alert(self, key, content, at=True):
                raise RuntimeError("炸了")

        svc, spy, _ = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                 idle_minutes=1,
                                                 idle_window="00:00-23:59"))
        svc.notifier = _Boom()
        self._aged(svc, 3601)
        svc._watchdog_tick()                   # 不应抛异常

    def test_notify_status_text(self):
        svc, spy, _ = self._service(NotifyConfig(enabled=True, webhook="https://x"))
        self.assertIn("已启用", svc.notify_status())
        svc.notifier = None
        self.assertIn("没填 webhook", svc.notify_status())

    def test_start_watchdog_is_noop_without_notifier(self):
        svc, spy, _ = self._service(NotifyConfig(enabled=False))
        svc.notifier = None                    # from_config 在未启用时就返回 None
        svc.start_watchdog()                   # 未启用：不该起线程、不该挂 Handler
        self.assertIsNone(svc._watchdog)
        self.assertIsNone(svc._alert_handler)

    def test_start_watchdog_installs_handler_and_thread(self):
        import logging as _logging
        svc, spy, _ = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                 log_alerts=True))
        svc.start_watchdog()
        try:
            self.assertIsNotNone(svc._watchdog)
            self.assertIsNotNone(svc._alert_handler)
            self.assertIn(svc._alert_handler, _logging.getLogger("bridge").handlers)
        finally:
            svc.close()
        # close() 之后必须卸干净，否则重复启动会叠加多个 Handler
        self.assertNotIn(svc._alert_handler, _logging.getLogger("bridge").handlers)
        self.assertIsNone(svc._alert_handler)

    def test_log_alerts_can_be_switched_off(self):
        svc, spy, _ = self._service(NotifyConfig(enabled=True, webhook="https://x",
                                                 log_alerts=False))
        svc.start_watchdog()
        self.assertIsNone(svc._alert_handler)


class TestSystemPrefix(unittest.TestCase):
    """自动通知带【系统】前缀，人工测试消息不带。

    这个区分的唯一目的是让采集端能把「程序自己的运维文案」从群消息里择出去
    （否则启动通知、失联告警都会被转发到微信业务群），同时用户用
    「发送消息到钉钉群」手工验证链路时那条消息仍能被正常转发。
    """

    def test_system_text_gets_prefix(self):
        rec = _Recorder()
        notifier(rec).send_text("▶️ 转发桥已启动", system=True)
        self.assertEqual(rec.body()["text"]["content"], "【系统】▶️ 转发桥已启动")

    def test_prefix_is_idempotent(self):
        from bridge.notify import mark_system, SYSTEM_PREFIX
        once = mark_system("x")
        self.assertEqual(mark_system(once), once)
        self.assertTrue(once.startswith(SYSTEM_PREFIX))

    def test_leading_whitespace_still_detected(self):
        rec = _Recorder()
        notifier(rec).send_text("  \n【系统】已经带前缀了", system=True)
        # 不能因为前面有空白就再叠一个前缀
        self.assertEqual(rec.body()["text"]["content"].count("【系统】"), 1)

    def test_default_has_no_prefix(self):
        rec = _Recorder()
        notifier(rec).send_text("普通文本")
        self.assertNotIn("【系统】", rec.body()["text"]["content"])

    def test_custom_message_has_no_prefix(self):
        """人工测试消息必须原样发出 —— 用户靠它验证端到端链路。"""
        rec = _Recorder()
        notifier(rec).send_custom("测试一下")
        self.assertEqual(rec.body()["text"]["content"], "测试一下")

    def test_alert_gets_prefix_by_default(self):
        rec = _Recorder()
        notifier(rec).alert("k1", "手机端失联：已经 5 分钟 没有心跳")
        self.assertTrue(rec.body()["text"]["content"].startswith("【系统】"))

    def test_test_message_has_no_prefix(self):
        rec = _Recorder()
        notifier(rec).test()
        self.assertNotIn("【系统】", rec.body()["text"]["content"])


if __name__ == "__main__":
    unittest.main()
