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
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from bridge.config import Config, DingTalkConfig, RouteConfig, StorageConfig, WeChatConfig
from bridge.fingerprint import fp_of, norm_sender, unit_fp
from bridge.service import BridgeService
from bridge.sources.android_gateway import (
    MAX_BATCH, MAX_SCREEN_BODY, AndroidGatewaySource, build_messages, lan_ips,
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
        self.assertEqual([m.content for m in self.received], ["第二条"])

    def test_scrolling_back_does_not_flood(self):
        self.post_frame(self.screen(self.row("第五条", 400)))
        body = self.post_frame(self.screen(
            self.row("第一条", 300), self.row("第二条", 500), self.row("第三条", 700)))
        self.assertEqual(body["messages"], 0)
        self.assertEqual(body["note"], "锚点丢失")
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


if __name__ == "__main__":
    unittest.main()
