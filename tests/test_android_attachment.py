"""附件转发链路的电脑侧回归测试。

链路：卡片正文出现「附件：xxx」→ 网关下发 fetch_attach 指令（带冷却）→
手机 POST /file 上传 → 网关落盘去重 → service.handle_attachment 渲染
表格图投递（media_path 路径）。
"""

import base64
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from bridge.service import BridgeService
from bridge.sources.android_gateway import (AndroidGatewaySource, _attach_name,
                                            _clean_attach_keys,
                                            _Ctx)
from bridge.table_image import CELL_SEP
from tests.test_android_gateway import GatewayCase, make_cfg, free_port

TOKEN = GatewayCase.token


def _mini_xlsx(rows: list[list[str]]) -> bytes:
    """造一个 inlineStr 的 xlsx（真机 export.xlsx 同款存储）。"""
    buf = io.BytesIO()
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(buf, "w") as z:
        body = "".join(
            '<row r="%d">%s</row>' % (
                i + 1,
                "".join('<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>'
                        % (chr(65 + j), i + 1, v)
                        for j, v in enumerate(row)))
            for i, row in enumerate(rows))
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<?xml version="1.0"?><worksheet xmlns="{ns}">'
                   f"<sheetData>{body}</sheetData></worksheet>")
    return buf.getvalue()


class TestAttachName(unittest.TestCase):

    def test_plain_line(self):
        # 真机当日签卡片的形态：不带扩展名
        self.assertEqual(_attach_name("当前情况：\n附件：运单明细", None), "运单明细")

    def test_with_extension(self):
        self.assertEqual(_attach_name("明细附件：拦截风险明细.xlsx", None),
                         "拦截风险明细.xlsx")

    def test_no_match(self):
        self.assertEqual(_attach_name("附件不存在这行", None), "")
        self.assertEqual(_attach_name("", None), "")


class TestAttachKeys(unittest.TestCase):
    """触发字眼可配置（2026-10-27 用户要求）：列表、逗号串都认；坏配置回落默认。"""

    def test_default_keys(self):
        self.assertEqual(_clean_attach_keys(None), ["附件：", "附件:"])
        self.assertEqual(_clean_attach_keys([]), ["附件：", "附件:"])

    def test_string_form(self):
        # ensure_config_keys 只补标量，所以配置文件里是逗号分隔的字符串
        self.assertEqual(_clean_attach_keys("附件：,附件:"),
                         ["附件：", "附件:"])
        self.assertEqual(_clean_attach_keys("明细单：，风险件："),
                         ["明细单：", "风险件："])

    def test_list_form_dedup(self):
        self.assertEqual(_clean_attach_keys(["附件：", " 附件：", "明细附件："]),
                         ["附件：", "明细附件："])

    def test_custom_key_matches(self):
        self.assertEqual(
            _attach_name("本批次：\n明细单：沪苏-1024", ["明细单："]), "沪苏-1024")
        # 自定义字眼后，默认字眼不再触发
        self.assertEqual(_attach_name("附件：运单明细", ["明细单："]), "")

    def test_bad_input_falls_back(self):
        self.assertEqual(_clean_attach_keys(",,，"), ["附件：", "附件:"])


class TestButtonStyleAttachLine(unittest.TestCase):
    """真机 2026-10-27 19:52：按钮行「查看完整月报明细（Excel）」被抠成「（Excel）」。

    那张卡片的按钮文案里只有格式标记、没有文件名。抠成「（Excel）」的后果：
    日志看不出是哪个附件；手机端拿这个名字去找节点，只能靠 fallback 点
    「随便哪一行附件」（点错也不会报错）。整行当名字才对 —— 它就是屏上
    可点的原文，手机端 contains 必中。
    """

    KEYS = ["附件：", "附件:", "明细附件", "查看完整月报明细"]

    def test_format_only_suffix_uses_whole_line(self):
        self.assertEqual(_attach_name("查看完整月报明细（Excel）", self.KEYS),
                         "查看完整月报明细（Excel）",
                         "只抠出「（Excel）」—— 名字里一个能认的字都没有")
        self.assertEqual(_attach_name("查看完整月报明细(Excel)", self.KEYS),
                         "查看完整月报明细(Excel)")

    def test_real_filename_still_stripped(self):
        """有真文件名的形态不能跟着改成整行 —— 微信消息会变成「附件：xxx」。"""
        self.assertEqual(
            _attach_name("明细附件：客户声音待履约汇总_311392_20261027.xlsx",
                         self.KEYS),
            "客户声音待履约汇总_311392_20261027.xlsx")

    def test_whole_line_button_without_suffix(self):
        # 有的卡片按钮不带「（Excel）」后缀
        self.assertEqual(_attach_name("查看完整月报明细", self.KEYS),
                         "查看完整月报明细")

    def test_label_key_alone_does_not_match(self):
        # 「附件：」是标签不是按钮：键后空着说明这行没有附件名
        self.assertEqual(_attach_name("附件：", ["附件："]), "")

    def test_real_report_content(self):
        """19:52 那条月报正文的真尾巴（说明行 + 按钮行）。"""
        content = ("以下为网点维度的履约情况汇总，可查看附件关注小件员履约详情\n"
                   "查看完整月报明细（Excel）")
        self.assertEqual(_attach_name(content, self.KEYS),
                         "查看完整月报明细（Excel）")


class TestFetchAttachOrder(unittest.TestCase):
    """网关在新消息里发现附件行 → 下发 fetch_attach 指令，同名有冷却。"""

    def _ctx(self):
        from bridge.sources.android_gateway import _order_attach as _  # noqa
        return _Ctx(token="t", chat_name="数字质控")

    def test_first_sight_orders_fetch(self):
        from bridge.sources.android_gateway import _order_attach
        ctx = self._ctx()
        unit = type("U", (), {"content": "【当日签履约情况】\n附件：运单明细"})()
        # 真机当日签的形态：不带扩展名
        self.assertEqual(_order_attach(ctx, [unit]), "运单明细")

    def test_cooldown_suppresses_repeat(self):
        from bridge.sources.android_gateway import _order_attach
        ctx = self._ctx()
        unit = type("U", (), {"content": "明细附件：拦截风险明细.xlsx"})()
        self.assertEqual(_order_attach(ctx, [unit]), "拦截风险明细.xlsx")
        self.assertEqual(_order_attach(ctx, [unit]), "")

    def test_no_attach_no_order(self):
        from bridge.sources.android_gateway import _order_attach
        ctx = self._ctx()
        unit = type("U", (), {"content": "今天没有附件的一句普通话"})()
        self.assertEqual(_order_attach(ctx, [unit]), "")

    def test_enabled_by_default(self):
        """新装/老配置（没有 attach_enabled 这个键）必须照常抓附件。"""
        self.assertTrue(self._ctx().attach_enabled)

    def test_disabled_switch_orders_nothing(self):
        """总开关关掉时一条指令都不下，手机端因此完全不动作。

        为什么要有这个开关：原先只能靠「把字眼清空」来关，而网关对空值
        回落默认字眼 —— 用户在设置里清空后它还在抓，属于最难查的那类。
        另外关着的时候不能占用冷却，否则重新打开要白等 10 分钟。
        """
        from bridge.sources.android_gateway import _order_attach
        ctx = self._ctx()
        ctx.attach_enabled = False
        unit = type("U", (), {"content": "附件：运单明细"})()
        self.assertEqual(_order_attach(ctx, [unit]), "")
        self.assertEqual(ctx.attach_orders, {})
        ctx.attach_enabled = True                     # 重新打开立刻就能抓
        self.assertEqual(_order_attach(ctx, [unit]), "运单明细")


class TestFileEndpoint(GatewayCase):
    """/file 上传：鉴权、落盘、sha1 校验、重复去重。"""

    def setUp(self):
        super().setUp()
        self.files = tempfile.mkdtemp(prefix="ddwx-files-")
        self.source._ctx.files_dir = self.files
        self.got: list = []
        self.source._ctx.on_file = lambda p, n, c: self.got.append((p, n, c))

    def _post_file(self, name: str, data: bytes, sha: str | None = None,
                   token: str | None = TOKEN, chat: str = "数字质控"):
        payload = {"name": name, "sha1": sha or hashlib.sha1(data).hexdigest(),
                   "data": base64.b64encode(data).decode("ascii"),
                   "chat": chat}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url("/file"), data=body,
                                     method="POST")
        if token is not None:
            req.add_header("X-Bridge-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_upload_ok_and_callback(self):
        data = _mini_xlsx([["运单号", "状态"], ["777", "未履约"]])
        code, body = self._post_file("运单明细.xlsx", data)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["duplicate"])
        self.assertEqual(len(self.got), 1)
        path, name, chat = self.got[0]
        self.assertEqual(name, "运单明细.xlsx")
        self.assertEqual(chat, "数字质控")
        self.assertTrue(path.startswith(self.files))

    def test_bad_sha_rejected(self):
        data = b"x"
        code, body = self._post_file("a.xlsx", data, sha="0" * 40)
        self.assertEqual(code, 400)
        self.assertEqual(self.got, [])

    def test_reject_without_token(self):
        code, _ = self._post_file("a.xlsx", b"x", token=None)
        self.assertEqual(code, 401)

    def test_duplicate_file_no_second_callback(self):
        data = _mini_xlsx([["a"], ["b"]])
        self._post_file("same.xlsx", data)
        code, body = self._post_file("same.xlsx", data)
        self.assertEqual(code, 200)
        self.assertTrue(body["duplicate"])
        self.assertEqual(len(self.got), 1)

    def test_path_traversal_neutralised(self):
        data = b"x"
        code, _ = self._post_file("../../evil.xlsx", data,
                                  sha=hashlib.sha1(data).hexdigest())
        self.assertEqual(code, 200)
        # 文件必须落在 files_dir 里，而且名字没有 ../
        import os
        listed = os.listdir(self.files)
        self.assertEqual(len(listed), 1)
        self.assertNotIn("..", listed[0])
        self.assertTrue(self.got[0][0].startswith(self.files))


class TestServiceHandleAttachment(unittest.TestCase):
    """handle_attachment：xlsx → 表格图 → 投递，重复文件靠 uid 判重。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from bridge.config import Config, DingTalkConfig, RouteConfig, \
            StorageConfig, WeChatConfig
        dt = DingTalkConfig(mode="android", gateway_host="127.0.0.1",
                            gateway_port=free_port(), gateway_token="t",
                            chat_names=["数字质控"])
        cfg = Config(
            dingtalk=dt,
            storage=StorageConfig(
                db_path=str(Path(self.tmp.name) / "bridge.db")),
            wechat=WeChatConfig(targets=["测试群"], table_image=True,
                                interval=0),
            routes=[RouteConfig(chat_names=["数字质控"], targets=["测试群"])],
        )
        self.sent: list = []
        self.svc = BridgeService(cfg, target=_FakeTarget(self.sent))
        self.addCleanup(self.svc.close)   # 不关的话 bridge.db 句柄占着，临时目录删不掉

    def test_renders_and_delivers_with_image(self):
        data = _mini_xlsx([["运单号", "状态"], ["777", "未履约"]])
        path = Path(self.tmp.name) / "attach.xlsx"
        path.write_bytes(data)
        self.assertTrue(self.svc.handle_attachment(str(path), "运单明细.xlsx",
                                                   "数字质控"))
        self.assertEqual(len(self.sent), 2)          # 文字 + 图片
        kind, payload = self.sent[0]
        self.assertEqual(kind, "text")
        self.assertIn("【附件】运单明细.xlsx", payload)
        kind, payload = self.sent[1]
        self.assertEqual(kind, "image")
        self.assertTrue(Path(payload).is_file())

    def test_duplicate_sha_is_silently_dropped(self):
        data = _mini_xlsx([["a"], ["b"]])
        path = Path(self.tmp.name) / "attach.xlsx"
        path.write_bytes(data)
        self.assertTrue(self.svc.handle_attachment(str(path), "x.xlsx",
                                                   "数字质控"))
        # 同一份文件第二次进来：uid（attach-sha）在库里已存在 → 判重复
        self.assertFalse(self.svc.handle_attachment(str(path), "x.xlsx",
                                                    "数字质控"))
        self.assertEqual(len(self.sent), 2)

    def test_unparsable_file_is_skipped(self):
        path = Path(self.tmp.name) / "bad.xlsx"
        path.write_bytes(b"not a zip")
        self.assertFalse(self.svc.handle_attachment(str(path), "bad.xlsx"))
        self.assertEqual(self.sent, [])


class _FakeTarget:
    """把 send_text / send_image 记进列表的假投递端。"""

    def __init__(self, sink: list):
        self._sink = sink
        self.name = "fake"

    def close(self):
        pass

    def send_text(self, target: str, text: str):
        from bridge.targets.base import SendResult
        self._sink.append(("text", text))
        return SendResult(True, "fake")

    def send_image(self, target: str, path: str):
        from bridge.targets.base import SendResult
        self._sink.append(("image", path))
        return SendResult(True, "fake")


if __name__ == "__main__":
    unittest.main()
