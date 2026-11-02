"""adb 直连切群（app/phone_adb.py）。

背景：2026-11-02 13:13 用户点「切到钉钉群」失败，手机回报「拉不起钉钉
（系统拒绝了直接启动）」—— 前台被另一个程序占着，无障碍服务被 ROM 拦了。
改成电脑端 adb 直连后，这里钉的是**判定逻辑**和**不乱点**这两件事：

    判定：标题（tv_title）算不算目标群；
    不乱点：界面上看不见那个群时，绝不能凭坐标瞎点 —— 用户的手机可能
            正被别的程序用着，误触的代价比失败大得多。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import phone_adb as pa  # noqa: E402

GROUP = "【数字质控】浙江杭州萧山区大江东公司"

# 真机结构（2026-11-02 抓自聊天页，已裁掉无关节点）
XML_CHAT = f"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node text="" resource-id="" class="android.widget.FrameLayout" bounds="[0,0][1176,2480]">
    <node text="{GROUP}" resource-id="com.alibaba.android.rimet:id/tv_title"
          class="android.widget.TextView" clickable="true" bounds="[302,143][868,194]" />
    <node text="今天 12:56" resource-id="" class="android.widget.TextView"
          clickable="false" bounds="[100,900][300,950]" />
  </node>
</hierarchy>"""

XML_OTHER_CHAT = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node text="另一个业务群" resource-id="com.alibaba.android.rimet:id/tv_title"
        class="android.widget.TextView" clickable="true" bounds="[302,143][700,194]" />
</hierarchy>"""

XML_LIST = f"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node text="{GROUP}" resource-id="com.alibaba.android.rimet:id/tv_conversation_name"
        class="android.widget.TextView" clickable="true" bounds="[100,600][800,700]" />
</hierarchy>"""

XML_EMPTY = "<hierarchy rotation='0'><node text='' bounds='[0,0][100,100]'/></hierarchy>"


class ParseTests(unittest.TestCase):
    def test_parse_bounds_and_center(self):
        nodes = pa.parse_nodes(XML_CHAT)
        self.assertEqual(len(nodes), 3)
        title = [n for n in nodes if n.rid == pa.TITLE_RID][0]
        self.assertEqual(title.text, GROUP)
        self.assertEqual(title.bounds, (302, 143, 868, 194))
        self.assertEqual(title.center, (585, 168))

    def test_node_without_bounds_is_dropped(self):
        nodes = pa.parse_nodes("<hierarchy><node text='x' /></hierarchy>")
        self.assertEqual(nodes, [])

    def test_parse_garbage_is_empty_not_crash(self):
        self.assertEqual(pa.parse_nodes(""), [])
        self.assertEqual(pa.parse_nodes("not xml at all"), [])

    def test_find_title(self):
        self.assertEqual(pa.find_title(pa.parse_nodes(XML_CHAT)), GROUP)
        self.assertEqual(pa.find_title(pa.parse_nodes(XML_LIST)), "")

    def test_find_text_exact_then_contains(self):
        nodes = pa.parse_nodes(XML_CHAT)
        self.assertIsNotNone(pa.find_text(nodes, GROUP, exact=True))
        # 钉钉标题常带后缀，包含匹配要能兜住
        self.assertIsNotNone(pa.find_text(nodes, "数字质控"))
        self.assertIsNone(pa.find_text(nodes, "不存在的群"))


class SameGroupTests(unittest.TestCase):
    def test_exact_and_prefix(self):
        self.assertTrue(pa.same_group(GROUP, GROUP))
        self.assertTrue(pa.same_group(GROUP + "（8）", GROUP))
        self.assertTrue(pa.same_group(GROUP, GROUP + "（8）"))

    def test_rejects_other_group(self):
        self.assertFalse(pa.same_group("另一个业务群", GROUP))
        self.assertFalse(pa.same_group("", GROUP))
        self.assertFalse(pa.same_group(GROUP, ""))


class FakeAdb:
    """按脚本回放 adb 的读数，并记录所有「动手」的动作。"""

    def __init__(self, *, front=None, launch_ok=True,
                 dumps=None, focus_seq=None):
        self._front = pa.DINGTALK_PKG if front is None else front
        self.launch_ok = launch_ok
        self._dumps = list(dumps or [])
        self._focus_seq = list(focus_seq or [])
        self.taps: list[tuple[int, int]] = []
        self.swipes = 0
        self.launched = 0
        self.woken = 0

    def pick_serial(self):
        return "FAKE123"

    def wake(self):
        self.woken += 1
        return "Awake"

    def focused_pkg(self):
        if self._focus_seq:
            return self._focus_seq.pop(0)
        return self._front

    def ensure_dingtalk(self, retries=2):
        if self._front == pa.DINGTALK_PKG:
            return True
        if not self.launch_ok:
            return False
        self.launched += 1
        self._front = pa.DINGTALK_PKG
        return True

    def dump_nodes(self):
        if not self._dumps:
            return []
        return pa.parse_nodes(self._dumps.pop(0))

    def tap(self, x, y):
        self.taps.append((x, y))

    def swipe_up(self):
        self.swipes += 1


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self._sleep = mock.patch.object(pa.time, "sleep", lambda *_: None)
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def test_already_in_target_group(self):
        adb = FakeAdb(dumps=[XML_CHAT])
        ok, msg = pa.switch_to_group(GROUP, adb=adb)
        self.assertTrue(ok)
        self.assertIn("本来就在", msg)
        self.assertEqual(adb.taps, [], "已经在目标群时不该点任何东西")
        self.assertEqual(adb.launched, 0, "已在前台不该再拉起一次")

    def test_launches_when_another_app_in_front(self):
        # 模拟 13:13 的现场：前台是申行者，钉钉在后台
        adb = FakeAdb(front="cn.sto.sxz.core.app", dumps=[XML_CHAT])
        ok, msg = pa.switch_to_group(GROUP, adb=adb)
        self.assertTrue(ok, msg)
        self.assertEqual(adb.launched, 1)
        self.assertIn("adb 已把钉钉切回", msg)
        self.assertEqual(adb.taps, [], "拉起后回到原聊天页就够，不该再点")

    def test_cannot_launch_reports_current_app(self):
        adb = FakeAdb(front="cn.sto.sxz.core.app", launch_ok=False)
        ok, msg = pa.switch_to_group(GROUP, adb=adb)
        self.assertFalse(ok)
        self.assertIn("cn.sto.sxz.core.app", msg, "失败说明里要带当前前台包名")
        self.assertIn("adb 拉不起钉钉", msg)
        self.assertEqual(adb.taps, [])

    def test_wrong_group_then_finds_in_list(self):
        adb = FakeAdb(dumps=[XML_OTHER_CHAT, XML_LIST, XML_CHAT])
        ok, msg = pa.switch_to_group(GROUP, adb=adb)
        self.assertTrue(ok, msg)
        self.assertEqual(adb.taps, [(450, 650)], "应点会话列表里那一行")

    def test_never_taps_when_group_not_visible(self):
        """看不见就不点 —— 手机可能正被别的程序用着，瞎点的代价太大。"""
        adb = FakeAdb(dumps=[XML_OTHER_CHAT] * 4)
        ok, msg = pa.switch_to_group(GROUP, adb=adb, rounds=3)
        self.assertFalse(ok)
        self.assertEqual(adb.taps, [])
        self.assertIn("另一个业务群", msg, "要说清当前停在哪")
        self.assertGreaterEqual(adb.swipes, 1, "没找到应该滑一屏再看")

    def test_empty_group_name(self):
        ok, msg = pa.switch_to_group("", adb=FakeAdb())
        self.assertFalse(ok)
        self.assertIn("没配目标群", msg)

    def test_adb_error_becomes_readable_message(self):
        class Broken(FakeAdb):
            def pick_serial(self):
                raise pa.AdbError("没有在线设备：USB 线没插稳")

        ok, msg = pa.switch_to_group(GROUP, adb=Broken())
        self.assertFalse(ok)
        self.assertIn("adb 不通", msg)
        self.assertIn("USB", msg)

    def test_empty_screen_does_not_loop_taps(self):
        adb = FakeAdb(dumps=[XML_EMPTY] * 4)
        ok, _msg = pa.switch_to_group(GROUP, adb=adb, rounds=2)
        self.assertFalse(ok)
        self.assertEqual(adb.taps, [])


class ScreenSizeTests(unittest.TestCase):
    def test_override_wins_for_foldable(self):
        # 真机：折叠屏同时报两个尺寸，必须以 Override 为准（dump 的坐标系）
        adb = pa.Adb.__new__(pa.Adb)
        adb.serial = "X"
        adb.shell = lambda cmd, timeout=15: (
            "Physical size: 2480x2200\nOverride size: 1176x2480")
        self.assertEqual(adb.screen_size(), (1176, 2480))

    def test_physical_when_no_override(self):
        adb = pa.Adb.__new__(pa.Adb)
        adb.serial = "X"
        adb.shell = lambda cmd, timeout=15: "Physical size: 1080x2340"
        self.assertEqual(adb.screen_size(), (1080, 2340))


if __name__ == "__main__":
    unittest.main(verbosity=2)
