"""安卓探测分析器的测试。

分析器是「阶段 0 闸门」的判据来源，判错了会直接导致错误的技术决策：
  · 假阳性 → 兴师动众做完阶段 1，才发现读不到东西；
  · 假阴性 → 白白放弃一条比 OCR 好得多的路。
所以这里既测正例也测反例。

用 importlib 从路径加载 —— `tools/` 不是包，也不该为了让测试能 import 而
把它变成包（那会让安装包里的目录结构也跟着变）。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "analyze_uiautomator",
    BASE / "tools" / "android_probe" / "analyze_uiautomator.py")
analyze = importlib.util.module_from_spec(_SPEC)
# 必须先登记进 sys.modules 再 exec：@dataclass 解析注解时要回查
# sys.modules[cls.__module__]，没登记就会 AttributeError: 'NoneType' has no __dict__
sys.modules[_SPEC.name] = analyze               # type: ignore[union-attr]
_SPEC.loader.exec_module(analyze)               # type: ignore[union-attr]


CHAT_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.alibaba.android.rimet" content-desc="" scrollable="false" bounds="[0,0][1080,2400]">
    <node index="0" text="【数字质控】浙江杭州萧山区大江东公司" class="android.widget.TextView" package="com.alibaba.android.rimet" content-desc="" scrollable="false" bounds="[120,80][900,140]" />
    <node index="1" text="" resource-id="com.alibaba.android.rimet:id/chat_list" class="androidx.recyclerview.widget.RecyclerView" package="com.alibaba.android.rimet" content-desc="" scrollable="true" bounds="[0,160][1080,2100]">
      <node index="0" text="" class="android.widget.LinearLayout" package="com.alibaba.android.rimet" content-desc="" scrollable="false" bounds="[0,300][1080,560]">
        <node index="0" text="签收跟单专员 机器人" class="android.widget.TextView" package="com.alibaba.android.rimet" content-desc="" scrollable="false" bounds="[60,300][500,340]" />
        <node index="1" text="@所有人【客户声音履约风险提醒】以下订单存在投诉风险，请尽快跟进：374票 陶龙" class="android.widget.TextView" package="com.alibaba.android.rimet" content-desc="" scrollable="false" bounds="[60,350][1020,540]" />
      </node>
      <node index="1" text="" class="android.widget.LinearLayout" package="com.alibaba.android.rimet" content-desc="" scrollable="false" bounds="[0,600][1080,700]">
        <node index="0" text="09:12" class="android.widget.TextView" package="com.alibaba.android.rimet" content-desc="" scrollable="false" bounds="[500,600][580,640]" />
      </node>
      <node index="2" text="" class="android.widget.ImageView" package="com.alibaba.android.rimet" content-desc="[图片]" scrollable="false" bounds="[60,1100][420,1400]" />
    </node>
    <node index="2" text="输入消息…" class="android.widget.EditText" package="com.alibaba.android.rimet" content-desc="" scrollable="false" bounds="[0,2140][900,2300]" />
  </node>
</hierarchy>
"""

SELF_DRAWN_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.alibaba.android.rimet" content-desc="" scrollable="false" bounds="[0,0][1080,2400]">
    <node index="0" text="" resource-id="com.alibaba.android.rimet:id/chat_list" class="android.widget.FrameLayout" package="com.alibaba.android.rimet" content-desc="" scrollable="true" bounds="[0,160][1080,2100]" />
  </node>
</hierarchy>
"""

OTHER_APP_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="设置" class="android.widget.TextView" package="com.android.settings" content-desc="" scrollable="false" bounds="[0,0][100,100]" />
</hierarchy>
"""


def tree_of(xml: str):
    root = ET.fromstring(xml)
    for child in root:
        if child.tag == "node":
            return analyze.build_tree(child)
    raise AssertionError("样例 XML 里没有 node")


class TestParse(unittest.TestCase):

    def test_counts_text_nodes(self):
        # 群名 / 发送者 / 正文 / 时间 / [图片] / 输入框 = 6 个
        root = tree_of(CHAT_XML)
        nodes = list(analyze.walk(root))
        self.assertEqual(len([n for n in nodes if n.label]), 6)

    def test_bounds_parsed(self):
        root = tree_of(CHAT_XML)
        self.assertEqual(root.bounds, (0, 0, 1080, 2400))

    def test_short_class_and_id(self):
        root = tree_of(CHAT_XML)
        rv = [n for n in analyze.walk(root) if "RecyclerView" in n.cls][0]
        self.assertEqual(rv.short_class, "RecyclerView")
        self.assertEqual(rv.short_id, "chat_list")

    def test_subtree_text_joins_in_order(self):
        root = tree_of(CHAT_XML)
        rv = [n for n in analyze.walk(root) if "RecyclerView" in n.cls][0]
        self.assertIn("签收跟单专员", analyze.subtree_text(rv.children[0]))
        self.assertIn("374票", analyze.subtree_text(rv.children[0]))


class TestClassifyChild(unittest.TestCase):
    """分类错一个，阶段 1 就会把「09:12」当成一条消息转发出去。"""

    def children(self):
        root = tree_of(CHAT_XML)
        rv = [n for n in analyze.walk(root) if "RecyclerView" in n.cls][0]
        return rv.children

    def test_message_child(self):
        self.assertEqual(analyze.classify_child(self.children()[0]), "消息")

    def test_time_separator_child(self):
        self.assertEqual(analyze.classify_child(self.children()[1]), "时间分隔")

    def test_placeholder_child_is_not_a_message(self):
        self.assertEqual(analyze.classify_child(self.children()[2]), "非文本")

    def test_cross_day_labels_are_separators(self):
        for label in ("昨天", "昨天 09:12", "星期一", "10-16", "2026-10-16"):
            with self.subTest(label=label):
                node = analyze.Node(text=label)
                self.assertEqual(analyze.classify_child(node), "时间分隔")

    def test_robot_body_with_full_width_brackets_is_a_message(self):
        """机器人正文用全角【】，不能被占位符规则误伤。"""
        node = analyze.Node(text="【客户声音履约风险提醒】374票")
        self.assertEqual(analyze.classify_child(node), "消息")


class TestVerdict(unittest.TestCase):

    def test_chat_screen_passes(self):
        root = tree_of(CHAT_XML)
        nodes = list(analyze.walk(root))
        labeled = [n for n in nodes if n.label]
        rv = [n for n in nodes if "RecyclerView" in n.cls][0]
        lines, ok = analyze.verdict_of(labeled, rv, len(rv.children))
        self.assertTrue(ok)
        self.assertTrue(any("可行" in line for line in lines))

    def test_self_drawn_screen_fails(self):
        root = tree_of(SELF_DRAWN_XML)
        nodes = list(analyze.walk(root))
        labeled = [n for n in nodes if n.label]
        lines, ok = analyze.verdict_of(labeled, None, 0)
        self.assertFalse(ok)
        self.assertTrue(any("走不通" in line for line in lines))

    def test_text_but_no_container_is_inconclusive(self):
        lines, ok = analyze.verdict_of([analyze.Node(text="一些文本")], None, 0)
        self.assertFalse(ok)
        self.assertTrue(any("自绘" in line for line in lines))


class TestMain(unittest.TestCase):

    def run_on(self, xml: str) -> int:
        """跑一次 main()，把它的报告收进内存 —— 别把测试输出淹掉。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dd.xml"
            p.write_text(xml, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                code = analyze.main([str(p)])
            self.report = buf.getvalue()
            return code

    def test_exit_zero_when_feasible(self):
        self.assertEqual(self.run_on(CHAT_XML), 0)
        self.assertIn("可行", self.report)

    def test_exit_one_when_not_feasible(self):
        self.assertEqual(self.run_on(SELF_DRAWN_XML), 1)

    def test_exit_two_when_file_missing(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(analyze.main(["/definitely/not/here.xml"]), 2)

    def test_report_warns_about_separators(self):
        """分隔条提醒必须在报告里出现 —— 阶段 1 的实现者多半只看报告。"""
        self.run_on(CHAT_XML)
        self.assertIn("时间分隔", self.report)

    def test_other_package_is_visible(self):
        """dump 的时候抓了别的应用 —— 报告要能看出来，不能当成有效结论。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dd.xml"
            p.write_text(OTHER_APP_XML, encoding="utf-8")
            root = analyze.load(p)
        pkgs = {n.pkg for n in analyze.walk(root) if n.pkg}
        self.assertEqual(pkgs, {"com.android.settings"})
        self.assertNotIn(analyze.TARGET_PKG, pkgs)

    def test_report_flags_wrong_package(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dd.xml"
            p.write_text(OTHER_APP_XML, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                analyze.main([str(p)])
        self.assertIn("没有看到钉钉的包名", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
