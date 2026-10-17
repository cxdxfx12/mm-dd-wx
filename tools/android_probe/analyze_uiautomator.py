"""分析 `adb shell uiautomator dump` 出来的节点树 XML，判定钉钉安卓能不能被无障碍读。

为什么先跑这个
    这是整个「安卓无障碍」方案里唯一没被验证过的前提：**钉钉安卓客户端到底
    肯不肯把消息文本暴露给无障碍服务**。PC 端是有实物证据的（`tools/dingtalk_tree.txt`
    里消息区控件树为空，钉钉 PC 用自研 DUI 自绘），安卓端是原生 Android 视图
    体系、大概率能读，但「大概率」不等于「能」。这个脚本就是为了把它变成确定答案。

怎么用
    # 手机连上电脑、开 USB 调试，钉钉停在目标群里
    adb shell uiautomator dump /sdcard/dd.xml
    adb pull /sdcard/dd.xml
    python tools/android_probe/analyze_uiautomator.py dd.xml

判定说明（重要）
    uiautomator 看到的是**无障碍树的一个较保守视图**：
      · 它说「读得到」→ 用真正的 AccessibilityService 一定能读到；
      · 它说「读不到」→ 不代表一定读不到。AccessibilityService 可以加
        FLAG_INCLUDE_NOT_IMPORTANT_VIEWS / FLAG_RETRIEVE_INTERACTIVE_WINDOWS，
        能看到被标成「对无障碍不重要」的视图，范围比 uiautomator 大。
    所以这个脚本是**单向可信的筛子**：过了就放心往下做，没过才需要上真服务。
    另外 `uiautomator dump --compressed` 会把没有文本的节点整个删掉，别用那个。
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# 消息列表在这些容器里；钉钉安卓实测用的是 RecyclerView
SCROLL_CLASSES = ("RecyclerView", "ListView", "GridView", "ScrollView")

TARGET_PKG = "com.alibaba.android.rimet"

# 时间分隔符：钉钉安卓把「09:12 / 今天 08:13 / 昨天 / 星期一 / 10-16」这类分隔条
# 也做成消息列表的独立子节点。所以「一个子节点 = 一条消息」不能直接用。
# ⚠ 与 bridge/sources/android_parse.py 的同名正则保持一致（真机格式是「今天 08:13」）。
TIME_ONLY_RE = re.compile(
    r"^((今天|昨天|前天|大前天)\s*\d{1,2}:\d{2}|"
    r"(今天|昨天|前天|大前天)|"
    r"\d{1,2}:\d{2}(:\d{2})?|"
    r"星期[一二三四五六日天]|"
    r"\d{1,2}-\d{1,2}|\d{4}-\d{1,2}-\d{1,2})\s*$"
)

# 非文本消息的占位描述（图片/文件这类消息只有 content-desc，没有正文）。
# 注意用半角方括号 —— 机器人正文里的【】是全角，不会被误伤。
PLACEHOLDER_RE = re.compile(
    r"^\[(图片|视频|文件|语音|表情|动画表情|链接|位置|红包|转账)\]$"
)

LINE = "=" * 74


@dataclass
class Node:
    cls: str = ""
    text: str = ""
    desc: str = ""
    rid: str = ""
    pkg: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    scrollable: bool = False
    children: list["Node"] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.text or self.desc

    @property
    def short_class(self) -> str:
        return self.cls.rsplit(".", 1)[-1] if self.cls else "?"

    @property
    def short_id(self) -> str:
        return self.rid.rsplit("/", 1)[-1] if self.rid else ""


def parse_bounds(value: str) -> tuple[int, int, int, int]:
    m = BOUNDS_RE.search(value or "")
    if not m:
        return (0, 0, 0, 0)
    return tuple(int(x) for x in m.groups())          # type: ignore[return-value]


def build_tree(elem: ET.Element) -> Node:
    node = Node(
        cls=elem.get("class") or "",
        text=(elem.get("text") or "").strip(),
        desc=(elem.get("content-desc") or "").strip(),
        rid=elem.get("resource-id") or "",
        pkg=elem.get("package") or "",
        bounds=parse_bounds(elem.get("bounds") or ""),
        scrollable=(elem.get("scrollable") or "").lower() == "true",
    )
    for child in elem:
        if child.tag == "node":
            node.children.append(build_tree(child))
    return node


def walk(node: Node):
    yield node
    for child in node.children:
        yield from walk(child)


def subtree_text(node: Node, limit: int = 120) -> str:
    """把一个容器子树里的文本按出现顺序拼起来（用作「一条消息」的正文）。"""
    parts: list[str] = []
    for n in walk(node):
        if n.label:
            parts.append(n.label)
    text = " | ".join(parts)
    return text[:limit] + ("…" if len(text) > limit else "")


def classify_child(node: Node) -> str:
    """给消息列表的子节点分类：``消息`` / ``时间分隔`` / ``非文本``。

    阶段 1 必须按这个分类过滤 —— 直接「一个子节点 = 一条消息」会把
    「09:12」这种时间分隔条当成一条消息转发出去。
    """
    labels = [n.label for n in walk(node) if n.label]
    if not labels:
        return "非文本"
    if all(PLACEHOLDER_RE.match(x.strip()) for x in labels):
        return "非文本"          # 只有「[图片]」这类占位描述，没有实际正文
    if all(TIME_ONLY_RE.match(x.strip()) for x in labels):
        return "时间分隔"
    return "消息"


def pick_message_list(root: Node, containers: list[Node]) -> Node | None:
    """挑出消息列表 —— **直接复用正式解析器的那套规则**。

    为什么不再自己写一份：真机上「哪个才是消息列表」远没有看起来那么显然
    （底部功能条也是 RecyclerView；气泡里的横滑表格也是可滚动容器；而列表
    本身因为装着那个表格反而不算「最里层」）。自己写一份的结果是：这个报告
    说 A 是消息列表、解析预览说 B，人会以为解析器错了，去改一个没错的地方。
    """
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from bridge.sources.android_parse import Node as ParseNode
        from bridge.sources.android_parse import find_message_list
    except ImportError:                                   # pragma: no cover
        return None

    back: dict[int, Node] = {}

    def conv(n: Node) -> "ParseNode":
        p = ParseNode(cls=n.cls, text=n.text, desc=n.desc, rid=n.rid,
                      pkg=n.pkg, bounds=n.bounds, scrollable=n.scrollable)
        back[id(p)] = n
        p.children = [conv(c) for c in n.children]
        return p

    choice = find_message_list(conv(root))
    return back.get(id(choice)) if choice is not None else None


def load(path: Path) -> Node:
    root = ET.parse(path).getroot()
    for child in root:
        if child.tag == "node":
            return build_tree(child)
    raise SystemExit(f"没在 {path} 里找到 node 元素 —— 这个文件不像是 uiautomator dump 的结果")


def report(root: Node, top: int) -> int:
    nodes = list(walk(root))
    labeled = [n for n in nodes if n.label]
    pkgs = sorted({n.pkg for n in nodes if n.pkg})

    print(LINE)
    print(f"概览：节点 {len(nodes)} 个，其中带文本/描述的 {len(labeled)} 个")
    print(f"      涉及包名：{', '.join(pkgs) or '(无)'}")
    if TARGET_PKG not in pkgs:
        print(f"      ⚠ 没有看到钉钉的包名 {TARGET_PKG} —— "
              f"dump 的时候钉钉不在前台？这个结果不能作数")
    print(LINE)

    # ---- 消息容器：决定「能不能按一条消息分组」 ----
    containers = [n for n in nodes
                  if any(k in n.cls for k in SCROLL_CLASSES) or n.scrollable]
    print(f"可滚动容器（消息列表候选）：{len(containers)} 个")
    for c in containers:
        x1, y1, x2, y2 = c.bounds
        rows = [ch for ch in c.children if subtree_text(ch)]
        print(f"  · {c.short_class}"
              f"{'#' + c.short_id if c.short_id else ''}"
              f"  子节点 {len(c.children)} 个（其中有文本的 {len(rows)} 个）"
              f"  区域 {x2 - x1}×{y2 - y1} @({x1},{y1})")

    # 用解析器的规则挑，保证「结构判定」与「解析预览」说的是同一个容器
    best = pick_message_list(root, containers)
    best_rows = len([ch for ch in best.children if subtree_text(ch)]) if best else 0

    if best is not None and best_rows >= 2:
        print()
        print(f"  最可能的「消息列表」：{best.short_class}"
              f"{'#' + best.short_id if best.short_id else ''}")
        counts = {"消息": 0, "时间分隔": 0, "非文本": 0}
        for ch in best.children:
            counts[classify_child(ch)] += 1
        for i, ch in enumerate(best.children[:top]):
            print(f"    [{i}] {classify_child(ch):<4s} {ch.short_class:16s} "
                  f"{subtree_text(ch)}")
        if len(best.children) > top:
            print(f"    …（还有 {len(best.children) - top} 个子节点）")
        print()
        print(f"  子节点构成：消息 {counts['消息']} 个、时间分隔 {counts['时间分隔']} 个、"
              f"非文本 {counts['非文本']} 个")
        if counts["时间分隔"] or counts["非文本"]:
            print("  ⚠ 分隔条与图片也是这个列表的子节点 —— 阶段 1 不能简单地按"
                  "「一个子节点一条消息」采，")
            print("    要先按上面的分类过滤掉「时间分隔」和「非文本」，"
                  "否则会把「09:12」当消息转发出去。")
    print(LINE)

    # ---- 文本清单 ----
    print(f"文本节点（按出现顺序，前 {top} 个）：")
    for i, n in enumerate(labeled[:top]):
        print(f"  {i:>3} {n.short_class:18s} {n.label[:100]}")
    if len(labeled) > top:
        print(f"  …（还有 {len(labeled) - top} 个，用 --top 调大显示数量）")
    print(LINE)

    # ---- 判定 ----
    verdict, ok = verdict_of(labeled, best, best_rows)
    print("判定：")
    for line in verdict:
        print("  " + line)
    print(LINE)
    return 0 if ok else 1


def _is_ancestor_of(outer: Node, target: Node) -> bool:
    """``target`` 是否在 ``outer`` 的子树里（对象同一性比较，与文字内容无关）。"""
    return any(n is target for n in walk(outer) if n is not outer)


def verdict_of(labeled: list[Node], best: Node | None, rows: int) -> tuple[list[str], bool]:
    out: list[str] = []
    if not labeled:
        out.append("✗ 一个带文本的节点都没有 —— 这条路走不通，或者 dump 的时机不对。")
        out.append("  先排除时机问题：必须进到**具体群聊页**、看到消息气泡之后再 dump；")
        out.append("  停在会话列表页也会是这个结果。确认之后仍为空，才说明消息区是自绘的。")
        out.append("  终极确认：上真正的 AccessibilityService（加")
        out.append("  FLAG_INCLUDE_NOT_IMPORTANT_VIEWS）再看 —— 它的视野比 uiautomator 大。")
        return out, False

    long_texts = [n.label for n in labeled if len(n.label) >= 12]
    out.append(f"✓ 读到了 {len(labeled)} 个带文本的节点，其中长文本 {len(long_texts)} 个 —— "
               f"文本**在**无障碍树里。")
    if long_texts:
        out.append(f"  最长的一条：{max(labeled, key=lambda n: len(n.label)).label[:80]}")

    if best is not None and rows >= 2:
        out.append(f"✓ 找到消息列表容器（{best.short_class}，{rows} 个带文本的子节点）"
                   f"—— 消息边界是清楚的。")
        out.append("")
        out.append("→ 结论：**可行**。解析器已按这个形状实现"
                   "（bridge/sources/android_parse.py）；")
        out.append("  这一节与下面「解析预览」说的应当是同一个容器，对不上说明解析器该改了。")
        return out, True

    out.append("△ 有文本，但没找到「一条消息一个子节点」的容器。")
    out.append("  可能原因：dump 时钉钉停在会话列表而不是聊天页；或消息区被自绘。")
    out.append("  请进到具体群聊、看到消息气泡之后再 dump 一次。")
    return out, False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="分析 uiautomator dump，判定钉钉安卓能否无障碍读取")
    ap.add_argument("xml", type=Path, help="uiautomator dump 出来的 XML")
    ap.add_argument("--top", type=int, default=25, help="打印前多少个文本节点（默认 25）")
    args = ap.parse_args(argv)

    if not args.xml.is_file():
        print(f"找不到文件：{args.xml}", file=sys.stderr)
        return 2
    return report(load(args.xml), max(1, args.top))


if __name__ == "__main__":
    sys.exit(main())
