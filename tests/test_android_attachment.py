"""附件转发链路的电脑侧回归测试。

链路：卡片正文出现「附件：xxx」→ 网关下发 fetch_attach 指令（带冷却）→
手机 POST /file 上传 → 网关落盘去重 → service.handle_attachment 投递**原文件**
（media_path 指向上传的那份文件本身，2026-10-31 起不再渲染表格图）。
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

    def test_weekly_report_covered_by_general_key(self):
        """「查看完整周报明细（Excel）」→ 必须能认出来（2026-10-31 漏抓）。

        配置里原来写死「查看完整月报明细」，周报换了个字就整条抓取链路不
        启动（日志里连一条「发现附件」都没有）。现在配的是前缀「查看完整」，
        周报/月报/日报一网打尽。

        ⚠ 2026-11-01 18:0x 改了取值口径：带「（Excel）」这种格式标记的按钮，
        名字取**整行原文**（手机端屏上可点的就是那一整行），不再是去掉前缀的
        残句「周报明细（Excel）」—— 后者屏上根本不存在。
        """
        keys = _clean_attach_keys("附件：,附件:,明细附件,查看完整,明细运单,下载附件")
        self.assertEqual(_attach_name("查看完整周报明细（Excel）", keys),
                         "查看完整周报明细（Excel）")
        self.assertEqual(_attach_name("查看完整月报明细（Excel）", keys),
                         "查看完整月报明细（Excel）")
        # 没有格式标记的按钮文案维持原口径（去掉前缀只留名字）
        self.assertEqual(_attach_name("查看完整日报明细", keys), "日报明细")


class TestAttachKeys(unittest.TestCase):
    """触发字眼可配置（2026-10-27 用户要求）：列表、逗号串都认；坏配置回落默认。"""

    def test_default_keys(self):
        # 2026-10-31 补「明细：」—— 滞留卡的附件行是「明细：下载3天滞留运单明细」
        self.assertEqual(_clean_attach_keys(None),
                         ["附件：", "附件:", "明细：", "明细:"])
        self.assertEqual(_clean_attach_keys([]),
                         ["附件：", "附件:", "明细：", "明细:"])

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
        # 2026-11-01 用户加规则「只要带明细字眼的都点开转发原文件」之后，
        # 「附件：运单明细」这行即便字眼表里没有「附件：」，也会被「明细」
        # 兜底规则整行收下（见 _attach_names 第 5 步）。名字取整行是因为它就是
        # 屏上可点的文本 —— 手机端 startsWith(整行) 必中。
        self.assertEqual(_attach_name("附件：运单明细", ["明细单："]), "附件：运单明细")
        # 不含「明细」的行仍严格看字眼：配置换掉后默认字眼不再触发。
        self.assertEqual(_attach_name("附件：客户声音.xlsx", ["明细单："]), "")

    def test_bad_input_falls_back(self):
        self.assertEqual(_clean_attach_keys(",,，"),
                         ["附件：", "附件:", "明细：", "明细:"])


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

    def test_download_button_alone_needs_its_own_key(self):
        """2026-10-31 13:18：卡片末尾只有按钮「下载附件」，没冒号也没文件名。

        词表里有它 → 整行全等命中，名字就是这行原文（手机端按屏上文本点，
        必中）；词表里没有 → 整条消息的 Excel 根本不下单，这就是那次现场。
        """
        keys = ["附件：", "附件:", "明细附件", "查看完整月报明细",
                "明细运单", "下载附件"]
        content = ("所有小件员未签信息我已经整理成Excel附件，请立即催签！\n"
                   "下载附件")
        self.assertEqual(_attach_name(content, keys), "下载附件")
        # 词表里没有时：精确/整行判据都不中，但 2026-11-01 起的「万能字眼」
        # （短行 + 名词 + 动作）会把它认出来 —— 这正是用户要的「别漏」。
        self.assertEqual(_attach_name(content, ["附件：", "附件:"]), "下载附件")
        # 关掉万能字眼才回到旧行为（词表决定一切）
        self.assertEqual(
            _attach_name(content, ["附件：", "附件:"], auto=False), "")


class TestHasKnownUnit(unittest.TestCase):
    """H5 锚点对不上时「就地重建、不翻页」的安全前提：屏上必须有已知消息。

    2026-10-31 用户第二次反馈「上下快速跳动找锚点」：当天 H5 锚点对不上
    51 次、原生帧只有 3 次。跳页几乎全来自 H5 分支，而 H5 卡片的指纹每
    重绘一次就整批变 —— 翻页根本找不回锚点。现在改成：视口在最新处
    （frame.hints 为空）且屏上还有已知消息时，直接重建 + 打捞，不翻页。
    这个测试守的就是「有没有已知消息」这条判据 —— 判错就会把整屏登记成
    历史（丢消息）或白翻（跳屏）。
    """

    def _shim(self):
        from bridge.sources.android_gateway import _Ctx, _Handler

        class Shim(_Handler):
            """只借 _Handler 的判据方法，不跑 BaseHTTPRequestHandler.__init__。"""

            def __init__(self):
                self.ctx = _Ctx(token="t", chat_name="数字质控")

        return Shim()

    class _U:
        def __init__(self, fp, sender="履约跟单专员", content="正文"):
            self.fp, self.sender, self.content = fp, sender, content

    class _F:
        def __init__(self, units):
            self.units = units

    def test_true_when_unit_in_last_good(self):
        from bridge.sources.android_gateway import _Handler
        s = self._shim()
        s.ctx.last_good_units = [self._U("fp-a")]
        self.assertTrue(_Handler._has_known_unit(
            s, self._F([self._U("fp-x"), self._U("fp-a")])))

    def test_false_when_screen_is_all_new(self):
        """整屏全新（换群/深翻）→ 不许就地重建，该老老实实翻页找锚点。"""
        from bridge.sources.android_gateway import _Handler
        s = self._shim()
        s.ctx.last_good_units = [self._U("fp-a")]
        self.assertFalse(_Handler._has_known_unit(
            s, self._F([self._U("fp-x"), self._U("fp-y")])))

    def test_true_when_content_already_delivered(self):
        """H5 卡片重绘换了指纹 → 按正文认（与打捞/去重同一套判据）。"""
        from bridge.sources.android_gateway import _Handler
        s = self._shim()
        s.ctx.last_good_units = []
        s.ctx.delivered_texts = [("履约跟单专员", "今天签收 98.07%")]
        self.assertTrue(_Handler._has_known_unit(
            s, self._F([self._U("fp-new", content="今天签收 98.07%")])))


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


class TestMultiAttachQueue(unittest.TestCase):
    """一张卡片有多条「明细：…」→ 全部入队，逐个抓（2026-10-31 真机形态）。

    服务诊断专员的滞留卡一片里就有 4 条明细；旧实现只取第一条，
    其余三条永远不会被点，表现成「附件只来了一部分」。
    """

    KEYS = ("附件：,附件:,明细：,明细:,明细附件,查看完整,"
            "明细运单,运单明细,下载附件")
    CARD = (
        "【普通件滞留质控通知】\n"
        "【普通件滞留3天】311票\n"
        "截至20261018，浙江杭州萧山区大江东公司有311票普通件滞留满3天。\n"
        "明细：下载3天滞留运单明细\n"
        "【普通件滞留5天】157票\n"
        "明细：下载5天滞留运单明细\n"
        "【普通件滞留7天】104票\n"
        "明细：下载7天滞留运单明细\n"
        "【超长期滞留14天】32票\n"
        "明细：下载14天滞留运单明细"
    )

    def _ctx(self):
        from bridge.sources.android_gateway import _Ctx, _clean_attach_keys
        ctx = _Ctx(token="t", chat_name="数字质控")
        ctx.attach_keys = _clean_attach_keys(self.KEYS)
        return ctx

    def _unit(self, content=None):
        return type("U", (), {"content": content or self.CARD})()

    def test_mingxi_colon_line_matches(self):
        """「明细：下载生鲜滞留运单明细」—— 用户 2026-10-31 问的那一行。"""
        from bridge.sources.android_gateway import _attach_name, _clean_attach_keys
        keys = _clean_attach_keys(self.KEYS)
        self.assertEqual(
            _attach_name("【生鲜滞留D1 18:00】3票\n明细：下载生鲜滞留运单明细", keys),
            "下载生鲜滞留运单明细")

    def test_all_rows_parsed(self):
        from bridge.sources.android_gateway import _attach_names
        names = _attach_names(self.CARD, self._ctx().attach_keys)
        self.assertEqual(names, ["下载3天滞留运单明细", "下载5天滞留运单明细",
                                 "下载7天滞留运单明细", "下载14天滞留运单明细"])

    def test_queue_holds_rest_while_one_is_running(self):
        from bridge.sources.android_gateway import _order_attach
        ctx = self._ctx()
        self.assertEqual(_order_attach(ctx, [self._unit()]), "下载3天滞留运单明细")
        self.assertEqual(ctx.attach_active, "下载3天滞留运单明细")
        # 手机在忙：再来的帧不能抢着下发第二条
        self.assertEqual(_order_attach(ctx, [self._unit()]), "")
        self.assertEqual(len(ctx.attach_queue), 3)

    def test_next_dispatched_after_file_arrives(self):
        from bridge.sources.android_gateway import _order_attach
        ctx = self._ctx()
        _order_attach(ctx, [self._unit()])
        ctx.attach_active = ""                 # /file 到达时的效果
        self.assertEqual(_order_attach(ctx, []), "下载5天滞留运单明细")

    def test_next_dispatched_after_timeout(self):
        """第一条彻底失败也得放行，否则队列永远卡住。"""
        import time as _t
        from bridge.sources.android_gateway import (ATTACH_FETCH_TIMEOUT,
                                                    _order_attach)
        ctx = self._ctx()
        _order_attach(ctx, [self._unit()])
        ctx.attach_orders["下载3天滞留运单明细"] = (
            _t.monotonic() - ATTACH_FETCH_TIMEOUT - 1)
        self.assertEqual(_order_attach(ctx, []), "下载5天滞留运单明细")

    def test_same_name_not_queued_twice(self):
        """卡片重渲染每帧都来一遍，队列里不能堆同一个名字。"""
        from bridge.sources.android_gateway import _order_attach
        ctx = self._ctx()
        _order_attach(ctx, [self._unit()])
        _order_attach(ctx, [self._unit()])
        _order_attach(ctx, [self._unit()])
        self.assertEqual(ctx.attach_queue,
                         ["下载5天滞留运单明细", "下载7天滞留运单明细",
                          "下载14天滞留运单明细"])

    def test_seen_names_are_remembered(self):
        """每帧扫到的附件行都要记下来 —— /file 回来时靠它起名。"""
        from bridge.sources.android_gateway import _order_attach
        ctx = self._ctx()
        _order_attach(ctx, [self._unit()])
        names = [n for n, _ts in ctx.attach_recent]
        self.assertIn("下载3天滞留运单明细", names)
        self.assertIn("下载14天滞留运单明细", names)


class TestDisplayNameForFile(unittest.TestCase):
    """文件传回来时叫什么名字（2026-11-01 用户：「上面附件叫什么就用什么」）。"""

    PHONE = ("2026-11-01_311392_intercept_risk_center_not_arrived_"
             "1793513190555.xlsx")

    def _ctx(self):
        from bridge.sources.android_gateway import _Ctx
        return _Ctx(token="t", chat_name="数字质控")

    def test_active_order_wins(self):
        from bridge.sources.android_gateway import _display_name_for_file
        ctx = self._ctx()
        ctx.attach_active = "拦截风险明细.xlsx"
        ctx.attach_recent.append(("别的名字.xlsx", __import__("time").monotonic()))
        self.assertEqual(_display_name_for_file(ctx, self.PHONE),
                         "拦截风险明细.xlsx")

    def test_recent_unique_same_ext_used_on_manual_click(self):
        """用户手动点链接时没有 active 单，靠最近见过的附件行按扩展名对。"""
        import time
        from bridge.sources.android_gateway import _display_name_for_file
        ctx = self._ctx()
        ctx.attach_recent.append(("拦截风险明细.xlsx", time.monotonic() - 5))
        self.assertEqual(_display_name_for_file(ctx, self.PHONE),
                         "拦截风险明细.xlsx")

    def test_ambiguous_recent_gives_up(self):
        """最近有好几个 .xlsx 附件行 → 猜错比留英文名更糟，宁可不起名。"""
        import time
        from bridge.sources.android_gateway import _display_name_for_file
        ctx = self._ctx()
        now = time.monotonic()
        ctx.attach_recent.append(("A明细.xlsx", now - 5))
        ctx.attach_recent.append(("B明细.xlsx", now - 4))
        self.assertEqual(_display_name_for_file(ctx, self.PHONE), "")

    def test_stale_name_ignored(self):
        import time
        from bridge.sources.android_gateway import (ATTACH_NAME_WINDOW,
                                                    _display_name_for_file)
        ctx = self._ctx()
        ctx.attach_recent.append(
            ("拦截风险明细.xlsx", time.monotonic() - ATTACH_NAME_WINDOW - 10))
        self.assertEqual(_display_name_for_file(ctx, self.PHONE), "")



class TestFileEndpoint(GatewayCase):
    """/file 上传：鉴权、落盘、sha1 校验、重复去重。"""

    def setUp(self):
        super().setUp()
        self.files = tempfile.mkdtemp(prefix="ddwx-files-")
        self.source._ctx.files_dir = self.files
        self.got: list = []
        self.source._ctx.on_file = lambda p, n, c: self.got.append((p, n, c))

    def _wait_got(self, n: int, timeout: float = 3.0) -> None:
        """等后台投递线程把回调送到。

        2026-11-02 起 /file 的回调是**异步**的（同步执行曾把手机上传拖到
        30 秒超时）—— 响应回来时回调可能还没跑，断言前必须等。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.got) >= n:
                return
            time.sleep(0.02)
        self.fail(f"3 秒内只等到 {len(self.got)}/{n} 次回调")

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
        self._wait_got(1)
        self.assertEqual(len(self.got), 1)
        path, name, chat = self.got[0]
        self.assertEqual(name, "运单明细.xlsx")
        self.assertEqual(chat, "数字质控")
        self.assertTrue(path.startswith(self.files))

    def test_card_name_passed_to_callback(self):
        """回调吃 4 个参数时，第 4 个就是「这条附件行叫什么」（显示名靠它）。"""
        got4: list = []
        self.source._ctx.on_file = lambda p, n, c, w: got4.append((n, w))
        self.source._ctx.attach_active = "拦截风险明细.xlsx"
        data = _mini_xlsx([["运单号"], ["777"]])
        phone = ("2026-11-01_311392_intercept_risk_center_not_arrived_"
                 "1793513190555.xlsx")
        code, body = self._post_file(phone, data)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        deadline = time.time() + 3
        while time.time() < deadline and not got4:
            time.sleep(0.02)
        self.assertEqual(got4, [(phone, "拦截风险明细.xlsx")])

    def test_empty_chat_falls_back_to_watch_group(self):
        """手机没带 chat → 用网关盯的群名兜底，附件才进得去微信。

        2026-10-31 17:50 事故：报文里 chat 是空串，附件的 Message.chat_name
        也跟着空，路由规则（按群名子串匹配）一条都不命中 → 日志只有一行
        「未命中转发规则，仅入库」，用户那边就是「附件没转发」。
        """
        data = _mini_xlsx([["运单号"], ["777"]])
        code, body = self._post_file("周报明细.xlsx", data, chat="")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self._wait_got(1)
        self.assertEqual(self.got[0][2], "数字质控")     # watch_group 兜底

    def test_bad_sha_rejected(self):
        data = b"x"
        code, body = self._post_file("a.xlsx", data, sha="0" * 40)
        self.assertEqual(code, 400)
        self.assertEqual(self.got, [])

    def test_reject_without_token(self):
        code, _ = self._post_file("a.xlsx", b"x", token=None)
        self.assertEqual(code, 401)

    def test_duplicate_file_still_callbacks(self):
        """磁盘已有同名文件也**必须**继续回调下游（2026-11-01 改）。

        旧行为「重复就拦截」留下一个死角：文件落盘了、第一次回调却没发成功
        （目标群名对不上 → 未命中规则），之后手机重传一百次都被 fresh=False
        拦成「重复跳过」—— 这份文件永远发不出去（14:46~14:53 真实事故）。
        现在「发没发成功」由下游按 deliveries 表判定，网关只负责转交。
        """
        data = _mini_xlsx([["a"], ["b"]])
        self._post_file("same.xlsx", data)
        self._wait_got(1)                      # 等第一份投完，inflight 才清
        code, body = self._post_file("same.xlsx", data)
        self.assertEqual(code, 200)
        self.assertTrue(body["duplicate"])
        self._wait_got(2)
        self.assertEqual(len(self.got), 2)

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
        self._wait_got(1)
        self.assertTrue(self.got[0][0].startswith(self.files))


class TestFileAsyncDelivery(GatewayCase):
    """回调异步化（2026-11-02 16:39 事故）。

    以前 ``/file`` 在 HTTP 线程里**同步**跑 handle_attachment，投递微信要点
    十几下、动辄 20~40 秒；手机端上传 30 秒等不到响应就记「上传失败」——
    实际 PC 侧 1 秒后投成了。15:32、16:39 两次都这么被误报。

    钉三件事：响应先回、回调在后台线程执行、在途重传不重发。
    """

    def setUp(self):
        super().setUp()
        self.files = tempfile.mkdtemp(prefix="ddwx-files-")
        self.source._ctx.files_dir = self.files

    def _post_file(self, name: str, data: bytes):
        payload = {"name": name, "sha1": hashlib.sha1(data).hexdigest(),
                   "data": base64.b64encode(data).decode("ascii"),
                   "chat": "数字质控"}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url("/file"), data=body,
                                     method="POST")
        req.add_header("X-Bridge-Token", TOKEN)
        start = time.monotonic()
        with urllib.request.urlopen(req, timeout=5) as resp:
            out = resp.status, json.loads(resp.read().decode("utf-8"))
        return out, time.monotonic() - start

    def test_reply_returns_before_slow_callback(self):
        """回调慢 3 秒，响应也必须立刻回 —— 手机端不能陪投递一起等。"""
        gate = threading.Event()
        self.source._ctx.on_file = lambda *a: gate.wait(5)
        data = _mini_xlsx([["a"], ["b"]])
        (code, body), elapsed = self._post_file("慢投递.xlsx", data)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertLess(elapsed, 1.0, "响应被投递拖住了（又回老路了）")
        gate.set()                       # 放行后台线程，别把 teardown 拖死

    def test_callback_runs_in_background_thread(self):
        done = threading.Event()
        threads: list = []

        def cb(*a):
            threads.append(threading.current_thread())
            done.set()

        self.source._ctx.on_file = cb
        data = _mini_xlsx([["a"], ["b"]])
        self._post_file("异步.xlsx", data)
        self.assertTrue(done.wait(3), "回调没被执行")
        self.assertNotEqual(threads[0], threading.current_thread())

    def test_inflight_duplicate_not_recalled(self):
        """同一份文件还在投递中，手机重传 → 不再回调，回 duplicate=true。

        投递异步后 deliveries 判重有窗口（上一份还没落库），靠
        attach_inflight 补位，不然超时重传会把一条附件发两遍。
        """
        gate = threading.Event()
        calls: list = []
        self.source._ctx.on_file = lambda *a: (calls.append(a), gate.wait(5))
        data = _mini_xlsx([["a"], ["b"]])
        (code1, _), _t = self._post_file("重复.xlsx", data)
        self.assertEqual(code1, 200)
        deadline = time.time() + 3
        while time.time() < deadline and not calls:
            time.sleep(0.02)
        (code2, body2), _t2 = self._post_file("重复.xlsx", data)
        self.assertEqual(code2, 200)
        self.assertTrue(body2["duplicate"])
        self.assertEqual(len(calls), 1, "在途重传又被投了一遍")
        gate.set()

    def test_inflight_cleared_after_callback(self):
        """投递完成后 inflight 必须清掉 —— 否则下次同内容文件永远被忽略。"""
        self.source._ctx.on_file = lambda *a: None
        data = _mini_xlsx([["a"], ["b"]])
        sha = hashlib.sha1(data).hexdigest()
        self._post_file("清理.xlsx", data)
        deadline = time.time() + 3
        while time.time() < deadline and sha in self.source._ctx.attach_inflight:
            time.sleep(0.02)
        self.assertNotIn(sha, self.source._ctx.attach_inflight)


class TestServiceHandleAttachment(unittest.TestCase):
    """handle_attachment：附件 → 原文件投递，重复文件靠 uid 判重。"""

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

    def test_sends_original_file(self):
        """附件一律发原文件（2026-10-31 用户定），不再渲染表格图。"""
        data = _mini_xlsx([["运单号", "状态"], ["777", "未履约"]])
        path = Path(self.tmp.name) / "attach.xlsx"
        path.write_bytes(data)
        self.assertTrue(self.svc.handle_attachment(str(path), "运单明细.xlsx",
                                                   "数字质控"))
        self.assertEqual(len(self.sent), 2)          # 提示文字 + 原文件
        kind, payload = self.sent[0]
        self.assertEqual(kind, "text")
        self.assertIn("【附件】运单明细.xlsx", payload)
        self.assertIn("1 行", payload)
        kind, payload = self.sent[1]
        self.assertEqual(kind, "file")
        # 2026-11-01 起发的是「文件名整理过」的副本：内容必须与原文件逐字节
        # 相同，但显示名要干净（不带 sha 前缀、不带「（Excel）」）。
        got = Path(payload)
        self.assertEqual(got.name, "运单明细.xlsx", "显示名要干净")
        self.assertEqual(got.read_bytes(), data, "内容必须是传上来的那个文件")

    def test_card_name_used_as_file_name(self):
        """文件名沿用消息里那条附件行（2026-11-01 用户要求）。

        手机回传的是 `2026-11-01_311392_intercept_risk_center_not_arrived_….xlsx`
        这种英文串，消息里写的却是「明细附件：拦截风险明细.xlsx」。
        """
        data = _mini_xlsx([["运单号"], ["777"]])
        phone = ("2026-11-01_311392_intercept_risk_center_not_arrived_"
                 "1793513190555.xlsx")
        path = Path(self.tmp.name) / phone
        path.write_bytes(data)
        self.assertTrue(self.svc.handle_attachment(
            str(path), phone, "数字质控", wanted="明细附件：拦截风险明细.xlsx"))
        self.assertIn("【附件】拦截风险明细.xlsx", self.sent[0][1])
        self.assertEqual(Path(self.sent[1][1]).name, "拦截风险明细.xlsx")
        self.assertEqual(Path(self.sent[1][1]).read_bytes(), data)

    def test_non_xlsx_attachment_still_forwarded(self):
        """pdf/docx 这类解析不了的附件也得发出去 —— 解析只为写行数。"""
        path = Path(self.tmp.name) / "ok.pdf"
        path.write_bytes(b"%PDF-1.4 not a zip")
        self.assertTrue(self.svc.handle_attachment(str(path), "ok.pdf",
                                                   "数字质控"))
        self.assertEqual([k for k, _ in self.sent], ["text", "file"])
        self.assertEqual(self.sent[0][1], "【附件】ok.pdf")

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

    def test_empty_file_is_skipped(self):
        """0 字节的文件没有转发价值，直接丢（且不该发一条只有标题的提示）。"""
        path = Path(self.tmp.name) / "empty.xlsx"
        path.write_bytes(b"")
        self.assertFalse(self.svc.handle_attachment(str(path), "empty.xlsx"))
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

    def send_file(self, target: str, path: str):
        from bridge.targets.base import SendResult
        self._sink.append(("file", path))
        return SendResult(True, "fake")


if __name__ == "__main__":
    unittest.main()
