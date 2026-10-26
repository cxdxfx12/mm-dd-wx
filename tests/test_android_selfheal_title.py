"""巡检读标题栏：content-desc 会劫持群名（2026-10-20 真机事故）。

## 现场

手机钉钉**明明停在目标群**，巡检状态却每一轮都写：

    15:09:17 · 定时 · 失败：搜索里没找到「【数字质控】浙江杭州萧山区大江东公司」，已放弃（不猜）

`watch_runs` 已经 27 —— 也就是说它每 10 分钟真地动一次界面，动了 27 次，
一次都没成功，而且手机上的群从头到尾没变过。

## 根因

`readTitle()` 的规则是「屏幕上部 18% 内、最靠上的非空文本」。
上一版取文本用的是 `textOf()`，而它会**在 text 为空时退回 content-desc**。
真机这一帧里（`data/screen-20261020_150912.xml`）：

    y=115  content-desc="发起钉钉电话"      ← 标题栏右侧的图标按钮
    y=115  content-desc="群聊信息"          ← 同上
    y=143  text="【数字质控】浙江杭州萧山区大江东公司"   ← 真正的群名

图标按钮的 content-desc 比群名**高 28 像素**，于是每轮都读到「发起钉钉电话」，
判定成「不在目标群」→ 去搜索 → 聊天页里没有搜索入口 → 报「搜索里没找到」。

## 修法

`readTitle()` 只认 `getText()`，不再退回 content-desc。

下面用 Python 复刻两版算法，直接拿那一帧真机样本跑：
旧算法必须**复现**出错误标题（证明这份样本确实能触发它），
新算法必须读出群名。只写「新算法对」是不够的 —— 那样样本一旦失真，
测试会静静地永远通过。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

FRAME = (Path(__file__).resolve().parents[1] / "data"
         / "screen-20261020_150912.xml")

WANT_GROUP = "【数字质控】浙江杭州萧山区大江东公司"
BAD_TITLE = "发起钉钉电话"


def nodes(text: str):
    """把 uiautomator XML 摊平成 (text, content_desc, top, left, right, bottom)。"""
    out = []
    for m in re.finditer(r"<node[^>]*>", text):
        tag = m.group(0)

        def attr(name: str) -> str:
            mm = re.search(name + r'="([^"]*)"', tag)
            return mm.group(1) if mm else ""

        b = re.search(r'bounds="\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]"', tag)
        if not b:
            continue
        out.append((attr("text"), attr("content-desc"),
                    int(b.group(2)), int(b.group(1)),
                    int(b.group(3)), int(b.group(4))))
    return out


def is_noise(t: str) -> bool:
    s = t.strip()
    if not s:
        return True
    c0 = s[0]
    # Java 的运算符优先级：(a || b || c || (d && e))，这里照抄
    if c0 in "([" or c0 in "（" or (c0 == "【" and len(s) <= 4):
        return True
    if re.fullmatch(r"\d+", s):
        return True
    return len(s) == 1 and 0xE000 <= ord(c0) <= 0xF8FF


def read_title(items, fallback_desc: bool, height: int = 2480):
    """复刻 Java 的 readTitle。fallback_desc 控制是否退回 content-desc。"""
    limit = int(height * 0.18)
    best, best_top = None, 1 << 30
    for text, desc, top, _l, _r, _b in items:
        t = text if text else (desc if fallback_desc else "")
        if not t or is_noise(t):
            continue
        if top < 0 or top > limit:
            continue
        if top < best_top:
            best_top, best = top, t
    return best


@unittest.skipUnless(FRAME.exists(), "缺少真机样本")
class TestReadTitleNotHijackedByContentDesc(unittest.TestCase):
    """旧算法必须复现事故，新算法必须读出群名。"""

    @classmethod
    def setUpClass(cls):
        cls.items = nodes(FRAME.read_text(encoding="utf-8"))

    def test_sample_really_contains_the_group_title(self):
        """先确认样本没变质：群名就在标题区。"""
        assert any(t == WANT_GROUP for t, _d, *_ in self.items), \
            "样本里找不到群名，测试失去意义"

    def test_old_algorithm_reproduces_the_bug(self):
        """旧算法（text 空则退回 content-desc）必须读出按钮名。"""
        old = read_title(self.items, fallback_desc=True)
        self.assertEqual(BAD_TITLE, old,
                         "这份样本没能复现事故 —— 说明它已经不是现场那一帧了")

    def test_new_algorithm_reads_the_group(self):
        new = read_title(self.items, fallback_desc=False)
        self.assertEqual(WANT_GROUP, new)


class TestSourceOnlyUsesText(unittest.TestCase):
    """源码约束：readTitle 里不许再出现 textOf（它带 content-desc 回退）。"""

    def setUp(self):
        self.src = (Path(__file__).resolve().parents[1] / "android" / "src"
                    / "com" / "hbdxm" / "ddwx" / "SelfHeal.java").read_text(
                        encoding="utf-8")

    def test_read_title_uses_get_text(self):
        body = self.src.split("static String readTitle", 1)[1]
        body = body.split("private static String blockingPage", 1)[0]
        self.assertIn("getText()", body)
        self.assertNotIn("textOf(", body,
                         "readTitle 退回 content-desc 会被标题栏图标按钮劫持")

    def test_chat_page_guard_exists(self):
        """聊天页里点「搜索入口」只会点到输入框，必须提前放弃。"""
        self.assertIn("inChatPage(", self.src)
        body = self.src.split("private static String searchAndOpen", 1)[1]
        body = body.split("// ② 写关键词", 1)[0]
        self.assertIn("inChatPage(svc)", body)

    def test_chat_page_guard_knows_muted_group(self):
        """禁言群的输入框提示语是「禁言中」，不是「发消息」。

        2026-10-20 20:54 真机：用户故意切进禁言群点「切到钉钉群」，
        inChatPage 只认「发消息/按住说话」→ 返回 false → 在聊天页里找
        搜索入口（根本不存在）→ 报「搜索里没找到」。这是回归红线。
        """
        body = self.src.split("private static boolean inChatPage", 1)[1]
        body = body.split("private static AccessibilityNodeInfo", 1)[0]
        self.assertIn("禁言", body,
                      "inChatPage 不认禁言提示语，禁言群里切群必失败")


if __name__ == "__main__":
    unittest.main()
