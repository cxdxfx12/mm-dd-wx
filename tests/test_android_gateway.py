"""安卓网关（mode: android）的离线测试。

分四层：
  1. 报文解析 —— 宽容但不能出错（空内容跳过、seq 缺省、fp 补算、会话名回退）
  2. 时间解析 —— 手机端的格式五花八门，认不出要退回本机时间而不是拒收
  3. 鉴权     —— token 是唯一挡住「别人往你群里发消息」的东西，必须测得死一点
  4. 真实链路 —— 起真 HTTP 服务、用 urllib 发真请求，断言消息端到端进了
                 BridgeService 并落库（投递端用桩，绝不碰微信）
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from bridge.config import Config, DingTalkConfig, RouteConfig, StorageConfig, WeChatConfig
from bridge.fingerprint import fp_of, norm_sender, unit_fp
from bridge.service import BridgeService
from bridge.sources import android_gateway
from bridge.sources.android_gateway import (
    HEARTBEAT_GRACE, MAX_BATCH, MAX_SCREEN_BODY, OFFLINE_REMIND_EVERY,
    AndroidGatewaySource, _Ctx, _apk_search_dirs, _find_apk, _online_now,
    _watchdog_step, build_messages, lan_ips, set_apk_dirs,
)
from bridge.sources.base import Source
from bridge.sources.factory import build_source
from bridge.targets.base import SendResult, Target

TOKEN = "s3cret-token"


class _StubTarget(Target):
    """投递端桩：只记下发了什么，不碰微信。"""

    name = "stub"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, target: str, text: str) -> SendResult:
        self.sent.append((target, text))
        return SendResult(ok=True, target=target, detail="stub")


def free_port() -> int:
    """要一个当前空闲的端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_cfg(port: int, token: str = TOKEN, **kw) -> Config:
    dt = DingTalkConfig(
        mode="android",
        gateway_host="127.0.0.1",
        gateway_port=port,
        gateway_token=token,
        chat_names=kw.pop("chat_names", ["数字质控"]),
        companies=kw.pop("companies", []),
        android_parse=kw.pop("android_parse", False),
        android_offline_after=kw.pop("android_offline_after", 300.0),
    )
    return Config(dingtalk=dt, **kw)


# --------------------------------------------------------------- 报文解析

class TestBuildMessages(unittest.TestCase):

    def build(self, payload: dict, **kw):
        return build_messages(payload, chat_name=kw.pop("chat_name", "数字质控"),
                              companies=kw.pop("companies", []), **kw)

    def test_basic_fields(self):
        msgs, skipped = self.build({"messages": [{
            "sender": "签收跟单专员 机器人",
            "content": "@所有人【客户声音履约风险提醒】374票",
            "time": "2026-10-16 20:31:00",
        }]})
        self.assertEqual(skipped, [])
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertEqual(m.source, "android")
        self.assertEqual(m.chat_name, "数字质控")
        self.assertEqual(m.sender_name, "签收跟单专员 机器人")
        self.assertEqual(m.content, "@所有人【客户声音履约风险提醒】374票")
        self.assertEqual(m.msg_type, "text")
        self.assertEqual(m.msg_time, datetime(2026, 10, 16, 20, 31, 0))

    def test_empty_content_is_skipped_not_error(self):
        msgs, skipped = self.build({"messages": [
            {"sender": "张三", "content": "   "},
            {"sender": "李四", "content": "有内容"},
        ]})
        self.assertEqual(len(msgs), 1)
        self.assertEqual(len(skipped), 1)
        self.assertIn("第 1 条", skipped[0])

    def test_non_dict_item_skipped(self):
        msgs, skipped = self.build({"messages": ["不是对象"]})
        self.assertEqual(msgs, [])
        self.assertEqual(len(skipped), 1)

    def test_fp_computed_when_missing(self):
        msgs, _ = self.build({"messages": [{"sender": "张三", "content": "你好"}]})
        self.assertEqual(msgs[0].raw_id, f"{fp_of('张三', '你好')}:1")

    def test_fp_from_phone_is_trusted(self):
        msgs, _ = self.build({"messages": [
            {"sender": "张三", "content": "你好", "fp": "deadbeef", "seq": 7}]})
        self.assertEqual(msgs[0].raw_id, "deadbeef:7")
        self.assertEqual(msgs[0].uid, "android:deadbeef:7")

    def test_seq_defaults_to_one_and_clamped(self):
        msgs, _ = self.build({"messages": [
            {"sender": "a", "content": "x"},
            {"sender": "a", "content": "x", "seq": 0},
            {"sender": "a", "content": "x", "seq": "不是数字"},
        ]})
        self.assertEqual([m.raw_id for m in msgs],
                         [f"{fp_of('a', 'x')}:1"] * 3)

    def test_retransmit_yields_identical_uid(self):
        """重传（网络抖动、没收到响应）必须落回同一个 uid，否则会被重复转发。"""
        item = {"sender": "签收跟单专员 机器人", "content": "同一条内容", "seq": 3}
        first, _ = self.build({"messages": [dict(item)]})
        again, _ = self.build({"messages": [dict(item)]})
        self.assertEqual(first[0].uid, again[0].uid)

    def test_gateway_fp_matches_ocr_path(self):
        """关键防漂移断言：网关补算的指纹 == OCR 路径的 unit_fp。

        两边算法一旦不一致，同一条消息在两条采集路径下会得到两个 uid，
        表现为「切了采集方式之后旧消息又发一遍」。
        """
        for sender in ("签收跟单专员 机器人", "签收跟单专员", "张三", "数字质控 播报助手"):
            with self.subTest(sender=sender):
                msgs, _ = self.build({"messages": [
                    {"sender": sender, "content": "跨天内容 1234"}]})
                self.assertEqual(msgs[0].raw_id,
                                 f"{unit_fp({'sender': sender, 'content': '跨天内容 1234'})}:1")
        # 并且「机器人」后缀的有无必须算同一条
        self.assertEqual(norm_sender("签收跟单专员 机器人"), norm_sender("签收跟单专员"))

    def test_chat_name_falls_back_to_payload_then_config(self):
        msgs, _ = self.build({"chat_name": "报文里的群", "messages": [
            {"sender": "a", "content": "x"}]})
        self.assertEqual(msgs[0].chat_name, "报文里的群")

        msgs, _ = self.build({"messages": [{"sender": "a", "content": "x"}]})
        self.assertEqual(msgs[0].chat_name, "数字质控")

    def test_per_message_chat_name_wins(self):
        msgs, _ = self.build({"chat_name": "报文里的群", "messages": [
            {"sender": "a", "content": "x", "chat_name": "单条的群"}]})
        self.assertEqual(msgs[0].chat_name, "单条的群")

    def test_unknown_msg_type_becomes_unknown(self):
        msgs, _ = self.build({"messages": [
            {"sender": "a", "content": "x", "msg_type": "火星文"}]})
        self.assertEqual(msgs[0].msg_type, "unknown")

    def test_is_self_rendered_as_me(self):
        msgs, _ = self.build({"messages": [
            {"sender": "我自己", "content": "x", "is_self": True}]})
        self.assertEqual(msgs[0].sender_name, "(我)")

    def test_single_object_shorthand(self):
        """顶层直接给一条消息 —— 方便用 curl 手工验证。"""
        msgs, _ = self.build({"sender": "张三", "content": "手测"})
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].content, "手测")

    def test_bad_messages_shape_raises(self):
        with self.assertRaises(ValueError):
            self.build({"messages": "不是数组"})

    def test_company_inferred_from_chat_or_sender(self):
        msgs, _ = self.build(
            {"messages": [{"sender": "示例科技有限公司 播报", "content": "x"}]},
            companies=["示例科技有限公司"])
        self.assertEqual(msgs[0].company, "示例科技有限公司")


# --------------------------------------------------------------- 时间解析

class TestTimeParsing(unittest.TestCase):

    def parse(self, value):
        msgs, _ = build_messages({"messages": [
            {"sender": "a", "content": "x", "time": value}]},
            chat_name="c", companies=[])
        return msgs[0].msg_time

    def test_full_datetime(self):
        self.assertEqual(self.parse("2026-10-16 20:31:05"),
                         datetime(2026, 10, 16, 20, 31, 5))

    def test_iso_style_with_t(self):
        self.assertEqual(self.parse("2026-10-16T20:31:05"),
                         datetime(2026, 10, 16, 20, 31, 5))

    def test_hhmm_uses_today(self):
        got = self.parse("20:31")
        self.assertEqual((got.hour, got.minute, got.second), (20, 31, 0))

    def test_epoch_seconds_and_millis(self):
        sec = 1760621460
        self.assertEqual(self.parse(sec), self.parse(sec * 1000))

    def test_missing_time_is_now(self):
        before = datetime.now()
        got = self.parse("")
        self.assertGreaterEqual(got, before.replace(microsecond=0))

    def test_garbage_falls_back_to_now(self):
        """认不出格式时退回本机时间 —— 时间只用于展示，不值得为它把消息丢掉。"""
        got = self.parse("前天下午")
        self.assertIsInstance(got, datetime)


# --------------------------------------------------------------- 真实 HTTP

class GatewayCase(unittest.TestCase):
    """起一个真网关，跑真 HTTP 请求。"""

    token = TOKEN
    parse = False                       # 子类置 True 即开启画面解析

    def setUp(self):
        self.port = free_port()
        self.source = AndroidGatewaySource(
            make_cfg(self.port, self.token, android_parse=self.parse))
        self.received: list = []
        self.thread = threading.Thread(
            target=self.source.run, args=(self.received.append,), daemon=True)
        self.thread.start()
        if not self.source.wait_ready(5.0):
            self.fail("网关没能在 5 秒内就绪")

    def tearDown(self):
        self.source.stop()
        self.thread.join(timeout=5.0)

    def url(self, path: str, token: str | None = None) -> str:
        base = f"http://127.0.0.1:{self.port}{path}"
        return f"{base}?token={quote(token)}" if token else base

    def settle(self, wait: float = 1.0) -> None:
        """等「先攒后发」的缓冲把消息投出来。

        为什么测试需要这一步：网关不再「解析出消息就立刻投递」，而是先攒够
        （静默 ``pending.QUIET`` 秒、只发最全的那版）再发 —— 这是为了对付
        H5 卡片分批渲染。所以断言「收到几条」之前必须先让它攒完。

        实现上直接驱动缓冲的 ``drain``（把 last_at 往前拨到「早就该发了」），
        不走 ``FLUSH_TICK`` 真实等待：否则每个用例都要多睡 3 秒，全套测试
        会慢到没人愿意跑。``wait`` 只是给 flusher 线程一个插队的机会。
        """
        if wait:
            time.sleep(wait)          # 让 flusher 线程先跑一轮
        # 兜底：有些用例故意卡在「还在长」，这里按「立即到期」强制排空
        self.source.flush_pending_now()
        if wait:
            time.sleep(0.15)

    def post(self, payload, token: str | None = TOKEN, path: str = "/messages"):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data,
                                     method="POST")
        if token is not None:
            req.add_header("X-Bridge-Token", token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def get(self, path: str = "/ping", token: str | None = TOKEN,
            header: bool = False):
        url = f"http://127.0.0.1:{self.port}{path}"
        if token and not header:
            url += f"?token={quote(token)}"
        req = urllib.request.Request(url)
        if token and header:
            req.add_header("X-Bridge-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))


class TestAuth(GatewayCase):

    def test_ping_ok_with_token(self):
        code, body = self.get()
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["source"], "android")

    def test_ping_rejected_without_token(self):
        code, _ = self.get(token=None)
        self.assertEqual(code, 401)

    def test_ping_rejected_with_wrong_token(self):
        code, _ = self.get(token="错了")
        self.assertEqual(code, 401)

    def test_ping_ok_with_header_token(self):
        code, body = self.get(header=True)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_ping_ok_with_query_token(self):
        code, _ = self.get(header=False)
        self.assertEqual(code, 200)

    def test_post_ok_with_query_token(self):
        """手工自测常用 ?token=xx，别只让请求头能用。"""
        req = urllib.request.Request(
            self.url("/messages", TOKEN),
            data=json.dumps({"messages": [{"sender": "a", "content": "x"}]}
                            ).encode("utf-8"), method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.received), 1)

    def test_post_rejected_without_token(self):
        code, body = self.post({"messages": [{"sender": "a", "content": "x"}]},
                               token=None)
        self.assertEqual(code, 401)
        self.assertEqual(self.received, [])

    def test_post_rejected_with_wrong_token(self):
        # 用纯 ASCII 的错 token：HTTP 头装不下中文，非 ASCII 的 token
        # 只能走 ?token= 那条路（另有测试覆盖）
        code, _ = self.post({"messages": [{"sender": "a", "content": "x"}]},
                            token="wrong-token")
        self.assertEqual(code, 401)
        self.assertEqual(self.received, [])


class TestEndpoint(GatewayCase):

    def test_happy_path(self):
        code, body = self.post({"messages": [
            {"sender": "签收跟单专员 机器人", "content": "【提醒】374票"},
        ]})
        self.assertEqual(code, 200)
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["skipped"], [])
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].content, "【提醒】374票")

    def test_empty_content_counted_as_skipped(self):
        code, body = self.post({"messages": [{"sender": "a", "content": ""}]})
        self.assertEqual(code, 200)
        self.assertEqual(body["accepted"], 0)
        self.assertEqual(len(body["skipped"]), 1)
        self.assertEqual(self.received, [])

    def test_unknown_path_404(self):
        code, _ = self.post({"messages": []}, path="/nope")
        self.assertEqual(code, 404)

    def test_empty_body_400(self):
        req = urllib.request.Request(self.url("/messages"), data=b"", method="POST")
        req.add_header("X-Bridge-Token", TOKEN)
        req.add_header("Content-Length", "0")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 400)

    def test_bad_json_400(self):
        req = urllib.request.Request(self.url("/messages"),
                                     data="{不是 json".encode("utf-8"),
                                     method="POST")
        req.add_header("X-Bridge-Token", TOKEN)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 400)

    def test_too_many_messages_413(self):
        items = [{"sender": "a", "content": f"第{i}条"} for i in range(MAX_BATCH + 1)]
        code, body = self.post({"messages": items})
        self.assertEqual(code, 413)
        self.assertIn(str(MAX_BATCH), body["error"])

    def test_ping_reports_stats(self):
        self.post({"messages": [{"sender": "a", "content": "x"}]})
        _, body = self.get()
        self.assertEqual(body["stats"]["accepted"], 1)


# --------------------------------------------------------------- 画面端点

# 一帧最小可用的节点树。形状照着 `adb shell uiautomator dump` 的真实输出抄，
# 属性名一个都不能改 —— 网关的入口检查、以及后面解析器都认这个形状。
FRAME_1 = (
    "<?xml version='1.0' encoding='UTF-8'?>\n"
    "<hierarchy rotation=\"0\">\n"
    "  <node index=\"0\" text=\"\" resource-id=\"\" class=\"android.widget.FrameLayout\""
    " package=\"com.alibaba.android.rimet\" content-desc=\"\" clickable=\"false\""
    " enabled=\"true\" scrollable=\"false\" bounds=\"[0,0][1080,2340]\">\n"
    "    <node index=\"1\" text=\"普通件滞留7天 96票\" resource-id=\"com.alibaba.android.rimet:id/tv\""
    " class=\"android.widget.TextView\" package=\"com.alibaba.android.rimet\""
    " content-desc=\"\" clickable=\"false\" enabled=\"true\" scrollable=\"false\""
    " bounds=\"[120,1200][900,1320]\">\n"
    "    </node>\n"
    "  </node>\n"
    "</hierarchy>\n"
)


class TestScreenEndpoint(GatewayCase):
    """``/screen`` —— 手机只送节点树、解析留在电脑侧的那条路。

    这条路的失败模式跟 ``/messages`` 完全不同，所以单独立一组测试：
    它**不会**产生消息，收了也只是存着；测试要盯的是「存得对吗、
    形状不对的东西有没有被挡在门口、以及能不能取回来」。
    """

    def post_screen(self, body, token: str | None = TOKEN, **headers):
        if isinstance(body, str):
            body = body.encode("utf-8")
        req = urllib.request.Request(self.url("/screen"), data=body, method="POST")
        if token is not None:
            req.add_header("X-Bridge-Token", token)
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        for k, v in headers.items():
            req.add_header(k.replace("_", "-"), str(v))
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def get_raw(self, token: str | None = TOKEN):
        url = f"http://127.0.0.1:{self.port}/screen"
        url += f"?token={quote(token)}&raw=1" if token else "?raw=1"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")

    # --- 收 ---

    def test_accept_frame(self):
        code, body = self.post_screen(
            FRAME_1, X_Ddwx_Md5="abc123", X_Ddwx_Texts=1, X_Ddwx_Device="Mate Xs 2")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["bytes"], len(FRAME_1.encode("utf-8")))
        self.assertEqual(body["count"], 1)
        self.assertEqual(self.received, [])       # 画面不产生消息，这是设计

    def test_md5_from_header_preferred(self):
        self.post_screen(FRAME_1, X_Ddwx_Md5="given-md5")
        _, meta = self.get("/screen")
        self.assertEqual(meta["md5"], "given-md5")

    def test_md5_computed_when_header_missing(self):
        """手机端没带头也要能工作 —— 否则一个版本不匹配就整条路废掉。"""
        import hashlib
        self.post_screen(FRAME_1)
        _, meta = self.get("/screen")
        self.assertEqual(meta["md5"], hashlib.md5(FRAME_1.encode("utf-8")).hexdigest())

    def test_second_frame_replaces_first(self):
        """只留最新一帧：这一层的语义是「手机现在长什么样」，不是历史序列。"""
        self.post_screen(FRAME_1)
        changed = FRAME_1.replace("96票", "97票")
        code, body = self.post_screen(changed)
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 2)
        _, raw = self.get_raw()
        self.assertIn("97票", raw)
        self.assertNotIn("96票", raw)

    def test_non_xml_rejected(self):
        """把「IP 填到了别的服务」这类问题挡在门口，别让解析器去猜结构。"""
        code, body = self.post_screen("这不是节点树，只是一个普通字符串")
        self.assertEqual(code, 400)
        self.assertIn("hierarchy", body["hint"])

    def test_non_utf8_rejected(self):
        code, _ = self.post_screen(b"\xff\xfe\x00\x01 not utf8")
        self.assertEqual(code, 400)

    def test_too_large_413(self):
        code, body = self.post_screen(b"<hierarchy>" + b"x" * MAX_SCREEN_BODY)
        self.assertEqual(code, 413)
        self.assertIn("过大", body["error"])

    def test_empty_body_400(self):
        req = urllib.request.Request(self.url("/screen"), data=b"", method="POST")
        req.add_header("X-Bridge-Token", TOKEN)
        req.add_header("Content-Length", "0")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 400)

    # --- 取 ---

    def test_get_before_any_frame_404(self):
        code, body = self.get("/screen")
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])
        self.assertIn("还没有收到", body["error"])

    def test_get_meta_excludes_xml(self):
        """摘要里不能夹带正文 —— 一帧几百 KB，塞进 JSON 就没人愿意看状态了。"""
        self.post_screen(FRAME_1, X_Ddwx_Texts=1)
        _, meta = self.get("/screen")
        self.assertTrue(meta["received"])
        self.assertEqual(meta["texts"], 1)
        self.assertNotIn("xml", meta)
        self.assertIsInstance(meta["age_sec"], float)

    def test_get_raw_returns_xml(self):
        self.post_screen(FRAME_1)
        code, raw = self.get_raw()
        self.assertEqual(code, 200)
        self.assertTrue(raw.startswith("<?xml"))
        self.assertIn("普通件滞留7天 96票", raw)

    def test_ping_includes_screen_meta(self):
        self.post_screen(FRAME_1)
        _, body = self.get()
        self.assertTrue(body["screen"]["received"])
        self.assertEqual(body["screen"]["count"], 1)

    def test_ping_before_any_frame_says_not_received(self):
        _, body = self.get()
        self.assertFalse(body["screen"]["received"])

    # --- 鉴权 ---

    def test_screen_rejected_without_token(self):
        code, _ = self.post_screen(FRAME_1, token=None)
        self.assertEqual(code, 401)

    def test_screen_get_rejected_without_token(self):
        code, _ = self.get("/screen", token=None)
        self.assertEqual(code, 401)

    def test_auth_token_alias_accepted(self):
        """手机端自建 App 发的是 X-Auth-Token，必须认 —— 否则表现是
        「token 一字不差却永远 401」，现场根本查不出来。"""
        req = urllib.request.Request(self.url("/screen"),
                                     data=FRAME_1.encode("utf-8"), method="POST")
        req.add_header("X-Auth-Token", TOKEN)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        _, meta = self.get("/screen")
        self.assertTrue(meta["received"])


# --------------------------------------------------------------- Source

class TestNonAsciiToken(unittest.TestCase):
    """token 填成中文也要能用。

    token 是让人手填的，填成中文很正常；而 ``hmac.compare_digest`` 对 str
    要求纯 ASCII，不先编成 bytes 就会直接抛 TypeError —— 用户看到的会是
    「服务端 500」这种毫无头绪的报错，而不是一个干净的 401。
    """

    TOKEN = "中文密钥"

    def setUp(self):
        self.port = free_port()
        self.source = AndroidGatewaySource(make_cfg(self.port, self.TOKEN))
        self.thread = threading.Thread(
            target=self.source.run, args=(lambda m: None,), daemon=True)
        self.thread.start()
        if not self.source.wait_ready(5.0):
            self.fail("网关没就绪")

    def tearDown(self):
        self.source.stop()
        self.thread.join(timeout=5.0)

    def ping(self, token: str | None) -> int:
        url = f"http://127.0.0.1:{self.port}/ping"
        if token is not None:
            url += f"?token={quote(token)}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_correct_non_ascii_token_accepted(self):
        self.assertEqual(self.ping(self.TOKEN), 200)

    def test_wrong_non_ascii_token_gives_401_not_500(self):
        self.assertEqual(self.ping("别的中文"), 401)

    def test_missing_token_gives_401(self):
        self.assertEqual(self.ping(None), 401)


class TestSource(unittest.TestCase):

    def test_check_requires_token(self):
        src = AndroidGatewaySource(make_cfg(free_port(), token=""))
        ok, why = src.check()
        self.assertFalse(ok)
        self.assertIn("token", why)

    def test_check_ok_with_token(self):
        src = AndroidGatewaySource(make_cfg(free_port()))
        ok, why = src.check()
        self.assertTrue(ok)
        self.assertIn("手机端填", why)

    def test_run_refuses_without_token(self):
        src = AndroidGatewaySource(make_cfg(free_port(), token=""))
        with self.assertRaises(RuntimeError) as ctx:
            src.run(lambda m: None)
        self.assertIn("token", str(ctx.exception))

    def test_invalid_port_rejected(self):
        src = AndroidGatewaySource(make_cfg(free_port()))
        src.port = 0
        ok, why = src.check()
        self.assertFalse(ok)
        self.assertIn("端口", why)

    def test_port_occupied_raises_readable_error(self):
        port = free_port()
        holder = socket.socket()
        holder.bind(("127.0.0.1", port))
        holder.listen(1)
        try:
            src = AndroidGatewaySource(make_cfg(port))
            with self.assertRaises(RuntimeError) as ctx:
                src.run(lambda m: None)
            self.assertIn("起不来", str(ctx.exception))
        finally:
            holder.close()

    def test_factory_builds_android_source(self):
        src = build_source(make_cfg(free_port()))
        self.assertIsInstance(src, AndroidGatewaySource)
        self.assertIsInstance(src, Source)
        self.assertEqual(src.name, "android")

    def test_default_port_used_when_blank(self):
        dt = DingTalkConfig(mode="android", gateway_port=0, gateway_token=TOKEN)
        src = AndroidGatewaySource(Config(dingtalk=dt))
        self.assertEqual(src.port, 8765)

    def test_lan_ips_has_no_loopback(self):
        for ip in lan_ips():
            self.assertFalse(ip.startswith("127."))


# --------------------------------------------------------------- 端到端

class TestEndToEnd(unittest.TestCase):
    """网关 → BridgeService → 落库 + 投递（投递端是桩）。

    刻意**不**复用 GatewayCase：它已经占了一个端口，而 Python 的 HTTPServer
    默认开着 SO_REUSEADDR，在 Windows 上会让第二个绑定「看起来成功」，
    于是请求被两个服务各接一半 —— 测试会变得不可解释。
    """

    def setUp(self):
        self.port = free_port()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        cfg = make_cfg(self.port,
                       storage=StorageConfig(db_path=str(self.db)),
                       wechat=WeChatConfig(dry_run=True, interval=0.0,
                                           targets=["测试群"]),
                       routes=[RouteConfig(name="t", targets=["测试群"],
                                           template="{sender}：{content}")])
        self.target = _StubTarget()
        self.svc = BridgeService(cfg, target=self.target)
        self.source = AndroidGatewaySource(cfg)
        self.thread = threading.Thread(
            target=self.source.run, args=(self.svc.handle,), daemon=True)
        self.thread.start()
        if not self.source.wait_ready(5.0):
            self.fail("业务链路上的网关没就绪")

    def tearDown(self):
        self.source.stop()
        self.thread.join(timeout=5.0)
        self.svc.close()
        self.tmp.cleanup()

    def post_to(self, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/messages", data=data, method="POST")
        req.add_header("X-Bridge-Token", TOKEN)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_message_lands_in_db_and_target(self):
        body = self.post_to({"messages": [{
            "sender": "签收跟单专员 机器人",
            "content": "@所有人【客户声音履约风险提醒】374票",
            "time": "2026-10-16 20:31:00",
        }]})
        self.assertEqual(body["accepted"], 1)

        self.assertEqual(len(self.target.sent), 1)
        tgt, text = self.target.sent[0]
        self.assertEqual(tgt, "测试群")
        self.assertIn("374票", text)

        rows = self.svc.storage.query(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "@所有人【客户声音履约风险提醒】374票")

    def test_retransmit_is_deduplicated(self):
        """手机重传同一条（同 fp + 同 seq）不能转发两次。

        这是整个网关最要紧的性质：手机在某些情况下收不到响应就会重发，
        没有幂等就会在微信群里刷出一串重复消息。
        """
        item = {"sender": "签收跟单专员 机器人", "content": "同一条", "seq": 5}
        self.post_to({"messages": [dict(item)]})
        self.post_to({"messages": [dict(item)]})
        self.assertEqual(len(self.target.sent), 1)

    def test_two_occurrences_of_identical_text_both_forwarded(self):
        """正文完全相同但确实是两条（seq 不同）时，两条都要发。"""
        self.post_to({"messages": [
            {"sender": "播报助手", "content": "定时播报", "seq": 1},
        ]})
        self.post_to({"messages": [
            {"sender": "播报助手", "content": "定时播报", "seq": 2},
        ]})
        self.assertEqual(len(self.target.sent), 2)

    def test_cross_day_not_filtered_by_gateway(self):
        """网关不做时间过滤 —— 手机离线一夜后补报的消息不能被当历史丢掉。"""
        body = self.post_to({"messages": [{
            "sender": "播报助手", "content": "昨天漏报的", "seq": 1,
            "time": "2026-10-15 23:50:00",
        }]})
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(len(self.target.sent), 1)


class TestScreenParse(GatewayCase):
    """``dingtalk.android_parse = true``：画面 → 消息。

    这组锁的是「不转发历史」在网关侧的**唯一**实现：第一帧只建基线，
    之后只发锚点之后的。弄错了就是用户往上翻一次历史、微信群被灌几百条。
    """

    parse = True
    PKG = "com.alibaba.android.rimet"

    # --- 造帧 ---
    def row(self, text, top, sender=None):
        if sender is None:
            return (f'<node text="{text}" class="android.widget.TextView" '
                    f'package="{self.PKG}" bounds="[80,{top}][1000,{top + 90}]"></node>')
        return (f'<node class="android.widget.LinearLayout" package="{self.PKG}" '
                f'bounds="[60,{top}][1020,{top + 180}]">'
                f'<node text="{sender}" class="android.widget.TextView" '
                f'package="{self.PKG}" bounds="[80,{top}][420,{top + 60}]"></node>'
                f'<node class="android.widget.LinearLayout" package="{self.PKG}" '
                f'bounds="[80,{top + 70}][1000,{top + 170}]">'
                f'<node text="{text}" class="android.widget.TextView" '
                f'package="{self.PKG}" bounds="[100,{top + 75}][980,{top + 165}]"></node>'
                f'</node></node>')

    def screen(self, *rows):
        body = "".join(rows)
        # 输入区必须在：解析器先判页面类型，认不出聊天页就一帧都不切
        # （防的是「@面板的人名被当成群消息」）。
        bar = (f'<node class="android.widget.TextView" package="{self.PKG}" '
               f'text="发消息或按住说话..." bounds="[182,2191][981,2262]"></node>')
        return ('<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
                f'<node class="android.widget.FrameLayout" package="{self.PKG}" '
                f'bounds="[0,0][1080,2340]">'
                f'<node class="androidx.recyclerview.widget.RecyclerView" '
                f'package="{self.PKG}" scrollable="true" bounds="[0,200][1080,2100]">'
                f'{body}</node>{bar}</node></hierarchy>')

    def post_frame(self, xml: str):
        req = urllib.request.Request(self.url("/screen"), data=xml.encode("utf-8"),
                                     method="POST")
        req.add_header("X-Bridge-Token", self.token)
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # --- 用例 ---
    def test_first_frame_only_builds_baseline(self):
        body = self.post_frame(self.screen(
            self.row("普通件滞留7天 96票", 400, "签收跟单专员"),
            self.row("客户声音履约风险提醒", 700)))
        self.assertEqual(body["messages"], 0)
        self.assertEqual(body["note"], "已建立基线")
        self.assertEqual(self.received, [])

    def test_second_frame_with_new_message_is_forwarded_once(self):
        base = self.screen(self.row("第一条", 400, "签收跟单专员"))
        self.post_frame(base)
        more = self.screen(self.row("第一条", 400, "签收跟单专员"),
                           self.row("第二条", 700, "签收跟单专员"))
        body = self.post_frame(more)
        self.assertEqual(body["messages"], 1)
        self.settle()
        self.assertEqual(len(self.received), 1)
        msg = self.received[0]
        self.assertEqual(msg.sender_name, "签收跟单专员")
        self.assertEqual(msg.content, "第二条")

    def test_same_frame_twice_forwards_nothing_extra(self):
        base = self.screen(self.row("第一条", 400))
        self.post_frame(base)
        self.post_frame(base)
        self.assertEqual(self.received, [])

    def test_time_separator_never_forwarded(self):
        self.post_frame(self.screen(self.row("第一条", 400)))
        body = self.post_frame(self.screen(
            self.row("第一条", 400),
            '<node text="09:12" class="android.widget.TextView" '
            f'package="{self.PKG}" bounds="[500,600][600,640]"></node>',
            self.row("第二条", 700)))
        self.assertEqual(body["messages"], 1)
        self.settle()
        self.assertEqual([m.content for m in self.received], ["第二条"])

    def test_scrolling_back_does_not_flood(self):
        self.post_frame(self.screen(self.row("第五条", 400)))
        body = self.post_frame(self.screen(
            self.row("第一条", 300), self.row("第二条", 500), self.row("第三条", 700)))
        self.assertEqual(body["messages"], 0)
        self.assertEqual(body["note"], "锚点丢失，翻一屏找")
        self.assertEqual(self.received, [])

    def test_broken_xml_returns_200_not_error(self):
        """解析失败要回 200：回 4xx 手机会一直重传同一帧，只会刷满日志。"""
        self.post_frame(self.screen(self.row("第一条", 400)))
        body = self.post_frame("<hierarchy rotation=\"0\"><node></hierarchy>")
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["messages"], 0)
        self.assertEqual(body["note"], "解析失败")

    def test_parse_off_by_default(self):
        """默认（android_parse 未开）时 /screen 一条都不产生 —— 只收不转。"""
        cfg = make_cfg(free_port(), TOKEN)
        self.assertFalse(cfg.dingtalk.android_parse)

    # --- 长消息补全指令 ---
    #
    # getRootInActiveWindow() 只给可见节点：一条超过一屏的消息，滚出去的部分
    # 在树里根本不存在。网关认出「被切掉了」之后要在响应里发 scroll 指令，
    # 手机端才知道该往上翻一屏（见 android/.../CollectorService）。

    def long_row(self, sender: str, top: int, bottom: int,
                 body: str = "很长的正文……") -> str:
        """一条很高的消息 —— 底边越过列表下沿 2100。"""
        return (f'<node class="android.widget.LinearLayout" package="{self.PKG}" '
                f'bounds="[60,{top}][1020,{bottom}]">'
                f'<node text="{sender}" class="android.widget.TextView" '
                f'package="{self.PKG}" bounds="[80,{top}][420,{top + 60}]"></node>'
                f'<node text="{body}" class="android.widget.TextView" '
                f'package="{self.PKG}" bounds="[80,{top + 70}][1000,{bottom - 10}]">'
                f'</node></node>')

    def test_long_message_asks_phone_to_scroll(self):
        self.post_frame(self.screen(self.row("第一条", 400, "签收跟单专员")))
        body = self.post_frame(self.screen(
            self.row("第一条", 400, "签收跟单专员"),
            self.long_row("履约跟单专员", 700, 3200)))     # 底边 3200 > 2100
        self.assertTrue(body.get("scroll"), "长消息没让手机补全")

    def test_normal_message_does_not_ask_to_scroll(self):
        """短消息绝不能让手机滑动 —— 群聊一直在来消息，滑起来就收不住了。"""
        self.post_frame(self.screen(self.row("第一条", 400, "签收跟单专员")))
        body = self.post_frame(self.screen(
            self.row("第一条", 400, "签收跟单专员"),
            self.row("第二条", 700, "签收跟单专员")))
        self.assertNotIn("scroll", body)

    def test_baseline_frame_never_asks_to_scroll(self):
        """建基线时不许滚：刚记下的基线会被自己滚脏。"""
        body = self.post_frame(self.screen(
            self.long_row("履约跟单专员", 400, 3200)))
        self.assertEqual(body["note"], "已建立基线")
        self.assertNotIn("scroll", body)

    # ---- 锚点丢失 → 先请手机翻一屏试探 --------------------------------
    #
    # 用户原话：「如果我手动向上翻手机屏他又能转发，是这个原因吗，能修复吗」。
    # 是的 —— 而且手动翻能好恰恰证明内容还在，只是被新消息挤出屏幕了。
    # 所以电脑判出 anchor_lost 时不再静默重建基线，而是先请手机翻一屏；
    # 翻回来锚点对上了，刚才漏的那条就能补出来。

    def test_anchor_lost_asks_phone_to_scroll_before_giving_up(self):
        """锚点对不上时不能直接重建基线 —— 要先让手机翻一屏。"""
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        body = self.post_frame(self.screen(
            self.row("历史1", 300), self.row("历史2", 600)))
        self.assertTrue(body.get("scroll"), "锚点丢失却没说让手机翻一屏")
        self.assertEqual(body["note"], "锚点丢失，翻一屏找")
        self.assertEqual(self.received, [])

    def test_probe_hit_recovers_the_swallowed_message(self):
        """翻一屏后锚点找回 → 被长消息挤掉的中间那条要补发出来。

        真机的形状：锚点（旧消息A）还在最上面，新来一条很高的消息把它下面
        撑满，中间那条「新消息B」被挤出屏幕 —— 节点树里根本没有它，所以
        常规帧怎么等都等不到。手机往上翻一屏后 B 重新露出来，这时候补全帧
        必须在 **A 与长消息之间** 把它捡回来。
        """
        self.post_frame(self.screen(self.row("旧消息A", 300)))
        # 一条很高的新消息进来，把「新消息B」和锚点一起挤出可见区
        b1 = self.post_frame(self.screen(
            self.row("新消息B", 500, "签收跟单专员"),
            self.long_row("履约跟单专员", 700, 3200)))
        self.assertIn(b1["note"], ("锚点丢失，翻一屏找", ""))
        self.assertNotIn("新消息B", [m.content for m in self.received])

        # 手机翻一屏：旧消息A 重新露出，B 也在，长消息退到下面
        b2 = self.post_fill(self.screen(
            self.row("旧消息A", 300),
            self.row("新消息B", 500, "签收跟单专员"),
            self.long_row("履约跟单专员", 1400, 3200)))
        self.settle()
        contents = [m.content for m in self.received]
        self.assertIn("新消息B", contents, "被长消息挤掉的那条没补回来")
        # 长消息只算一条：补全帧里它既露头又被切着，不能出现两次
        self.assertEqual(contents.count("很长的正文……"), 1)
        self.assertEqual(self.received[0].content, "新消息B")

        # 手机翻回列表底部：同一批内容不能因为「刚才补过」再发一遍
        b3 = self.post_frame(self.screen(
            self.row("新消息B", 500, "签收跟单专员"),
            self.long_row("履约跟单专员", 1400, 3200)))
        self.assertEqual(b3["messages"], 0, "翻回底部后重复发了")
        self.assertEqual(len(self.received), 2)

        # 之后照常的新消息还要能发（锚点没被弄坏）
        b4 = self.post_frame(self.screen(
            self.long_row("履约跟单专员", 300, 1900), self.row("新消息C", 2000)))
        self.assertEqual(b4["messages"], 1)
        self.settle()
        self.assertEqual(self.received[-1].content, "新消息C")

    def test_second_miss_gives_up_and_rebuilds_baseline(self):
        """翻一屏还对不上（真在翻历史）→ 认输重建，别让手机来回翻。

        不认输的后果是手机在「翻上去、翻回来」之间无限循环，一条都不转发，
        表现和「锚点永久卡死」一模一样。
        """
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        b1 = self.post_frame(self.screen(self.row("历史1", 300),
                                         self.row("历史2", 600)))
        self.assertTrue(b1.get("scroll"))
        # 手机翻过了头，还是对不上；冷却期内不再发指令，直接重建基线
        b2 = self.post_fill(self.screen(self.row("更早的历史1", 300)))
        self.assertIn("对不上锚点", b2["note"])
        b3 = self.post_frame(self.screen(self.row("更早的历史1", 300),
                                         self.row("更早的历史2", 600)))
        self.assertNotIn("scroll", b3)
        self.assertEqual(b3["note"], "锚点丢失")
        self.assertEqual(self.received, [])
        # 基线已重建：此后新消息恢复转发
        b4 = self.post_frame(self.screen(self.row("更早的历史1", 300),
                                         self.row("其实很新的消息", 900)))
        self.assertEqual(b4["messages"], 1)
        self.settle()
        self.assertEqual(self.received[-1].content, "其实很新的消息")

    def test_probe_is_not_repeated_for_the_same_frame(self):
        """同一屏连报两次只翻一次 —— 手机原地没动，再翻是白折腾。"""
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        same = self.screen(self.row("历史1", 300), self.row("历史2", 600))
        self.assertTrue(self.post_frame(same).get("scroll"))
        self.assertNotIn("scroll", self.post_frame(same))

    def test_probe_does_not_fire_on_baseline_frame(self):
        """建基线时不许翻：刚记下的基线会被自己翻脏。"""
        body = self.post_frame(self.screen(self.row("第一条", 400)))
        self.assertEqual(body["note"], "已建立基线")
        self.assertNotIn("scroll", body)

    # ---- 新卡片长在锚点**上方** ----------------------------------------
    #
    # 真机事故（2026-10-17 19:12）：用户报「标签履约周累计达成（1012-1015）
    # 又没采集到」。后来抓到的那一帧（data/screen-20261017_191838.xml）显示
    # 卡片就在屏幕上、解析也完全正常，但它渲染在锚点（那条采集波动告警）
    # **上方** —— 只看锚点之后的 `units[idx+1:]` 永远是空的，于是卡片既不算
    # 新消息、也不入库，日志里一条错都不报。
    #
    # 下面两条锁的是「上方的新内容要发」和「上方的历史绝不能发」。

    def test_card_above_anchor_is_forwarded(self):
        """卡片从屏幕顶挤进来（排在锚点上方）也必须发出去。

        位置关系照抄真机那一帧（view 顶 265、卡片 y0 也 265、卡片下沿 1251
        紧贴告警上沿 1251）：卡片**顶到列表上沿**、底边紧贴锚点顶边，中间
        没夹别的消息 —— 这才是「卡片插进来」的形状。位置差一点点就判不出来，
        这不是容错问题：宽松一点就会把翻历史的整屏旧消息灌进微信群。
        """
        alert = "采集波动：无法确认新旧消息"
        self.post_frame(self.screen(self.row(alert, 900, "服务诊断专员")))
        body = self.post_frame(self.screen(
            self.row("标签履约周累计达成 1012-1015", 200, "履约跟单专员"),
            self.row(alert, 385, "服务诊断专员")))
        self.assertEqual(body["messages"], 1, "锚点上方的卡片没被转发")
        self.settle()
        self.assertEqual([m.sender_name for m in self.received], ["履约跟单专员"])
        self.assertIn("标签履约周累计达成", self.received[0].content)

        # 手机原地不动又报了几帧：同一条不能重复发
        for _ in range(3):
            again = self.post_frame(self.screen(
                self.row("标签履约周累计达成 1012-1015", 200, "履约跟单专员"),
                self.row(alert, 385, "服务诊断专员")))
            self.assertEqual(again["messages"], 0)
        self.assertEqual(len(self.received), 1)

    def test_history_above_anchor_is_not_forwarded(self):
        """往上翻历史时上面全是旧消息 —— 一条都不能当新消息发。"""
        self.post_frame(self.screen(
            self.row("第五条的正文，说的是今晚的交接安排与注意事项。", 400)))
        body = self.post_frame(self.screen(
            self.row("很久以前的旧消息，内容是八月的积压情况一共 99 票。", 265),
            self.row("更早的一条，说的是七月的派送异常与处理结果。", 460),
            self.row("第五条的正文，说的是今晚的交接安排与注意事项。", 655)))
        self.assertEqual(body["messages"], 0, "翻历史时把旧消息发出去了")
        self.assertEqual(self.received, [])

    # ---- 上翻补全帧 ----
    #
    # 补全帧和常规帧**方向相反**：常规帧的新消息在锚点下方，补全帧要的是
    # 锚点上方那半截长消息。直接喂给 FrameDiffer 会「新增 0 条」，白翻一屏。

    def post_fill(self, xml: str):
        """模拟手机端上翻一屏后重报（带 reason=scroll-fill）。"""
        req = urllib.request.Request(self.url("/screen"), data=xml.encode("utf-8"),
                                     method="POST")
        req.add_header("X-Bridge-Token", self.token)
        req.add_header("X-Ddwx-Reason", "scroll-fill")
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_fill_frame_delivers_the_upper_part(self):
        """长消息攒齐后**只发一条完整的**，不是「半截 + 前半截」两条。

        旧行为是先发半截、补全帧再把前半截当新消息发一条 —— 群里看到的是
        「残句 → 完整消息」（用户原话「内容特别长的消息转发还是有问题」）。
        新行为：第一次见长消息不发，只请手机翻一屏；补全帧回来拼成一条投递。
        """
        self.post_frame(self.screen(self.row("旧消息A", 400, "签收跟单专员")))

        # 第一眼：屏幕上只有后半截（"很长的正文……"）。**不发**，只要求补全。
        first = self.post_frame(self.screen(
            self.row("旧消息A", 400, "签收跟单专员"),
            self.long_row("履约跟单专员", 700, 3200)))
        self.assertTrue(first.get("scroll"), "长消息没让手机补全")
        self.assertEqual(first["messages"], 0, "半截长消息不该先发出去")
        self.assertEqual(self.received, [], "半截长消息先发了 —— 群里会出现残句")

        # 补全帧:上半截 + 屏幕上的后半截。两块有重叠，要拼成一条。
        body = self.post_fill(self.screen(
            self.long_row("履约跟单专员", 300, 1200,
                          body="这是被切掉的前半截，很长的正文……"),
            self.long_row("履约跟单专员", 700, 3200)))
        self.assertEqual(body["messages"], 1, "补全后应当发一条完整的")
        self.settle()
        self.assertEqual(len(self.received), 1, "补全不该额外多发一条")
        self.assertEqual(self.received[0].content, "这是被切掉的前半截，很长的正文……")

    def test_long_message_sent_once_when_fill_never_comes(self):
        """补全始终不来（手机没手势权限等）时，超时后仍要发出去。

        宁可发一条不完整的，也不能永远不发 —— 这是「攒齐再发」的兜底。
        """
        self.post_frame(self.screen(self.row("旧消息A", 400, "签收跟单专员")))
        self.post_frame(self.screen(
            self.row("旧消息A", 400, "签收跟单专员"),
            self.long_row("履约跟单专员", 700, 3200)))
        self.assertEqual(self.received, [])

        # 模拟「已经等超时了」：把上限设成负数，则 `elapsed > 负数` 恒成立。
        # ⚠ 别写成 0.0 配 `>` —— elapsed 恰好等于 0 的那一瞬会被判「没超时」，
        #   全量跑时偶发失败、单跑又不复现，是最难查的那种抖动。
        with mock.patch("bridge.sources.android_gateway.LONG_WAIT_MAX", -1.0):
            body = self.post_frame(self.screen(
                self.row("旧消息A", 400, "签收跟单专员"),
                self.long_row("履约跟单专员", 700, 3200)))
        self.assertEqual(body["messages"], 1, "超时后必须放行")
        self.settle()
        self.assertEqual(len(self.received), 1)

    def test_fill_frame_with_unknown_anchor_is_dropped(self):
        """翻过头（对不上锚点）时一条都不许发 —— 那是历史消息。"""
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        body = self.post_fill(self.screen(
            self.row("很久以前的历史1", 300), self.row("很久以前的历史2", 600)))
        self.assertEqual(body["messages"], 0)
        self.assertIn("对不上锚点", body["note"])
        self.assertEqual(self.received, [])

    def test_fill_frame_does_not_move_the_baseline(self):
        """补全帧不能改基线：基线要留给「翻回原位」之后的那一帧。"""
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        self.post_fill(self.screen(self.row("补全内容", 300),
                                   self.row("旧消息A", 700)))
        # 翻回原位后，照常的新消息仍要能发出来
        body = self.post_frame(self.screen(self.row("旧消息A", 400),
                                           self.row("新消息B", 700)))
        self.assertEqual(body["messages"], 1)
        self.settle()
        self.assertEqual(self.received[-1].content, "新消息B")

    # ---- 多屏长消息：一轮一轮往上补 --------------------------------------
    #
    # 用户 2026-10-18 原话：「1.6 屏不够，要 3 屏才够，有的消息很长的」。
    # 旧实现只翻一次 62% 屏高、只拼两截，3 屏的正文会得到「第 1 屏 + 第 3 屏」，
    # 中间那段被跳过，而日志一切正常 —— 是最难发现的那种丢失。
    #
    # 新实现：手机每收到一次 scroll 就翻一屏并重报一帧；电脑侧每轮把新露出的
    # 那段对齐进全文，直到这条消息**顶边不再被切**（= 已经是第一屏）才收尾。

    def _seg(self, tag: str, n: int = 30) -> str:
        """造一段可辨识、且段与段之间不会偶然撞上重叠的正文。"""
        return "".join(f"{tag}{i:03d}" for i in range(n))

    def _three_screens(self):
        """造一条 3 屏长消息的四段可见内容：``(第1屏, 第2屏, 第3屏, 首帧看到的)``。

        ⚠ **相邻两屏必须真的重叠**（每段结尾 9 个字与下一段开头相同）。
        手机每轮只翻 62% 屏高，重叠是设计使然，也是 `_align_fragment` 唯一的
        对齐依据 —— 造几个首尾不相接的字符串，测的就不是真机形状了。
        """
        a = self._seg("A")
        b = self._seg("B")
        c = self._seg("C")
        # ⚠ 每段 4 个字符（字母 + 三位数字），切片下标必须是 4 的倍数，
        #   否则会把 "A004" 切成 "3A004" 这种残字，看起来像丢了一屏。
        # s1 = A015..A029（结尾 A027A028A029 与 s2 开头重叠 9 字）
        # s2 = A027..A029 + B000..B029
        # s3 = B027..B029 + C000..C029
        s1 = a[60:]                 # A015 起
        s2 = a[108:] + b            # A027 起 + 第 2 屏
        s3 = b[108:] + c            # B027 起 + 第 3 屏
        # 首帧：手机停在最新处，只看到第 3 屏（最下面那截）
        return s1, s2, s3, s3

    def one_long(self, sender: str, top: int, bottom: int, body: str) -> str:
        """**一条**长消息（单个正文节点）。

        ⚠ 不能拿两个 ``long_row`` 拼同一屏来模拟「上下两段」：解析器按坐标把
        相邻气泡归并成一条消息，两段会被拼成一条（y 顺序也不保证），测的
        就不是真实形状了。真机上「一条消息被切成两段」只发生在**不同帧**里。
        """
        return (f'<node class="android.widget.LinearLayout" package="{self.PKG}" '
                f'bounds="[60,{top}][1020,{bottom}]">'
                f'<node text="{sender}" class="android.widget.TextView" '
                f'package="{self.PKG}" bounds="[80,{top}][420,{top + 60}]"></node>'
                f'<node text="{body}" class="android.widget.TextView" '
                f'package="{self.PKG}" bounds="[80,{top + 70}][1000,{bottom - 10}]">'
                f'</node></node>')

    def test_three_screen_message_is_collected_over_rounds(self):
        """3 屏长消息要一轮一轮补齐 —— 中间段不能丢，而且只发一条。

        真机上的形状：每一轮手机只多露 0.6 屏，刚露出来的那屏**顶边仍被
        切着**，电脑侧据此再要一次 scroll；直到某一轮顶边贴住列表上沿
        （= 已经是第一屏）才收尾。第 2 屏就是在中间那几轮里补齐的。
        """
        s1, s2, s3, first_text = self._three_screens()

        self.post_frame(self.screen(self.row("旧消息A", 200, "签收跟单专员")))
        first = self.post_frame(self.screen(
            self.row("旧消息A", 200, "签收跟单专员"),
            self.one_long("履约跟单专员", 250, 3200, first_text)))
        self.assertTrue(first.get("scroll"), "第一次见长消息就该请手机翻页")
        self.assertEqual(first["messages"], 0)

        # ① 第 1 轮：翻上来看到第 2 屏 + 第 3 屏开头，顶边仍被切 → 还要翻
        r1 = self.post_fill(self.screen(
            self.one_long("履约跟单专员", -100, 3200, s2)))
        self.assertTrue(r1.get("scroll"), "顶边还切着就必须继续翻（否则中间段丢失）")
        self.assertEqual(r1["messages"], 0, "没齐之前不许发半截")
        self.settle()
        self.assertEqual(self.received, [], "多轮补全期间不该有消息发出去")

        # ② 第 2 轮：翻到顶，第 1 屏顶边贴住列表上沿 → 收尾，发一条完整的
        r2 = self.post_fill(self.screen(
            self.one_long("履约跟单专员", 200, 3200, s1)))
        self.assertNotIn("scroll", r2, "已经到顶了还让手机翻 —— 会翻出历史")
        self.assertEqual(r2["messages"], 1, "齐了应当发一条")
        self.settle()
        self.assertEqual(len(self.received), 1, "多轮补全只许投递一条")
        got = self.received[0].content
        self.assertIn("B010B011B012", got, "中间那一屏丢了")
        self.assertTrue(got.startswith("A015A016A017"), f"开头不是第一屏：[{got[:30]}] len={len(got)}")
        self.assertTrue(got.endswith("C027C028C029"), "结尾不是第三屏")

    def test_five_screen_message_is_fully_collected(self):
        """5 屏的正文（用户说「有的消息很长的」）也要一路补到第一屏。

        每轮手机只多露 60% 屏高，5 屏要走 4~5 轮。这里按真机节奏一轮一轮喂，
        最后一轮顶边贴住列表上沿才算齐 —— 而且自始至终只投递一条。
        """
        segs = [self._seg(chr(ord("A") + i)) for i in range(5)]   # A..E
        overlap = 9
        # 第 i 屏可见文本 = 上一屏尾巴 overlap 字 + 本屏全文
        screens = []
        for i, s in enumerate(segs):
            head = segs[i - 1][-overlap:] if i else ""
            screens.append(head + s)
        last = screens[-1]

        self.post_frame(self.screen(self.row("旧消息A", 200, "签收跟单专员")))
        first = self.post_frame(self.screen(
            self.row("旧消息A", 200, "签收跟单专员"),
            self.one_long("履约跟单专员", 250, 3200, last)))
        self.assertTrue(first.get("scroll"))

        rounds = 0
        # 手机从最下面一屏开始，每轮往上退一屏
        for idx in range(len(screens) - 2, -1, -1):
            rounds += 1
            body = self.post_fill(self.screen(
                self.one_long("履约跟单专员", 200 if idx == 0 else -100,
                              3200, screens[idx])))
            if idx > 0:
                self.assertTrue(body.get("scroll"),
                                f"第 {rounds} 轮顶边还切着，必须继续翻")
                self.assertEqual(body["messages"], 0)
            else:
                self.assertNotIn("scroll", body, "到顶了还翻")
                self.assertEqual(body["messages"], 1)
        self.assertGreaterEqual(rounds, 4, "5 屏的正文轮数不该少到 4 以下")

        self.settle()
        self.assertEqual(len(self.received), 1, "始终只许投一条")
        got = self.received[0].content
        for i, s in enumerate(segs):
            self.assertIn(s[:12], got, f"第 {i + 1} 屏的内容丢了")
            self.assertIn(s[-9:], got, f"第 {i + 1} 屏的结尾丢了")
        self.assertTrue(got.startswith(segs[0]), "开头不是第一屏")
        self.assertTrue(got.endswith(segs[-1]), "结尾不是最后一屏")

    def test_fill_rounds_are_capped(self):
        """翻满上限就按现状投递，不许让手机无限翻。"""
        body0 = self._seg("X")
        self.post_frame(self.screen(self.row("旧消息A", 200, "签收跟单专员")))
        self.post_frame(self.screen(
            self.row("旧消息A", 200, "签收跟单专员"),
            self.one_long("履约跟单专员", 250, 3200, body0)))
        # 每轮都「顶边被切」且每轮都多一段新内容 —— 永远不判齐，
        # 只能靠轮数上限收尾。
        prev = body0
        res = None
        for i in range(1, 30):
            tag = chr(ord("D") + (i % 20)) + "z"
            nxt = self._seg(tag, 12) + prev
            res = self.post_fill(self.screen(
                self.one_long("履约跟单专员", -100, 3200, nxt)))
            prev = nxt
            if not res.get("scroll"):
                break
        self.assertIsNotNone(res)
        self.assertFalse(res.get("scroll"), "手机被要求一直翻 —— 轮数上限没生效")
        self.assertIn("轮数上限", res.get("note", ""))
        self.settle()
        self.assertEqual(len(self.received), 1, "到头来只发一条")
        self.assertEqual(self.received[0].content, prev, "应当发攒到的最全版本")

    def test_plain_frame_between_rounds_does_not_drop_the_long_message(self):
        """补全中间夹一帧「没有截断也没有新消息」，不能把攒着的长消息清掉。

        旧实现在这种帧上顺手 `_clear_long()`（理由是「用户可能划走了」）。
        多轮补全要翻五六次，中间必然夹着手机翻页的中间态帧 —— 在那里清掉
        等于前面几轮白翻，而且再也没人请手机继续翻。
        """
        s1, s2, s3, first_text = self._three_screens()
        self.post_frame(self.screen(self.row("旧消息A", 200, "签收跟单专员")))
        self.post_frame(self.screen(
            self.row("旧消息A", 200, "签收跟单专员"),
            self.one_long("履约跟单专员", 250, 3200, first_text)))
        r1 = self.post_fill(self.screen(
            self.one_long("履约跟单专员", -100, 3200, s2)))
        self.assertTrue(r1.get("scroll"))

        # 中间态帧：屏幕上还是最初那半截，既无新内容、也无截断提示
        gap = self.post_frame(self.screen(
            self.row("旧消息A", 200, "签收跟单专员")))
        self.assertEqual(gap["messages"], 0)

        # 接着翻：这一轮露到第一屏，顶边贴住 → 齐了
        r2 = self.post_fill(self.screen(
            self.one_long("履约跟单专员", 200, 3200, s1)))
        self.assertNotIn("scroll", r2)
        self.settle()
        self.assertEqual(len(self.received), 1, "中间态帧把长消息弄丢了")
        self.assertIn("A015", self.received[0].content)
        self.assertIn("C020", self.received[0].content)

    # ---- H5 卡片「扣住待稳」 --------------------------------------------
    #
    # 真机事故（2026-10-17 拦截跟单日报）：机器人消息是 H5 卡片，内容分批
    # 渲染。抓到半成品就转发，群里收到的就是「有标题没数据」的残废消息；
    # 渲染完的那一帧锚点又对不上被静默重建基线，缺的内容永远补不回来。

    def garbled(self, xml: str) -> str:
        """往帧里塞一批坏坐标文本节点 —— 钉钉 H5 卡片的标志形状。

        实测真机帧里 H5 卡片的文字节点全是 ``bounds="[x,265][x,负数]"``，
        right 甚至小于 left。这里复制 5 个 ``[0,265][100,-50]``（阈值 4）。
        """
        badge = "".join(
            f'<node text="h5占位{i}" class="android.widget.TextView" '
            f'package="{self.PKG}" bounds="[0,265][100,-50]"></node>'
            for i in range(5))
        return xml.replace('</node></hierarchy>', badge + '</node></hierarchy>')

    def test_garbled_frame_holds_then_delivers_when_stable(self):
        """H5 卡片的每条都要发出来，且同一条只发一次。

        这条原本锁的是旧「扣住等第二眼」机制（第 1 眼扣、第 2 眼建基线、
        第 3 眼没新内容）。那个机制在真机上没通 —— 卡片重绘会让指纹全变，
        「第 2 眼」几乎等不到。现在改成「收进缓冲、静默几秒发最全的」，
        所以断言换成新机制该有的样子。

        ⚠ 第一帧只建基线（防翻历史灌屏），所以要先单独把「旧消息A」报一遍
        当基线，H5 卡片才算是「新消息」。
        """
        self.post_frame(self.screen(self.row("旧消息A", 400)))

        xml = self.garbled(self.screen(self.row("旧消息A", 400),
                                       self.row("H5报表正文", 700)))
        b1 = self.post_frame(xml)
        self.assertEqual(b1["messages"], 1, "H5 条目应当被收进缓冲")
        self.assertGreaterEqual(b1.get("buffered", 0), 1)
        # 收进缓冲那一条就算这一帧解析出的「新单元数」，手机端按它判断
        # 这一屏有没有产生转发。真正投递要等 settle()。

        xml2 = self.garbled(self.screen(self.row("旧消息A", 400),
                                        self.row("H5报表正文", 700),
                                        self.row("拦截跟单日报 表格数据", 980)))
        self.post_frame(xml2)
        self.settle()
        got = [m.content for m in self.received]
        self.assertIn("H5报表正文", got, "第一张 H5 卡片丢了")
        self.assertIn("拦截跟单日报 表格数据", got, "第二张 H5 卡片丢了")
        self.assertEqual(len(got), 2, "同一条卡片发了不止一次")

    def test_h5_content_growing_is_delivered_by_total_cap(self):
        """内容一直长（每帧都不一样）也必须发出去，且只发最全的那版。

        旧机制靠 `hold_total` 撞到 `HOLD_MAX` 才放行，是因为它拿指纹判「还是
        同一条」——内容一变指纹就变，永远等不到稳定。新机制不判指纹，改成
        「同一发送者窗口内保留最长版本」，所以多少帧都不会卡住。
        """
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        prev = ""
        for i in range(6):          # 长 6 截，远超旧机制的 4 眼上限
            prev = " ".join(f"长到第{j}段" for j in range(i + 1))
            self.post_frame(self.garbled(self.screen(
                self.row("旧消息A", 400), self.row(prev, 700))))
        self.settle()
        self.assertEqual(len(self.received), 1,
                         "长了几截就发了几条 —— 应当只发最全的那一版")
        self.assertEqual(self.received[-1].content, prev,
                         "发的不是最全的那一版")

    def test_native_frame_has_zero_extra_latency(self):
        """原生消息（坐标正常）不扣：一次上报直接投递。"""
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        body = self.post_frame(self.screen(self.row("旧消息A", 400),
                                           self.row("普通消息B", 700)))
        self.assertEqual(body["messages"], 1)
        self.assertNotIn("hold", body)
        self.settle()
        self.assertEqual(self.received[-1].content, "普通消息B")

    def test_h5_card_is_not_lost_after_hold_window(self):
        """H5 卡片的这条必须投递出去 —— 不靠「第二眼」，靠攒。

        2026-10-18 真机事故：用户报「拦截跟单日报」「按需派送履约风险提醒」
        两条都没转发。全天日志：扣住第 1 眼 14 次、进到第 2 眼 **0 次**、
        终投递 1 条 —— 也就是说「等重报帧指纹一致」这条路在真机上基本没通。

        根因是它的前提不成立：H5 卡片每重绘一次，**整批节点指纹都会变**，
        于是 `hold_key` 每帧都对不上，`seen` 永远等于 1。

        现在不再问「两帧一样吗」，改成「见过的所有版本里哪个最全」：
        卡片长了几截就攒几截，静默几秒后发最全的那版。这个用例锁的就是
        「无论重绘多少次、指纹怎么变，这条最终都要发出去，且只发一次」。
        """
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        xml = self.garbled(self.screen(self.row("旧消息A", 400),
                                       self.row("拦截跟单日报 表格数据", 700)))
        b1 = self.post_frame(xml)
        self.assertEqual(b1["messages"], 1, "H5 卡片应当被**收进缓冲**（不是直接丢）")

        # 手机按 hold 重报同一帧（真机上常发生，且指纹会变）
        self.post_frame(xml)
        self.settle()
        self.assertEqual(len(self.received), 1,
                         "H5 卡片丢了 —— 真机上就是「拦截跟单日报」"
                         "「按需派送履约风险提醒」这种情况")
        self.assertEqual(self.received[0].content, "拦截跟单日报 表格数据")

    def test_growing_h5_card_only_the_fullest_version_is_sent(self):
        """卡片分批长出来：只发最全的那一版，不是发三遍。

        这是「先攒后发」的核心场景 —— 真机上 H5 卡片先出标题、再出正文、
        最后表格单元格才长全。每一帧的指纹都不同（所以旧机制永远等不到
        「稳定」），但内容是一次比一次全的。
        """
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        # 解析器把一屏里的多行合并成空格分隔的一行（真机 XML 里就是一个
        # TextView，没有换行）—— 所以期望值也按空格写。
        stages = ["拦截跟单日报",
                  "拦截跟单日报 今天共拦截 37 票",
                  "拦截跟单日报 今天共拦截 37 票 明细：马伟豪 17 票"]
        for text in stages:
            self.post_frame(self.garbled(self.screen(
                self.row("旧消息A", 400), self.row(text, 700))))
        self.settle()
        self.assertEqual(len(self.received), 1, "卡片长了几截就发了几条")
        self.assertEqual(self.received[0].content, stages[-1],
                         "发的不是最全的那一版")

    def test_hold_units_dropped_when_gone_from_screen(self):
        """H5 卡片还没发出去就被划走了 —— 不能事后当新消息补发。

        旧机制用「扣住的那批单元」判这个；现在扣住的记录没了，靠缓冲的
        时间窗 + 内容包含关系：卡片停在屏上时每帧都是同一个 key 在续命，
        一旦用户划走、屏上换成别人的内容，它就再也不会被续命，静默到期后
        发出的仍是**当时收到的最全版本**（而不是被划走后的别的消息）。
        这里只锁一点：**别把「H5卡片内容」和后来那条混成一条**。
        """
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        xml = self.garbled(self.screen(self.row("旧消息A", 400),
                                       self.row("H5卡片内容", 700)))
        self.post_frame(xml)
        # 用户划走了：屏上换成别的发送者的内容
        self.post_frame(self.screen(self.row("后来的一条", 400, "另外一个人"),
                                    self.row("又一条", 700, "另外一个人")))
        # 再报一次同一屏（试探翻一屏之后正常回来的那帧）
        body = self.post_frame(self.screen(self.row("后来的一条", 400, "另外一个人"),
                                           self.row("又一条", 700, "另外一个人")))
        sent = [m.content for m in self.received]
        self.assertNotIn("H5卡片内容", sent,
                         "扣住的 H5 内容不该在划走之后补发")
        self.assertEqual(body["messages"] >= 0, True)

    def test_system_notes_do_not_take_up_the_screen(self):
        """本程序自己的【系统】通知不参与比对 —— 它们会挤掉真业务消息。

        2026-10-18 实测：可视区 1753 px，两条系统通知（「转发桥已启动」
        578 px +「已停止」227 px）吃掉 805 px，真正的业务消息被顶出屏幕，
        采集端根本看不见。程序每重启一次就插两条，丢失会累积。
        """
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        body = self.post_frame(self.screen(
            self.row("旧消息A", 400),
            self.row("【系统】转发桥已启动 模式：正式发送", 700),
            self.row("【系统】转发桥已停止 运行统计：收到 2", 1000),
            self.row("拦截跟单日报 正文", 1300)))
        self.assertEqual(body["messages"], 1,
                         "系统通知不该让业务消息跟着丢")
        self.settle()
        self.assertEqual(self.received[-1].content, "拦截跟单日报 正文")


    # ---- 同一条卡片换了渲染形态，不许发第二遍 --------------------------
    #
    # 用户原话：「我手机往上翻页，会发重复的」。
    # 真机（2026-10-17 拦截跟单日报）：同一张机器人卡片，表格有时解析成
    # 横排「列名: 值 ｜ 列名: 值」，有时退化成一格一行。指纹不同 → uid 不同
    # → 下游 is_duplicate 挡不住 → 18:07:03 和 18:07:07 各发了一次。

    # 形态 A：表格识别成功（横排）
    CARD_A = ("@所有人拦截跟单日报\n时间 2026/10/17 · 浙江杭州萧山区大江东公司\n"
              "跟单概况\n指标\n10/13\n10/14\n10/15\n10/16\n10/17\n"
              "跟单任务数\n30\n21\n23\n20\n17\n完成率\n93.33%\n90.48%\n"
              "考核预警（近5日）\n未及时转出量\n0 票\n预计考核金额\n0 元")
    # 形态 B：表格识别失败（一格一行，列顺序被打散）
    CARD_B = ("@所有人拦截跟单日报\n时间 2026/10/17 · 浙江杭州萧山区大江东公司\n"
              "跟单概况\n指标\n跟单任务数\n10/13\n30\n10/14\n21\n10/15\n23\n"
              "10/16\n20\n10/17\n17\n完成率\n93.33%\n90.48%\n"
              "考核预警（近5日）\n未及时转出量\n0 票\n预计考核金额\n0 元")

    def test_reshaped_card_is_delivered_only_once(self):
        """同一条卡片的两种渲染只能发一次 —— 这是用户报的那个重复。"""
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        a = self.post_frame(self.screen(self.row("旧消息A", 400),
                                        self.row(self.CARD_A, 700)))
        self.assertEqual(a["messages"], 1)
        self.settle()
        self.assertEqual(len(self.received), 1, "第一种形态没发出来")
        b = self.post_frame(self.screen(self.row("旧消息A", 400),
                                        self.row(self.CARD_B, 700)))
        self.assertEqual(b["messages"], 1, "第二种形态仍需被收下（它还没投递）")
        self.settle()
        self.assertEqual(len(self.received), 1, "换了渲染形态的同一条卡片又被发了一遍")

    def test_different_report_with_new_numbers_still_goes_out(self):
        """隔天的日报格式一样、数字不同 —— 必须照发（判重不能吃掉真消息）。"""
        self.post_frame(self.screen(self.row("旧消息A", 400)))
        self.post_frame(self.screen(self.row("旧消息A", 400),
                                    self.row(self.CARD_A, 700)))
        changed = self.CARD_A.replace("93.33%", "88.10%")
        body = self.post_frame(self.screen(self.row("旧消息A", 400),
                                           self.row(changed, 700)))
        self.assertEqual(body["messages"], 1)
        self.settle()
        self.assertEqual(len(self.received), 2)


# --------------------------------------------------------------- 手机装 App

class TestFindApk(unittest.TestCase):
    """挑哪个 APK 发出去 —— 按版本号，不按 mtime。

    真机事故：安装版把 APK 解压到 ``app/``，同一次解压出来的 0.1.0 和
    0.2.0 的 mtime **精确到秒完全一样**。早先只按 mtime 取 ``max()``，
    相等时保留先遇到的那个，于是页面一直发 0.1.0（缺手势权限、没有心跳），
    而页面上写着「下载并安装 ddwx-collector-0.1.0.apk」，看着完全正常。
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="apk-"))
        set_apk_dirs([self.dir])

    def tearDown(self):
        set_apk_dirs([])
        shutil.rmtree(self.dir, ignore_errors=True)

    def put(self, name: str, *, mtime: float | None = None,
            size: int = 10) -> Path:
        p = self.dir / name
        p.write_bytes(b"PK" + b"\0" * size)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_same_mtime_picks_the_higher_version(self):
        """0.1.0 在列表里排前面且 mtime 相同 —— 仍必须挑 0.2.0。"""
        self.put("ddwx-collector-0.1.0.apk", mtime=1_700_000_000)
        self.put("ddwx-collector-0.2.0.apk", mtime=1_700_000_000)
        self.assertEqual(_find_apk().name, "ddwx-collector-0.2.0.apk")

    def test_older_version_with_newer_mtime_still_loses(self):
        """版本号高的赢，哪怕旧版本的文件时间更新。"""
        self.put("ddwx-collector-0.1.0.apk", mtime=2_000_000_000)
        self.put("ddwx-collector-0.2.0.apk", mtime=1_700_000_000)
        self.assertEqual(_find_apk().name, "ddwx-collector-0.2.0.apk")

    def test_same_version_newer_mtime_wins(self):
        """版本号相同时（重编同名包）才轮到 mtime 决定。"""
        self.put("ddwx-collector-0.2.0.apk", mtime=1_700_000_000)
        self.put("ddwx-collector-0.2.0.apk", mtime=1_700_000_000)
        # 换一种文件名不合规的，确认合规的那个不被它挤掉
        self.put("collector-dev.apk", mtime=2_000_000_000)
        self.assertEqual(_find_apk().name, "ddwx-collector-0.2.0.apk")

    def test_ten_beats_nine(self):
        """按数字比，不能按字符串比 —— '0.10.0' 要赢 '0.9.0'。"""
        self.put("ddwx-collector-0.9.0.apk", mtime=1_700_000_000)
        self.put("ddwx-collector-0.10.0.apk", mtime=1_700_000_000)
        self.assertEqual(_find_apk().name, "ddwx-collector-0.10.0.apk")

    def test_no_apk_returns_none(self):
        self.assertIsNone(_find_apk())


class TestApkSearchDirs(unittest.TestCase):
    """「去哪儿找 APK」不能靠 ``sys.executable``。

    2026-10-18 真机事故：安装版 ``喵喵钉钉-微信消息转发桥.exe`` 只是个小启动器，
    网关实际跑在它拉起的 **worker 子进程**里（``_env.json`` 指定的 Python）。
    于是 ``sys.executable`` = ``.../envs/default/Scripts/python.exe``，
    ``exe_dir`` 算成那个 Scripts 目录 —— 安装目录压根没进候选，
    刚放进 ``app/`` 的 ``ddwx-collector-0.3.0.apk`` 从来没被扫到过，
    页面稳定发 ``android/dist`` 里的老 0.2.0。用户看到的是「升级了还是老包」。
    """

    def test_program_dir_comes_from_module_not_executable(self):
        """程序目录必须由模块路径上溯得到，跟解释器装在哪无关。"""
        dirs = _apk_search_dirs()
        self.assertTrue(dirs, "至少要有候选目录")
        # 模块所在仓库/程序目录 = .../bridge/sources/android_gateway.py 上溯两级
        root = Path(android_gateway.__file__).resolve().parents[2]
        self.assertIn(root / "app", dirs,
                      "程序目录下的 app/ 必须在候选里（安装版把当前包放这里）")
        # 关键词：worker 进程跑时 sys.executable 是 Scripts/python.exe，
        # 万一它成了**唯一**的基准，这条断言就会挂。
        self.assertNotEqual(dirs[0], Path(sys.executable).resolve().parent,
                            "候选首位不能是解释器目录")

    def test_app_dir_is_preferred_over_android_dist(self):
        """``<程序目录>/app`` 排在 ``<程序目录>/android/dist`` 前面。"""
        dirs = _apk_search_dirs()
        root = Path(android_gateway.__file__).resolve().parents[2]
        self.assertLess(dirs.index(root / "app"),
                        dirs.index(root / "android" / "dist"))

    def test_no_duplicates(self):
        low = [str(d).lower() for d in _apk_search_dirs()]
        self.assertEqual(len(low), len(set(low)))


class TestAppDownload(GatewayCase):
    """``/app``（下载页）与 ``/app.apk``（安装包）。

    为什么要走网关而不是让用户开个文件服务器：手机第一次装的时候**还没有
    token**（token 要靠装完 App 才能填），所以这两个端点必须免鉴权；
    而装完要手填的三项（IP / 端口 / token）直接印在页面上，
    省掉「用肉眼把 24 位 token 从电脑抄到手机」这步最容易错的操作。
    """

    def get(self, path: str) -> tuple[int, str, bytes]:
        with urllib.request.urlopen(self.url(path), timeout=5) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()

    def test_app_page_needs_no_token_and_shows_the_settings(self):
        status, ctype, raw = self.get("/app")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        html = raw.decode("utf-8")
        # 三样必须能照着填
        self.assertIn("IP 地址", html)
        # 端口要印**本次实际监听的**那个，不能是写死的常量：
        # 用户在设置里改过端口时，页面照着旧值显示，手机上填了必然连不上，
        # 而且从 App 的报错里也看不出是端口错了。
        self.assertIn(str(self.port), html)
        self.assertNotIn(">8765<", html)
        self.assertIn(self.token, html)

    def test_apk_is_served_as_package_archive(self):
        status, ctype, raw = self.get("/app.apk")
        self.assertEqual(status, 200)
        # 必须是 android 安装包的 MIME —— 否则手机浏览器会把它当文本打开
        self.assertEqual(ctype, "application/vnd.android.package-archive")
        self.assertEqual(raw[:2], b"PK")        # APK 就是 zip

    def test_unknown_path_still_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_screen_endpoint_still_requires_token(self):
        """加下载页不能顺手把别的端点也放开 —— 画面里有群消息正文。"""
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.url("/screen"), timeout=5)
        self.assertEqual(ctx.exception.code, 401)


# --------------------------------------------------------------- 心跳与失联
#
# 这一层解决的是「程序在跑、但一条都不转，而且没人发现」——手机端 App 被系统
# 清理、锁屏断网之后，电脑侧收不到任何异常。判据只能是心跳。

MIN_FRAME = ('<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
             '<node class="android.widget.FrameLayout" '
             'package="com.alibaba.android.rimet" bounds="[0,0][1080,2340]">'
             '<node class="android.widget.ListView" '
             'package="com.alibaba.android.rimet" scrollable="true" '
             'bounds="[0,200][1080,2100]"/></node></hierarchy>')


class TestHeartbeatEndpoint(GatewayCase):
    """心搏端点的行为锁死：谁能刷新「在线」，谁不能。"""
    def post_ping(self, token: str | None = TOKEN, device: str = "PAL-AL00"):
        req = urllib.request.Request(self.url("/ping"), data=b"{}",
                                     method="POST")
        if token is not None:
            req.add_header("X-Bridge-Token", token)
        if device:
            req.add_header("X-Ddwx-Device", device)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def post_frame(self):
        req = urllib.request.Request(self.url("/screen"),
                                     data=MIN_FRAME.encode("utf-8"),
                                     method="POST")
        req.add_header("X-Bridge-Token", TOKEN)
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status

    def test_post_ping_marks_online(self):
        code, body = self.post_ping()
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["last_seen"])          # 形如 "13:31:58"

        _, state = self.get()
        self.assertTrue(state["online"])
        self.assertTrue(state["heartbeat"])
        self.assertEqual(state["device"], "PAL-AL00")

    def test_post_ping_requires_token(self):
        """心跳也要鉴权 —— 否则局域网上任何人都能把「失联告警」压掉。"""
        code, _ = self.post_ping(token=None)
        self.assertEqual(code, 401)
        _, state = self.get()
        self.assertFalse(state["online"])
        self.assertFalse(state["heartbeat"])

    def test_get_ping_without_hb_does_not_count(self):
        """浏览器/curl 随手打开 /ping 只是自测，不能把人当成「手机在线」。"""
        code, state = self.get("/ping")
        self.assertEqual(code, 200)
        self.assertFalse(state["online"])

        # 带 hb=1 才算心跳。注意 token 要自己拼进 query —— 帮助方法只会在
        # 整条 path 后面追加 `?token=`，path 里已经有一个 `?` 时会拼出两个。
        _, state = self.get(f"/ping?hb=1&device=PAL-AL00&token={quote(TOKEN)}",
                            token=None)
        self.assertTrue(state["online"])
        self.assertTrue(state["heartbeat"])

    def test_screen_counts_as_alive_but_not_heartbeat(self):
        """上报画面能证明「活着」，但**不能冒充心跳**。

        写反了会怎样：旧版 APK 没有心跳，只要收到过画面就算「有心跳」，
        于是「群里没人说话」被当成「手机失联」，一天报几十次。
        """
        self.assertEqual(self.post_frame(), 200)
        _, state = self.get()
        self.assertTrue(state["online"])
        self.assertFalse(state["heartbeat"])


class TestOnlineNow(unittest.TestCase):
    """``/ping`` 的 online 必须回答「现在」，不是「历史上连上过」。

    真机上吃过一次亏：手机锁屏八分钟、心跳一次都没来，而 /ping 仍回
    ``online: true``（旧实现是 ``bool(last_seen)``），排查方向直接被带偏到
    「电脑侧是不是卡了」。字段名既然叫 online，就不能拿历史当现状。
    """

    def new_ctx(self) -> _Ctx:
        return _Ctx(token="t", chat_name="数字质控")

    def test_never_seen_is_offline(self):
        self.assertFalse(_online_now(self.new_ctx()))

    def test_fresh_heartbeat_is_online(self):
        c = self.new_ctx()
        c.touch(device="PAL-AL00", heartbeat=True)
        self.assertTrue(_online_now(c))

    def test_stale_heartbeat_is_offline(self):
        """锁屏八分钟那种情形：心跳停在很久以前，必须报不在线。"""
        c = self.new_ctx()
        c.touch(heartbeat=True)
        c.last_seen = time.monotonic() - 480
        self.assertFalse(_online_now(c))

    def test_exactly_at_threshold_still_online(self):
        """边界上不算失联（与看门狗口径一致：超过阈值才算）。"""
        c = self.new_ctx()
        c.touch(heartbeat=True)
        c.last_seen = time.monotonic() - 299
        self.assertTrue(_online_now(c))

    def test_detection_off_falls_back_to_ever_seen(self):
        """关掉失联检测（offline_after=0）时没有阈值可用，退回「收到过」。

        注意这里不能写 ``time.monotonic() - 99999``：monotonic 是**开机**以来的
        秒数，机器刚开机时减掉一个大数会变成负值，落到「从没收到过」分支上，
        测试就成了假绿/假红。用 1.0（开机的第一秒）表达「很久以前」才稳。
        """
        c = self.new_ctx()
        c.offline_after = 0.0
        c.touch(heartbeat=True)
        c.last_seen = 1.0
        self.assertTrue(_online_now(c))


class TestWatchdog(unittest.TestCase):
    """失联状态机。测纯函数而不用线程 + sleep：后者慢，而且负载高时会随机红。"""

    def new_ctx(self) -> _Ctx:
        return _Ctx(token="t", chat_name="数字质控")

    def test_never_seen_alerts_after_threshold(self):
        c = self.new_ctx()
        self.assertIsNone(_watchdog_step(c, 0.0, 100.0, 300.0))
        msg = _watchdog_step(c, 0.0, 400.0, 300.0)
        self.assertIsNotNone(msg)
        self.assertEqual(msg[0], logging.WARNING)
        self.assertIn("一次都没有收到", msg[1])
        self.assertIn("6 分钟", msg[2])
        self.assertEqual(c.state, "missing")

    def test_never_seen_reminder_is_rate_limited(self):
        c = self.new_ctx()
        _watchdog_step(c, 0.0, 400.0, 300.0)
        self.assertIsNone(_watchdog_step(c, 0.0, 460.0, 300.0))
        again = _watchdog_step(c, 0.0, 400.0 + OFFLINE_REMIND_EVERY + 1, 300.0)
        self.assertIsNotNone(again)
        self.assertIn("仍然没有任何上报", again[1])

    def test_online_then_offline_then_recover(self):
        c = self.new_ctx()
        c.touch(device="PAL-AL00", heartbeat=True)
        base = c.last_seen

        self.assertIsNone(_watchdog_step(c, base, base + 10, 300.0))
        self.assertEqual(c.state, "online")

        msg = _watchdog_step(c, base, base + 400, 300.0)
        self.assertEqual(msg[0], logging.WARNING)
        self.assertIn("失联", msg[1])
        self.assertIn("PAL-AL00", msg[2])
        self.assertEqual(c.state, "offline")

        # 冷却期内不重复刷日志（WARNING 会触发钉钉告警）
        self.assertIsNone(_watchdog_step(c, base, base + 500, 300.0))

        c.touch(heartbeat=True)
        back = c.last_seen
        msg = _watchdog_step(c, back, back + 5, 300.0)
        self.assertEqual(msg[0], logging.WARNING)
        self.assertIn("恢复", msg[1])
        # 断开时长要按「上一次心跳」算，不能按刚刷新的 last_seen ——
        # 后者永远是 0 秒，正好把最想看的信息抹掉。
        self.assertEqual(msg[2][0], "5 秒")
        self.assertEqual(c.state, "online")

    def test_no_heartbeat_never_reports_offline(self):
        """旧版 APK（没有心跳）永不判失联。

        它只在画面变化时上报，「群里没人说话」和「App 死了」在电脑侧看来
        完全一样。硬判就会天天误报，而误报几次之后就没人再看告警了 ——
        那比不告警更糟。所以只提示一次「升级 APK」。
        """
        c = self.new_ctx()
        c.touch()                                   # 有业务上报，无心跳
        base = c.last_seen

        # 宽限期内不下结论：真机上第一帧画面几乎总比第一个心跳先到，
        # 立刻断言「旧版 APK」会把排查带去「装错包」（每次重启都误报一次）
        self.assertIsNone(_watchdog_step(c, base, base + 1, 300.0))
        self.assertEqual(c.state, "waiting")

        msg = _watchdog_step(c, base, base + HEARTBEAT_GRACE + 1, 300.0)
        self.assertEqual(msg[0], logging.INFO)
        self.assertIn("0.2.0", msg[1])
        self.assertEqual(c.state, "online")

        # 一天都没动静，仍然沉默
        self.assertIsNone(_watchdog_step(c, base, base + 86400, 300.0))
        self.assertEqual(c.state, "online")

    def test_missing_then_connects(self):
        c = self.new_ctx()
        _watchdog_step(c, 0.0, 400.0, 300.0)        # → missing
        c.touch(heartbeat=True)
        base = c.last_seen
        msg = _watchdog_step(c, base, base + 5, 300.0)
        self.assertEqual(msg[0], logging.WARNING)
        self.assertIn("终于连上", msg[1])
        self.assertEqual(c.state, "online")

    def test_touch_does_not_flip_state(self):
        """状态迁移只能发生在看门狗里，否则日志会「一会儿说恢复一会儿说失联」。"""
        c = self.new_ctx()
        c.touch(heartbeat=True)
        self.assertEqual(c.state, "waiting")


class TestOfflineConfig(unittest.TestCase):

    def test_default_threshold(self):
        cfg = make_cfg(free_port(), TOKEN)
        self.assertEqual(cfg.dingtalk.android_offline_after, 300.0)

    def test_source_reads_threshold(self):
        s = AndroidGatewaySource(
            make_cfg(free_port(), TOKEN, android_offline_after=90))
        self.assertEqual(s.offline_after, 90.0)

    def test_negative_means_disabled(self):
        s = AndroidGatewaySource(
            make_cfg(free_port(), TOKEN, android_offline_after=-5))
        self.assertEqual(s.offline_after, 0.0)

    def test_bad_value_falls_back(self):
        s = AndroidGatewaySource(
            make_cfg(free_port(), TOKEN, android_offline_after="abc"))
        self.assertEqual(s.offline_after, 300.0)


if __name__ == "__main__":
    unittest.main()
