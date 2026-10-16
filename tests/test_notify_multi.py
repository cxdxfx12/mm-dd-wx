"""多个钉钉机器人（= 多个 webhook 地址）的支持。

背景
    群里常常不止一个机器人：每个上游系统各挂一个自定义机器人。
    告警和运行报告要能同时发给他们，而且每个机器人有**自己**的
    「加签密钥 / 自定义关键词」—— 不能只留一套全局值。
    所以有了 ``notify.webhooks`` 列表与 :class:`RobotSpec`。

这里锁住的行为
    · 一行一个机器人的写法（``地址 | 加签密钥 | 关键词``）解析 / 还原
    · 老的单个 ``notify.webhook`` 必须继续能用（向后兼容，不能因为升级就失联）
    · 一条通知要发到每一个机器人，且各自按自己的关键词补齐、各自加签
    · 「部分失败」不能把已经发成功的那几个一起判死
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.config import NotifyConfig, load_config                 # noqa: E402
from bridge.notify import (DingTalkNotifier, RobotSpec,             # noqa: E402
                           collect_robots, parse_robot_line,
                           robots_from_lines)
from cfgfile import ensure_list_key, read_rules                     # noqa: E402

URL_A = "https://oapi.dingtalk.com/robot/send?access_token=aaaa"
URL_B = "https://oapi.dingtalk.com/robot/send?access_token=bbbb"
SEC_A = "SECaaaaaaaaaaaaaaaaaaaa"
SEC_B = "SECbbbbbbbbbbbbbbbbbbbb"


# ══════════════════════════════════════════════════════════════════════
#  假的 urlopen
# ══════════════════════════════════════════════════════════════════════

class _Resp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _MultiRecorder:
    """假的 urlopen：URL 里含 ``bad`` 里任一标记就返回业务错误，否则成功。"""

    def __init__(self, bad: tuple[str, ...] = ()):
        self.bad = bad
        self.requests: list = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if any(b in req.full_url for b in self.bad):
            return _Resp({"errcode": 310000, "errmsg": "keywords not in content"})
        return _Resp({"errcode": 0, "errmsg": "ok"})

    @property
    def count(self) -> int:
        return len(self.requests)

    def urls(self) -> list[str]:
        return [r.full_url for r in self.requests]

    def bodies(self) -> list[dict]:
        return [json.loads(r.data.decode("utf-8")) for r in self.requests]


def _multi(rec: _MultiRecorder, specs: list[RobotSpec], **kw) -> DingTalkNotifier:
    return DingTalkNotifier(robots=specs, opener=rec, retries=0, **kw)


# ══════════════════════════════════════════════════════════════════════
#  一行 = 一个机器人
# ══════════════════════════════════════════════════════════════════════

class TestParseRobotLine(unittest.TestCase):
    def test_url_only(self):
        rb = parse_robot_line(URL_A)
        self.assertEqual(rb, RobotSpec(URL_A, "", ""))

    def test_url_with_secret(self):
        rb = parse_robot_line(f"{URL_A} | {SEC_A}")
        self.assertEqual((rb.url, rb.secret, rb.keyword), (URL_A, SEC_A, ""))

    def test_url_with_secret_and_keyword(self):
        rb = parse_robot_line(f"{URL_A}|{SEC_A}|转发桥")
        self.assertEqual((rb.url, rb.secret, rb.keyword), (URL_A, SEC_A, "转发桥"))

    def test_blank_segment_falls_back_to_default(self):
        """自己那段留空 -> 用配置里的全局默认值，老配置不改也能跑。"""
        rb = parse_robot_line(f"{URL_A}||", default_secret=SEC_A,
                              default_keyword="转发桥")
        self.assertEqual((rb.secret, rb.keyword), (SEC_A, "转发桥"))

    def test_own_value_beats_default(self):
        rb = parse_robot_line(f"{URL_A}|{SEC_B}|加急", default_secret=SEC_A,
                              default_keyword="转发桥")
        self.assertEqual((rb.secret, rb.keyword), (SEC_B, "加急"))

    def test_blank_and_comment_ignored(self):
        for bad in ("", "   ", "# 这一行是说明"):
            self.assertIsNone(parse_robot_line(bad))

    def test_separator_without_url_ignored(self):
        self.assertIsNone(parse_robot_line(" | SECxxx | 关键词"))

    def test_surrounding_whitespace_trimmed(self):
        rb = parse_robot_line(f"  {URL_A}  |  {SEC_A}  |  转发桥  ")
        self.assertEqual((rb.url, rb.secret, rb.keyword), (URL_A, SEC_A, "转发桥"))

    def test_more_segments_than_needed_ignored(self):
        rb = parse_robot_line(f"{URL_A}|{SEC_A}|转发桥|多余的")
        self.assertEqual((rb.url, rb.secret, rb.keyword), (URL_A, SEC_A, "转发桥"))

    def test_as_line_roundtrip(self):
        for spec in (RobotSpec(URL_A),
                     RobotSpec(URL_A, SEC_A),
                     RobotSpec(URL_A, SEC_A, "转发桥")):
            self.assertEqual(parse_robot_line(spec.as_line()), spec)

    def test_label_masks_token(self):
        lab = RobotSpec(URL_A, SEC_A, "转发桥").label()
        self.assertNotIn("aaaa", lab)
        self.assertIn("已加签", lab)
        self.assertIn("转发桥", lab)


class TestRobotsFromLines(unittest.TestCase):
    def test_multiple_lines_kept_in_order(self):
        out = robots_from_lines([URL_A, URL_B])
        self.assertEqual([r.url for r in out], [URL_A, URL_B])

    def test_duplicate_url_collapsed(self):
        out = robots_from_lines([URL_A, URL_A, URL_B])
        self.assertEqual([r.url for r in out], [URL_A, URL_B])

    def test_semicolon_and_newline_split(self):
        """老的 notify.webhook 是一个字符串，用分号塞多个也要能拆开。"""
        out = robots_from_lines([f"{URL_A};{URL_B}"])
        self.assertEqual([r.url for r in out], [URL_A, URL_B])
        out = robots_from_lines([f"{URL_A}\n{URL_B}"])
        self.assertEqual([r.url for r in out], [URL_A, URL_B])

    def test_dict_item_supported(self):
        out = robots_from_lines([{"url": URL_A, "secret": SEC_A,
                                  "keyword": "转发桥"}])
        self.assertEqual(out, [RobotSpec(URL_A, SEC_A, "转发桥")])

    def test_blank_items_ignored(self):
        self.assertEqual(robots_from_lines(["", "  ", None]), [])


class TestCollectRobots(unittest.TestCase):
    def test_prefers_webhooks_list(self):
        n = NotifyConfig(enabled=True, webhook=URL_A, webhooks=[URL_B])
        self.assertEqual([r.url for r in collect_robots(n)], [URL_B])

    def test_falls_back_to_single_webhook(self):
        """向后兼容：老配置只有 notify.webhook，升级后必须照常发得出去。"""
        n = NotifyConfig(enabled=True, webhook=URL_A, secret=SEC_A,
                         keyword="转发桥")
        out = collect_robots(n)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].url, out[0].secret, out[0].keyword),
                         (URL_A, SEC_A, "转发桥"))

    def test_global_values_are_defaults(self):
        n = NotifyConfig(enabled=True, webhooks=[URL_A, f"{URL_B}|{SEC_B}"],
                         secret=SEC_A, keyword="转发桥")
        out = collect_robots(n)
        self.assertEqual(out[0].secret, SEC_A)          # 没写 -> 用全局
        self.assertEqual(out[1].secret, SEC_B)          # 写了 -> 用自己的
        self.assertEqual([r.keyword for r in out], ["转发桥", "转发桥"])

    def test_nothing_configured(self):
        self.assertEqual(collect_robots(NotifyConfig(enabled=True)), [])
        self.assertEqual(collect_robots(None), [])


# ══════════════════════════════════════════════════════════════════════
#  发送：一条通知 -> 每个机器人都发一份
# ══════════════════════════════════════════════════════════════════════

class TestMultiRobotSend(unittest.TestCase):
    def test_sends_to_every_robot(self):
        rec = _MultiRecorder()
        ok, detail = _multi(rec, [RobotSpec(URL_A), RobotSpec(URL_B)]
                            ).send_text("窗口被最小化了")
        self.assertTrue(ok)
        self.assertEqual(rec.count, 2)
        self.assertEqual(detail, "2 个机器人全部发送成功")

    def test_single_robot_detail_stays_ok(self):
        """老行为不变：只有一个机器人时 detail 仍是 "ok"。"""
        rec = _MultiRecorder()
        ok, detail = _multi(rec, [RobotSpec(URL_A)]).send_text("hi")
        self.assertTrue(ok)
        self.assertEqual(detail, "ok")

    def test_each_robot_gets_its_own_keyword(self):
        """关键词是每个机器人各自配的，缺词要按各自那个词补。"""
        rec = _MultiRecorder()
        _multi(rec, [RobotSpec(URL_A, keyword="转发桥"),
                     RobotSpec(URL_B, keyword="告警")]).send_text("出问题了")
        c = [b["text"]["content"] for b in rec.bodies()]
        self.assertTrue(c[0].endswith("[转发桥]"))
        self.assertTrue(c[1].endswith("[告警]"))

    def test_keyword_already_present_not_duplicated(self):
        rec = _MultiRecorder()
        _multi(rec, [RobotSpec(URL_A, keyword="转发桥")]).send_text("转发桥：正常")
        self.assertEqual(rec.bodies()[0]["text"]["content"], "转发桥：正常")

    def test_each_robot_signed_with_its_own_secret(self):
        rec = _MultiRecorder()
        _multi(rec, [RobotSpec(URL_A, SEC_A), RobotSpec(URL_B, SEC_B)]
               ).send_text("hi")
        for url, secret in zip(rec.urls(), (SEC_A, SEC_B)):
            self.assertIn("timestamp=", url)
            self.assertIn("sign=", url)
            self.assertTrue(secret)

    def test_robot_without_secret_not_signed(self):
        rec = _MultiRecorder()
        _multi(rec, [RobotSpec(URL_A, SEC_A), RobotSpec(URL_B)]).send_text("hi")
        self.assertIn("sign=", rec.urls()[0])
        self.assertNotIn("sign=", rec.urls()[1])

    def test_partial_failure_still_counts_as_sent(self):
        """坏掉一个机器人不许把健康的那个一起判死，否则冷却失效会刷屏。"""
        rec = _MultiRecorder(bad=("bbbb",))
        ok, detail = _multi(rec, [RobotSpec(URL_A), RobotSpec(URL_B)]
                            ).send_text("hi")
        self.assertTrue(ok)
        self.assertIn("1/2", detail)

    def test_all_failure_returns_false(self):
        rec = _MultiRecorder(bad=("aaaa", "bbbb"))
        nf = _multi(rec, [RobotSpec(URL_A), RobotSpec(URL_B)])
        ok, detail = nf.send_text("hi")
        self.assertFalse(ok)
        self.assertTrue(nf.last_error)

    def test_last_error_cleared_after_success(self):
        bad = _MultiRecorder(bad=("aaaa", "bbbb"))
        nf = _multi(bad, [RobotSpec(URL_A), RobotSpec(URL_B)])
        nf.send_text("hi")
        self.assertTrue(nf.last_error)
        nf._opener = _MultiRecorder()
        nf.send_text("hi")
        self.assertEqual(nf.last_error, "")

    def test_no_robot_configured(self):
        nf = DingTalkNotifier(opener=_MultiRecorder(), retries=0)
        ok, detail = nf.send_text("hi")
        self.assertFalse(ok)
        self.assertIn("没有配置", detail)

    def test_markdown_broadcast(self):
        rec = _MultiRecorder()
        _multi(rec, [RobotSpec(URL_A), RobotSpec(URL_B)]
               ).send_markdown("标题", "正文")
        self.assertEqual(rec.count, 2)
        self.assertEqual(rec.bodies()[0]["msgtype"], "markdown")

    def test_at_payload_goes_to_all(self):
        rec = _MultiRecorder()
        _multi(rec, [RobotSpec(URL_A), RobotSpec(URL_B)],
               at_mobiles=["13800000000"], at_all=True).send_text("hi", at=True)
        for b in rec.bodies():
            self.assertEqual(b["at"]["atMobiles"], ["13800000000"])
            self.assertTrue(b["at"]["isAtAll"])


class TestMultiDescribe(unittest.TestCase):
    def test_count_mentioned(self):
        nf = DingTalkNotifier(robots=[RobotSpec(URL_A), RobotSpec(URL_B)],
                              at_all=True)
        desc = nf.describe()
        self.assertIn("2 个机器人", desc)
        self.assertIn("@所有人", desc)

    def test_single_robot_keeps_old_wording(self):
        nf = DingTalkNotifier(webhook=URL_A, secret=SEC_A, keyword="转发桥")
        desc = nf.describe()
        self.assertIn("关键词「转发桥」", desc)
        self.assertIn("已加签", desc)
        self.assertNotIn("个机器人", desc)

    def test_enabled_reflects_count(self):
        self.assertTrue(DingTalkNotifier(robots=[RobotSpec(URL_A)]).enabled)
        self.assertFalse(DingTalkNotifier().enabled)


class TestFromConfigMulti(unittest.TestCase):
    def test_builds_all_robots(self):
        nf = DingTalkNotifier.from_config(NotifyConfig(
            enabled=True, webhooks=[f"{URL_A}|{SEC_A}", URL_B],
            at_mobiles=["138"], cooldown=60))
        self.assertIsNotNone(nf)
        self.assertEqual(len(nf.robots), 2)
        self.assertEqual(nf.robots[0].secret, SEC_A)
        self.assertEqual(nf.robots[1].secret, "")

    def test_legacy_single_still_works(self):
        nf = DingTalkNotifier.from_config(NotifyConfig(
            enabled=True, webhook=URL_A, keyword="转发桥"))
        self.assertEqual(len(nf.robots), 1)
        self.assertEqual(nf.keyword, "转发桥")

    def test_first_robot_exposed_as_legacy_attrs(self):
        """老代码还在读 notifier.webhook，语义是「第一个机器人」。"""
        nf = DingTalkNotifier.from_config(NotifyConfig(
            enabled=True, webhooks=[f"{URL_A}|{SEC_A}|转发桥", URL_B]))
        self.assertEqual(nf.webhook, URL_A)
        self.assertEqual(nf.secret, SEC_A)
        self.assertEqual(nf.keyword, "转发桥")

    def test_enabled_without_any_address(self):
        self.assertIsNone(DingTalkNotifier.from_config(
            NotifyConfig(enabled=True, webhook="  ")))


# ══════════════════════════════════════════════════════════════════════
#  配置读写
# ══════════════════════════════════════════════════════════════════════

CONFIG_TMPL = """\
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

routes:
  - name: "r"
    chat_names: ["数字质控"]
    targets: ["申通五星业务员培训群"]
    template: "{{content}}"
"""


class TestNotifyConfigParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ddwx_multi_"))
        self.path = self.tmp / "config.yaml"
        self._saved = os.environ.pop("DINGTALK_ROBOT_WEBHOOKS", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["DINGTALK_ROBOT_WEBHOOKS"] = self._saved
        else:
            os.environ.pop("DINGTALK_ROBOT_WEBHOOKS", None)

    def _write(self, body: str) -> None:
        self.path.write_text(body, encoding="utf-8")

    def test_webhooks_list_parsed(self):
        self._write(CONFIG_TMPL.format(webhook="")
                    .replace('  keyword: ""',
                             '  keyword: ""\n'
                             '  webhooks:\n'
                             f'    - "{URL_A} | {SEC_A} | 转发桥"\n'
                             f'    - "{URL_B}"'))
        cfg = load_config(self.path)
        self.assertEqual(len(cfg.notify.webhooks), 2)
        self.assertEqual([r.url for r in collect_robots(cfg.notify)],
                         [URL_A, URL_B])
        self.assertEqual(collect_robots(cfg.notify)[0].secret, SEC_A)

    def test_env_webhooks_override_file(self):
        self._write(CONFIG_TMPL.format(webhook=URL_A))
        os.environ["DINGTALK_ROBOT_WEBHOOKS"] = f"{URL_B}; {URL_A}"
        cfg = load_config(self.path)
        self.assertEqual(cfg.notify.webhooks, [URL_B, " " + URL_A])
        self.assertEqual(cfg.notify.webhook, "")
        self.assertEqual([r.url for r in collect_robots(cfg.notify)],
                         [URL_B, URL_A])


class TestEnsureListKey(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ddwx_ensure_"))
        self.path = self.tmp / "config.yaml"
        self.original = CONFIG_TMPL.format(webhook=URL_A)
        self.path.write_text(self.original, encoding="utf-8")

    def test_inserts_when_missing_before_next_key(self):
        """老配置里没有 webhooks 键 —— 要就地插在 webhook 之后、secret 之前。"""
        self.assertTrue(ensure_list_key(self.path, "notify", "webhooks",
                                        [f"{URL_A} | {SEC_A}", URL_B],
                                        after_key="webhook"))
        body = self.path.read_text(encoding="utf-8")
        lines = body.splitlines()
        i_wh = next(i for i, s in enumerate(lines) if s.strip().startswith("webhook:"))
        i_ws = next(i for i, s in enumerate(lines) if s.strip().startswith("webhooks:"))
        i_se = next(i for i, s in enumerate(lines) if s.strip().startswith("secret:"))
        self.assertLess(i_wh, i_ws)
        self.assertLess(i_ws, i_se)
        self.assertEqual(lines[i_wh + 1].strip(), "webhooks:")

    def test_inserted_items_are_readable_and_parsable(self):
        ensure_list_key(self.path, "notify", "webhooks",
                        [f"{URL_A} | {SEC_A}", URL_B], after_key="webhook")
        cfg = load_config(self.path)
        self.assertEqual([r.url for r in collect_robots(cfg.notify)],
                         [URL_A, URL_B])

    def test_replaces_when_present(self):
        ensure_list_key(self.path, "notify", "webhooks", [URL_A],
                        after_key="webhook")
        ensure_list_key(self.path, "notify", "webhooks", [URL_B],
                        after_key="webhook")
        cfg = load_config(self.path)
        self.assertEqual(cfg.notify.webhooks, [URL_B])
        self.assertEqual(self.path.read_text(encoding="utf-8").count("webhooks:"), 1)

    def test_routes_still_last_section(self):
        """notify 段之后仍必须是 routes —— set_routes 依赖这一点。"""
        ensure_list_key(self.path, "notify", "webhooks", [URL_A],
                        after_key="webhook")
        self.assertEqual(read_rules(self.path)[0]["targets"],
                         ["申通五星业务员培训群"])
        self.assertTrue(
            self.path.read_text(encoding="utf-8").rstrip().splitlines()[-1]
            .startswith("    "))

    def test_missing_section_returns_false(self):
        p = self.tmp / "none.yaml"
        p.write_text("storage:\n  db_path: x\n", encoding="utf-8")
        self.assertFalse(ensure_list_key(p, "notify", "webhooks", [URL_A]))

    def test_empty_list_writes_inline_empty(self):
        ensure_list_key(self.path, "notify", "webhooks", [],
                        after_key="webhook")
        cfg = load_config(self.path)
        self.assertEqual(cfg.notify.webhooks, [])


class TestSettingsRobotRows(unittest.TestCase):
    """设置中心的「转换函数」—— 界面与配置之间的那一层。"""

    def test_rows_from_legacy_config(self):
        import settings_ui
        rows = settings_ui.robot_rows_from_config(
            {"webhook": URL_A, "secret": SEC_A, "keyword": "转发桥"})
        self.assertEqual(rows, [(URL_A, SEC_A, "转发桥")])

    def test_rows_from_new_config(self):
        import settings_ui
        rows = settings_ui.robot_rows_from_config(
            {"webhooks": [f"{URL_A}|{SEC_A}", URL_B]})
        self.assertEqual(rows, [(URL_A, SEC_A, ""), (URL_B, "", "")])

    def test_rows_empty_config(self):
        import settings_ui
        self.assertEqual(settings_ui.robot_rows_from_config({}), [])

    def test_lines_roundtrip_through_notify(self):
        import settings_ui
        rows = [(URL_A, SEC_A, "转发桥"), (URL_B, "", "")]
        lines = settings_ui.robot_lines_for_config(rows)
        back = robots_from_lines(lines)
        self.assertEqual([(r.url, r.secret, r.keyword) for r in back], rows)

    def test_lines_omit_empty_tail(self):
        import settings_ui
        lines = settings_ui.robot_lines_for_config([(URL_A, "", "")])
        self.assertEqual(lines, [URL_A])


if __name__ == "__main__":
    unittest.main()
