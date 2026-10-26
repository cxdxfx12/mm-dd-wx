"""巡检怎么从「已经停在聊天页」走到「切回目标群」（2026-10-20 真机）。

真机复现出的失败链，一条一条都很反直觉：

  · 用户点按钮时，钉钉**通常就停在某个群里**（聊天页）。旧代码在聊天页里
    直接 `return null`（理由是「聊天页没有搜索入口」），于是永远不动作。
    实际路径是：聊天页 → 按返回 → 消息列表（右上角有搜索图标）→ 搜索 → 进群。

  · 搜索结果是**一行里两个命中点**：
        左边 `tv_friend_name`  → 进 ChatMsgActivity（会话，要的）
        右边 `group_tag_layout`（「猜你想找」标签）→ 开 CommonWebViewActivity
    点右边那个会开一个**空白网页**，肉眼看就是「卡住了 / 没反应」。
    实测同一行 x=300 进会话、x=610 进网页 —— 只差 310 像素。

  · 主界面的搜索入口是个**图标字体按钮**：text 是私有区乱码，
    只有 content-desc="搜索"，本身不可点，可点的是外面一层 FrameLayout。
    所以「只看 text」的匹配永远找不到它。

这些都不是靠读代码能想出来的，所以用断言锁住，改坏了要立刻红。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ANDROID = Path(__file__).resolve().parents[1] / "android"
SELFHEAL = ANDROID / "src/com/hbdxm/ddwx/SelfHeal.java"


class SelfHealSourceCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SELFHEAL.read_text(encoding="utf-8")

    def method(self, name: str, span: int = 3000) -> str:
        """粗略截出某个方法的源码段（够做字符串断言即可）。"""
        i = self.src.index(name)
        return self.src[i: i + span]


class TestChatPageBacksOut(SelfHealSourceCase):
    def test_chat_page_no_longer_gives_up(self):
        """旧代码：在聊天页就 return null 放弃。现在必须先退到消息列表。"""
        body = self.method("private static String searchAndOpen")
        self.assertNotIn(
            "在聊天页，没有搜索入口，放弃本轮", body,
            "聊天页里又直接放弃了 —— 用户停在群里点时按钮将毫无反应",
        )
        self.assertIn("backToMessageList", body, "聊天页里没有先退到消息列表")

    def test_back_limited_to_two_steps(self):
        """退多了会退到桌面，故障就从「找不到入口」变成「拉不起钉钉」。"""
        body = self.method("private static boolean backToMessageList", 1500)
        self.assertIn("i < 2", body, "退出的层数上限变了，可能一路退到桌面")

    def test_stops_when_leaving_dingtalk(self):
        body = self.method("private static boolean backToMessageList", 1500)
        self.assertIn("isDingTalkForeground", body,
                      "没检查前台是不是钉钉，退到桌面就找不到原因了")


class TestSearchEntryIcon(SelfHealSourceCase):
    def test_icon_entry_matched_by_content_desc(self):
        """图标字体按钮：text 是乱码，只有 content-desc 是「搜索」。"""
        body = self.method("private static AccessibilityNodeInfo findSearchEntry", 3000)
        self.assertIn("getContentDescription", body,
                      "没有认 content-desc 型的搜索图标，主界面上找不到入口")
        self.assertIn("isEmpty(n.getText())", body,
                      "没排除「文本就是搜索二字」的普通节点，会把会话行当入口")

    def test_icon_entry_limited_to_top_area(self):
        """只在屏幕顶部找：底部 Tab 栏也有「搜索」字样。"""
        body = self.method("private static AccessibilityNodeInfo findSearchEntry", 3000)
        self.assertIn("topLimit", body)


class TestResultRowHit(SelfHealSourceCase):
    # ⚠ 这两条原来查的是 searchAndOpen。结果行的判定后来被抽成了
    #   findResultRow（搜索结果异步渲染，要轮询重抓，逻辑不能留在循环里），
    #   断言还盯在老位置就会一直报「找不到」，而代码其实是对的
    #   （2026-10-20 就为这个白查了一轮）。查实现的**函数**，不查它的调用点。

    def test_prefers_conversation_item(self):
        body = self.method("private static String findResultRow")
        self.assertIn("tv_friend_name", body,
                      "没优先点会话条目，可能点到「猜你想找」标签开空白网页")

    def test_excludes_tag_zone(self):
        body = self.method("private static String findResultRow")
        self.assertIn("isInsideTag", body,
                      "没排除标签区 —— 点标签会开空白 WebView，用户看到的是「卡住」")

    def test_old_placement_is_gone(self):
        """searchAndOpen 里不该再有结果行匹配 —— 有的话就是两份逻辑，早晚走岔。"""
        body = self.method("private static String searchAndOpen", 2200)
        self.assertNotIn("stripTailParen", body,
                         "結果行匹配又回流到 searchAndOpen 了")

    def test_tag_detected_by_view_id(self):
        body = self.method("private static boolean isInsideTag", 900)
        self.assertIn("group_tag_layout", body,
                      "标签区判据应基于钉钉的 viewId，不是坐标（坐标随机型变）")


class TestTagZoneWouldTripTheOldCode(unittest.TestCase):
    """反向验证：老算法（取第一个可点祖先）在这份真机结构上会点到标签。

    只测「新算法对」是不够的 —— 样本失真时测试会静静永远通过。
    这里把真机那一行的结构抄下来跑两遍，要求**老算法出错、新算法正确**。
    """

    # (viewId, text, bounds, clickable)
    ROW = [
        ("search_young_group_item_pr", "", (38, 549, 1138, 749), True),
        ("ll_text_content", "", (264, 574, 1100, 724), False),
        ("tv_friend_name", "【数字质控】浙江杭州萧山区大江东公司(8)", (264, 574, 957, 649), False),
        ("group_tag_layout", "【数字质控】浙江杭州萧山区大江东公司(8)", (976, 585, 1100, 638), True),
    ]

    def test_old_pick_is_the_tag(self):
        """老算法 = 遍历到第一个同名节点就往上找最近可点祖先。"""
        hit = None
        for rid, text, bounds, clickable in self.ROW:
            if not text.startswith("【数字质控】"):
                continue
            # 往上找可点祖先：tv_friend_name 自己不可点 → 父 ll_text_content 也不可点
            # → 再往上是 search_young_group_item_pr（可点）
            if clickable:
                hit = rid
                break
        self.assertEqual(hit, "group_tag_layout",
                         "样本已失真：老算法在这份结构上不再出错，这条测试失去意义")

    def test_new_pick_is_the_conversation(self):
        """新算法 = 先认 tv_friend_name（且不在标签区里）。"""
        hit = None
        for rid, text, bounds, clickable in self.ROW:
            if not text.startswith("【数字质控】"):
                continue
            if rid == "tv_friend_name":
                hit = rid
                break
        self.assertEqual(hit, "tv_friend_name", "新算法没挑中会话条目")


if __name__ == "__main__":
    unittest.main()


class TestTailParenAndSearchBox(SelfHealSourceCase):
    """两个真机踩出的匹配坑（2026-10-20）。

    · 结果行文字是「群名(8)」——带成员数后缀。旧的 equals(want) 永远不等，
      watch_runs 连续十几轮全部「搜索里没找到」。
    · 全局搜索页的搜索框里写着**用户输入的群名**，位置比标题栏还高；
      旧 readTitle 把它当标题 → 假「已在目标群」。
    """

    def test_strips_member_count_suffix(self):
        # 同上：实现已移到 findResultRow，断言跟着实现走。
        body = self.method("private static String findResultRow")
        self.assertIn("stripTailParen", body, "没剥「(8)」后缀，精确匹配永远失败")

    def test_read_title_skips_search_box(self):
        body = self.method("static String readTitle", 2200)
        self.assertIn("search_src_text", body,
                      "readTitle 没跳过搜索框 —— 搜索页会被误判成「已在目标群」")
