"""安卓无障碍节点树 → 消息单元（`mode: "android"` 的阶段 1 解析器）。

输入是手机端 App 原样送上来的**一屏**节点树，格式与
``adb shell uiautomator dump`` 一致（``<hierarchy>`` + ``bounds="[l,t][r,b]"``）。
输出是「旧 → 新」排列的消息单元。

为什么解析放在电脑侧
    手机端是**故意做「笨」**的：界面一变就把这一屏节点树原样送出去，不解析、
    不判新老、不去重（见 ``android/README.md``）。好处有三条——改规则不用重装
    手机；「不转发历史」这套逻辑只有一份，与 OCR 路径共用同一条下游；手机端
    出错面只剩「读不到」和「网络不通」。

三层职责（读完这个模块就明白整条链路）
    1. :func:`parse_frame`   —— 一帧 → 若干 :class:`Unit`（纯函数，好测）
    2. :class:`FrameDiffer`  —— 帧序列 → 「这一帧新出现的几条」（基线 + 锚点）
    3. :func:`units_to_messages` —— Unit → :class:`Message`（交给下游）

为什么需要「基线 + 锚点」而不是「没见过就算新」
    群聊可以往上翻。若按「没见过 = 新」判，用户翻一次历史就会把几百条从没
    转发过的旧消息灌进微信群。所以第一帧只用来**建基线**（一条都不发），
    之后只认「上一帧最后一条消息之后」的部分——和 OCR 路径的
    ``pick_new_units`` 是同一个语义。

已知局限（阶段 1 刻意接受）
    · 锚点丢失（一屏全换了，比如换群、清缓存、长时间离线后一次性涌进大量
      消息）时**这一帧不发**，只把锚点重建到当前帧，并在 ``Diff.anchor_lost``
      里报告。宁可漏几条让人补发（``tools/forward_recent.py``），也不冒险把
      历史灌进群。
    · 同一个人连发两条一模一样的文字，只有第一条会被下游接受（指纹相同）。
      这与 OCR 路径的行为一致，最终闸门是 ``storage.is_duplicate``。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime

from ..fingerprint import unit_fp
from ..models import Message
from .base import infer_company

# ------------------------------------------------------------------ 形状常量

BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# 消息列表通常在这些容器里；钉钉安卓实测是 RecyclerView
SCROLL_CLASSES = ("RecyclerView", "ListView", "GridView", "ScrollView")

TARGET_PKG = "com.alibaba.android.rimet"

# 时间分隔条。钉钉安卓把它做成消息列表的**独立子节点**，直接「一个子节点
# 一条消息」会把「09:12」当消息转发出去 —— 阶段 1 必须按类型过滤掉。
# 真机上的格式是「今天 08:13」（实测），只有「今天」后面不带时间时才是孤零零的
# 三个字，所以这里刻意不写成 `今天.*` —— 那会把「今天还有1票…」这种正文也吃掉。
TIME_ONLY_RE = re.compile(
    r"^((今天|昨天|前天|大前天)\s*\d{1,2}:\d{2}|"
    r"(今天|昨天|前天|大前天)|"
    r"\d{1,2}:\d{2}(:\d{2})?|"
    r"星期[一二三四五六日天]|"
    r"\d{1,2}-\d{1,2}|\d{4}-\d{1,2}-\d{1,2})\s*$"
)

# 非文本消息的占位描述（图片/文件这类只有 content-desc，没有正文）。
# 用半角方括号 —— 机器人正文里的【】是全角，不会被误伤。
PLACEHOLDER_RE = re.compile(
    r"^\[(图片|视频|文件|语音|表情|动画表情|链接|位置|红包|转账)\]$"
)

# 发送者昵称的长度上限。真实昵称很少超过这个长度，而正文第一行经常更长；
# 用它把「昵称在上面、正文在下面」这两种角色的判定再做一道保险。
SENDER_MAX_LEN = 24
# 昵称与正文之间的垂直容差（像素）：布局里两行常有 0~4px 重叠，取 8 够用。
SENDER_GAP_TOL = 8

# 气泡里挂着的「操作按钮」不是消息内容。真机上它们是 ``android.view.View`` 的
# content-desc（长按气泡就出现「复制」），跟着正文发出去就是一句莫名其妙的噪音。
NOISE_LABELS = frozenset({
    "复制", "复制链接", "转发", "转发给", "撤回", "回复", "引用", "多选", "删除",
    "编辑", "收藏", "翻译", "提醒", "更多", "举报", "保存图片", "查看大图",
    "全屏", "下载", "识别图中二维码",
})

# 逐层往下的最大深度：防病态布局（自嵌套包装）把递归拖爆。
MAX_SPLIT_DEPTH = 6

# 气泡容器的 resource-id（真机上是语义化名字，不是混淆串）。用它认正文比
# 「面积最大」可靠得多 —— 行被滑到屏幕边界时行内 bounds 会整体塌掉。
# 仍然保留面积法兜底：某天 id 改了也不至于彻底读不到正文。
BUBBLE_RID_RE = re.compile(
    r"(chat_bubble_container|chatting_content_view_stub|card_view_container)$")

# 一行至少要有这么高才算「完整地露在屏幕上」。真机实测：被滑到列表顶部边界
# 之外的消息，行级 bounds 会缩成 38px 这种细条（内部 bounds 更是塌成负数）。
# 这种半截行不该被转发 —— 内容可能不全，而且它多半早就发过了。
MIN_ROW_HEIGHT = 60

# 昵称后面会跟一个单独的「机器人」标签（机器人账号发消息时）。库里存的昵称
# 不该带它 —— 指纹那边也会把尾部的「机器人」去掉，这里提前对齐，展示才一致。
ROBOT_TAG_RE = re.compile(r"\s+机器人$")

# ---------------------------------------------------------------- 页面类型
#
# 手机端是「界面变就上报」，用户点开 @ 面板、群信息、搜索成员、会话列表时
# 报上来的同样是**一个带很多文本的滚动列表**。这些人名/身份标签一旦被当成
# 群消息转发到微信群，就是实打实的内容外泄（真机第二帧就是这么来的：
# 「选择要@的人」面板被解析成 9 条消息）。所以在切分**之前**先判页面类型，
# 判不准就一帧都不解析，也不动基线。
#
# 正向信号：聊天页底部输入区的提示语。这条最稳 —— 别的页面没有输入框。
CHAT_HINTS = ("发消息或按住说话", "按住说话", "发送消息…", "发送消息...")
# 备用正向信号：底部工具条上的固定入口。提示语文案被改掉时还能认出来。
CHAT_TOOL_LABELS = frozenset({"表情", "图片", "更多"})
# 反向信号：这些文案只在非聊天页出现，命中即一票否决。
# 刻意只收**绝不会出现在聊天页**的字符串：
#   · 不收「群聊信息」—— 它在聊天页是右上角图标的 content-desc；
#   · 不收「搜索」「群管理」这类 —— 聊天页的菜单/图标也会带，
#     误杀聊天页的后果是「程序在跑但一条不转」，比误转更难发现。
# 用有序元组而不是集合：@ 面板同时含「选择要@的人」和「搜索名字或群身份」，
# 集合的遍历顺序不定，日志里报哪个就不定 —— 排障时人会被误导。
NON_CHAT_MARKERS = (
    "选择要@的人", "搜索名字或群身份", "发起群聊", "选择联系人",
)


class ParseError(ValueError):
    """节点树不是 uiautomator 的形状 —— 让调用方给用户一句人话。"""


# ------------------------------------------------------------------ 节点树

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
    def y0(self) -> int:
        return self.bounds[1]


def _parse_bounds(value: str) -> tuple[int, int, int, int]:
    m = BOUNDS_RE.search(value or "")
    if not m:
        return (0, 0, 0, 0)
    return tuple(int(x) for x in m.groups())            # type: ignore[return-value]


def _build(elem: ET.Element) -> Node:
    node = Node(
        cls=elem.get("class") or "",
        text=(elem.get("text") or "").strip(),
        desc=(elem.get("content-desc") or "").strip(),
        rid=elem.get("resource-id") or "",
        pkg=elem.get("package") or "",
        bounds=_parse_bounds(elem.get("bounds") or ""),
        scrollable=(elem.get("scrollable") or "").lower() == "true",
    )
    for child in elem:
        if child.tag == "node":
            node.children.append(_build(child))
    return node


def walk(node: Node):
    yield node
    for child in node.children:
        yield from walk(child)


def parse_xml(text: str) -> Node:
    """XML 文本 → 根节点。形状不对就抛 :class:`ParseError`。"""
    if not text or "<hierarchy" not in text[:2000]:
        raise ParseError("节点树里没有 <hierarchy> —— 这不是 uiautomator 格式的 XML")
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError as exc:
        raise ParseError(f"节点树不是合法 XML：{exc}") from exc
    for child in root:
        if child.tag == "node":
            return _build(child)
    raise ParseError("节点树里 <hierarchy> 下面没有 node")


# ------------------------------------------------------------------ 一帧 → 单元

@dataclass
class Unit:
    """一条消息（或一条带占位描述的非文本消息）。"""
    sender: str = ""
    content: str = ""
    kind: str = "text"                  # text | image | file | voice | …
    y0: int = 0
    y1: int = 0

    @property
    def fp(self) -> str:
        return unit_fp({"sender": self.sender, "content": self.content})

    def as_dict(self) -> dict:
        return {"sender": self.sender, "content": self.content,
                "kind": self.kind, "y0": self.y0, "y1": self.y1, "fp": self.fp}


KIND_OF_PLACEHOLDER = {
    "[图片]": "image", "[视频]": "video", "[文件]": "file",
    "[语音]": "voice", "[链接]": "link", "[位置]": "link",
    "[表情]": "image", "[动画表情]": "image",
    "[红包]": "unknown", "[转账]": "unknown",
}


@dataclass
class Frame:
    units: list[Unit] = field(default_factory=list)      # 旧 → 新
    separators: list[str] = field(default_factory=list)  # 时间分隔条（仅排障用）
    container: str = ""
    rows: int = 0                                        # 列表子节点总数
    texts: int = 0                                       # 全树的带文本节点数
    page: str = "chat"                                   # chat | other | unknown
    page_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def fps(self) -> list[str]:
        return [u.fp for u in self.units]

    @property
    def is_chat(self) -> bool:
        return self.page == "chat"


def detect_page(root: Node) -> tuple[str, str]:
    """判断这一屏是不是**聊天消息页**。返回 ``(kind, 依据)``。

    手机端「界面变就上报」，用户点开 @ 面板／群信息／搜索时，报上来的是一个
    塞满人名和身份标签的滚动列表 —— 结构与消息列表几乎一样。不先判页面，
    这些人名就会被当成群消息发进微信群（真机第二帧正是这种情况）。

    判定顺序：反向信号一票否决 → 正向信号 → 兜底 ``unknown``（一律按「不是
    聊天页」处理，即不解析、不动基线）。
    """
    labels = [n.label for n in walk(root) if n.label]
    joined = "\n".join(labels)

    for marker in NON_CHAT_MARKERS:
        if marker in joined:
            return "other", f"页面上出现「{marker}」"

    for hint in CHAT_HINTS:
        if hint in joined:
            return "chat", f"输入区提示语「{hint}」"

    if CHAT_TOOL_LABELS.issubset(set(labels)):
        return "chat", "底部工具条齐全（表情/图片/更多）"

    return "unknown", "既没有输入区提示语，也没有已识别的非聊天页文案"


def _leaves(node: Node) -> list[Node]:
    """带文本的**叶子**节点，按文档顺序。

    只取叶子是为了避免重复：容器自己也可能带 content-desc（整条消息的合并
    描述），连同子节点一起收就会把同一句话算两遍。
    """
    out: list[Node] = []

    def rec(n: Node) -> None:
        labeled_children = [c for c in n.children if any(x.label for x in walk(c))]
        if n.label and not labeled_children:
            out.append(n)
            return
        for c in n.children:
            rec(c)

    rec(node)
    return out


def _clean_leaves(node: Node) -> list[Node]:
    """去掉「不是消息内容」的叶子：气泡上的操作按钮、右侧隐藏时间戳。

    真机上这两类会把正文弄脏：
      · 气泡里挂着一个 ``android.view.View``，content-desc=``复制``；
      · 每条消息右侧有个**零宽**的 ``chatting_item_hidden_time``（``08:13``），
        当正文发出去就是一句莫名其妙的时间。
    """
    out: list[Node] = []
    for n in _leaves(node):
        lab = n.label
        if not lab or lab in NOISE_LABELS:
            continue
        left, _, right, _ = n.bounds
        if right - left <= 0 and TIME_ONLY_RE.match(lab):
            continue
        out.append(n)
    return out


def _area(node: Node) -> int:
    left, top, right, bottom = node.bounds
    return max(0, right - left) * max(0, bottom - top)


def _candidates(root: Node) -> list[Node]:
    return [n for n in walk(root)
            if n.scrollable or any(k in n.cls for k in SCROLL_CLASSES)]


def _is_inside(outer: Node, target: Node) -> bool:
    return any(n is target for n in walk(outer) if n is not outer)


def find_message_list(root: Node) -> Node | None:
    """挑出「消息列表」容器。

    真机实测（Mate Xs 2 / 钉钉 7.x，见 ``android/README.md``）：聊天区是
    ``ListView``，但它**不是**树里唯一的可滚动容器 —— 底部功能条
    （``RecyclerView#native_menu_bar``）、气泡里的横滑表格
    （``HorizontalScrollView``）也都是。所以不能只看「谁更靠里」。

    两步：
      1. 先只在**没有被别的候选包住**的候选里挑，取「能切出的消息条数最多、
         面积最大」的那个（会话列表页/底部功能条都在树里时，这一条把它们挡掉）；
      2. 再往里钻一层：外层 ScrollView 套内层 RecyclerView 是常见写法，若内层
         装了外层一半以上的文本，说明外层只是个壳 → 换成内层。
         阈值取「一半」是因为气泡里的横滑表格也满足「更靠里」，但它只有几条
         文本，占不到整屏的一半。
    """
    cands = _candidates(root)
    if not cands:
        return None

    # 先按面积筛一道：消息列表基本占满屏幕，而底部功能条、气泡里的横滑表格
    # 都只是窄窄一截。真机实测这一条最有效 —— 功能条同样是 RecyclerView，
    # 它下面有 4 个带文本的子节点，按「条数」比它反而赢过只有 2 组消息的列表。
    root_area = _area(root) or 1
    big = [c for c in cands if _area(c) * 100 >= root_area * 12]
    cands = big or cands

    def units_of(n: Node) -> list[Unit]:
        return _container_units(n)

    top = [c for c in cands if not any(_is_inside(o, c) for o in cands if o is not c)]
    best = max(top or cands, key=lambda c: (len(units_of(c)), _area(c)))

    for _ in range(4):
        outer_leaves = len(_leaves(best))
        inner = [c for c in cands
                 if _is_inside(best, c)
                 and not any(_is_inside(c, o) for o in cands
                             if o is not c and _is_inside(best, o))]
        inner = [c for c in inner
                 if units_of(c) and len(_leaves(c)) * 2 >= max(1, outer_leaves)]
        if not inner:
            break
        best = max(inner, key=lambda c: (len(_leaves(c)), _area(c)))
    return best if units_of(best) else None


def _labeled_children(node: Node) -> list[Node]:
    return [c for c in node.children if any(x.label for x in walk(c))]


def _unwrap(node: Node, depth: int = 3) -> Node:
    """穿过「只有一个带文本子节点」的包装层，落到真正有内容的那一层。

    真实行结构常常是 ``行 > 内容包装 > [昵称, 气泡]``，直接看行的子节点只能
    看到一个包装容器，结构信号就丢了。
    """
    cur = node
    for _ in range(depth):
        kids = _labeled_children(cur)
        if len(kids) == 1 and _labeled_children(kids[0]):
            cur = kids[0]
        else:
            break
    return cur


def _split_by_structure(row: Node) -> tuple[str, list[Node]] | None:
    """按视图结构切昵称：第一块是**叶子**、第二块是**容器**（气泡）时才成立。

    这比「谁更短」可靠得多 —— 正文可能只有「374票」，比昵称还短。真实布局里
    昵称是贴着行的 TextView，正文在包起来的气泡容器里。
    """
    kids = _labeled_children(row)
    if len(kids) < 2:
        return None
    first, second = kids[0], kids[1]
    if _labeled_children(first):                # 第一块自己是容器，不像昵称
        return None
    if not _labeled_children(second):           # 第二块也是叶子 → 没有结构信号
        return None
    name = first.label
    if not name or len(name) > SENDER_MAX_LEN:
        return None
    if first.bounds[3] > second.bounds[1] + SENDER_GAP_TOL:
        return None
    return name, _leaves(second)


def _looks_like_sender(first: Node, second: Node) -> bool:
    """没有结构信号时的**保守**判据：第一行是昵称、第二行是正文？

    信号是「**在正上方** + 明显更短」。两条同时成立才算 —— 宁可把昵称留在
    正文里（只是难看），也不能把正文第一行当昵称作废（那是真丢内容）。

    例外：正文是 ``[图片]`` 这类占位符时，昵称几乎总是更长，不能只按长度比
    —— 否则「昵称 + 图片」这一行会被拼成 ``昵称\\n[图片]``，昵称混进正文里。
    """
    name = first.label
    if not name or len(name) > SENDER_MAX_LEN:
        return False
    if first.bounds[3] > second.bounds[1] + SENDER_GAP_TOL:
        return False
    return len(name) < len(second.label) or bool(PLACEHOLDER_RE.match(second.label))


def _is_time_strip(node: Node) -> bool:
    """是时间分隔条吗（``今天 08:13`` / ``星期一`` / ``10-16``）。

    真机上「时间分隔条」是列表子节点里的一个独立包装层（``layout_chatting_split``），
    而每条消息右侧还有一个**零宽**的 ``chatting_item_hidden_time``（同样的时间
    文本，宽度 0，给读屏用的）。只看文本会把后者也当成分隔条 —— 于是整条消息
    被误判成「一组」，正文被拆散。零宽的那条在这里被挡掉。
    """
    leaves = _leaves(node)
    if not leaves:
        return False
    if not all(TIME_ONLY_RE.match(x.label) for x in leaves):
        return False
    left, top, right, bottom = node.bounds
    return right - left > 0 and bottom - top != 0


def _looks_like_msgrow(node: Node) -> bool:
    """这一层像不像「一条消息」（昵称/头像 + 一个装了多行文本的气泡）。"""
    kids = _labeled_children(node)
    if len(kids) < 2:
        return False
    return any(len(_labeled_children(k)) >= 2 and _clean_leaves(k) for k in kids)


def _one(node: Node) -> list[Unit]:
    unit = split_row(node)
    if unit.kind == "empty":
        return []
    # 只露出一截的消息行直接丢掉。真机上消息被滑到列表顶部边界外时，行级
    # bounds 会缩成 38px 这种细条、行内 bounds 全部塌成负数 —— 这时切出来的
    # 正文是错的（昵称被当成正文，实测「签收跟单专员/机器人」），而它多半
    # 早就发过了。宁可漏一条（`forward_recent.py` 能补），也不发错内容。
    #
    # 时间分隔条不适用：它只是个标记，本身就可能只有 40px 高；真丢了也只是
    # 排障信息少一行，不影响转发。
    if unit.kind != "time" and node.bounds[3] - node.bounds[1] < MIN_ROW_HEIGHT:
        return []
    return [unit]


def _item_units(item: Node, depth: int = 0) -> list[Unit]:
    """列表的一个子节点 → 0..N 个 Unit。

    **真机上 ListView 的子节点不是「一条消息」**，而是「时间分隔条 + 若干条
    消息」的一组（实测结构：``LinearLayout`` › ``layout_chatting_split`` +
    若干 ``RelativeLayout``）。把它当一条的后果不是报错，而是整屏消息被拼成
    一条、时间条混进正文里 —— 所以这里要能往下走两层。
    """
    # 半截行在 `_one` 里丢；时间分隔条不受高度限制 —— 它只是个标记，
    # 而真机上时间条本身就可能只有 40px 高。
    kids = _labeled_children(item)
    if not kids:
        return _one(item)

    times = [k for k in kids if _is_time_strip(k)]
    if times and depth < MAX_SPLIT_DEPTH:
        out: list[Unit] = []
        for k in times:
            _, y0, _, y1 = k.bounds
            out.append(Unit(kind="time", content=_leaves(k)[0].label,
                            y0=y0, y1=y1))
        for k in kids:
            if any(k is t for t in times):
                continue
            out.extend(_item_units(k, depth + 1) if _labeled_children(k)
                       else _one(k))
        return out

    # 一个包装层里塞了多条消息（RecyclerView 套 RecyclerView 这类）
    if depth < MAX_SPLIT_DEPTH and sum(1 for k in kids
                                       if _looks_like_msgrow(k)) >= 2:
        out = []
        for k in kids:
            out.extend(_item_units(k, depth + 1) if _labeled_children(k)
                       else _one(k))
        return out
    return _one(item)


def _container_units(container: Node) -> list[Unit]:
    out: list[Unit] = []
    for row in container.children:
        out.extend(_item_units(row))
    return out


def _strip_robot_tag(name: str) -> str:
    return ROBOT_TAG_RE.sub("", name or "").strip()


def _bubble_and_sender(node: Node) -> tuple[Node | None, str]:
    """在一条消息行里分出「气泡（正文）」与「昵称」。

    真机结构：``RelativeLayout`` › ``[头像FrameLayout(content-desc=昵称)``,
    ``CheckBox``, ``ViewGroup[昵称, 机器人]``, ``气泡FrameLayout``, ``零宽时间]``。
    昵称取气泡**之前**第一个像名字的块，正文取气泡里的全部文本 —— 这样头像的
    content-desc 与「机器人」标签都不会混进正文。
    """
    kids = _labeled_children(node)
    bubble: Node | None = None

    # 先按 resource-id 认气泡。这比「面积最大」可靠得多：消息行被滑到屏幕边界
    # 只露出一截时，行内所有子节点的 bounds 都会塌掉（top 被夹成列表顶部、
    # bottom 变成负数），此时昵称块的「面积」会算得比气泡还大，正文就成了
    # 「昵称 / 机器人」——真机实测过（08:42 那条只露 38px）。
    for k in kids:
        if BUBBLE_RID_RE.search(k.rid):
            bubble = k
            break

    if bubble is None:
        # 退回「面积最大」：id 被改掉或这颗树里没带 id 时的兜底。
        # 不能只按「文本行数」比：只有一行的消息（正文两三个字）与「昵称 +
        # 机器人标签」那个小块的文本数一样多，按行数会挑到昵称块。
        best: tuple[int, int] = (-1, -1)
        for k in kids:
            if not _labeled_children(k):
                continue
            key = (_area(k), len(_clean_leaves(k)))
            if key > best:
                bubble, best = k, key
    if bubble is None:
        return None, ""

    sender = ""
    for k in kids:
        if k is bubble:
            break
        if _labeled_children(k):
            leaves = _clean_leaves(k)
            cand = leaves[0].label if leaves else ""
        else:
            cand = k.label
        cand = _strip_robot_tag(cand)
        if (cand and len(cand) <= SENDER_MAX_LEN
                and not PLACEHOLDER_RE.match(cand) and cand not in NOISE_LABELS
                and not TIME_ONLY_RE.match(cand)):
            sender = cand
            break
    return bubble, sender


def split_row(row: Node) -> Unit:
    """一个「消息行」→ 一个 :class:`Unit`（时间分隔条也会返回，kind="time"）。

    先切昵称、再判类型：只有切掉昵称之后才能正确认出「昵称 + [图片]」这种
    非文本消息 —— 否则整行会被当成 text，把 ``[图片]`` 当正文发出去。
    """
    parts = _clean_leaves(row)
    # bounds 是 (left, top, right, bottom)，别按 (x0, y0, x1, y1) 解 ——
    # 解错的后果不是报错，而是「旧→新」排序全乱，现场表现成转发顺序错乱。
    _, y0, _, y1 = row.bounds
    if not parts:
        return Unit(kind="empty", y0=y0, y1=y1)

    labels = [p.label for p in parts]
    if all(TIME_ONLY_RE.match(x) for x in labels):
        return Unit(kind="time", content=labels[0], y0=y0, y1=y1)

    sender = ""
    body = labels
    node = _unwrap(row)
    bubble, name = _bubble_and_sender(node)
    if bubble is not None:
        inner = [p.label for p in _clean_leaves(bubble)]
        if inner:
            sender, body = name, inner
    else:
        struct = _split_by_structure(node)
        if struct is not None:
            sender, body = struct[0], [p.label for p in struct[1]]
        elif len(parts) >= 2 and _looks_like_sender(parts[0], parts[1]):
            sender, body = labels[0], labels[1:]

    if all(PLACEHOLDER_RE.match(x) for x in body):
        return Unit(sender=sender, content=body[0],
                    kind=KIND_OF_PLACEHOLDER.get(body[0], "unknown"),
                    y0=y0, y1=y1)
    return Unit(sender=sender, content="\n".join(body), kind="text", y0=y0, y1=y1)


def parse_frame(text: str) -> Frame:
    """一屏节点树 → :class:`Frame`（units 已按 y 排成「旧 → 新」）。"""
    root = parse_xml(text)
    frame = Frame()

    all_nodes = list(walk(root))
    frame.texts = sum(1 for n in all_nodes if n.label)
    pkgs = {n.pkg for n in all_nodes if n.pkg}
    if TARGET_PKG not in pkgs:
        frame.warnings.append(
            f"这棵树里没有钉钉的包名 {TARGET_PKG}（看到的是 "
            f"{'、'.join(sorted(pkgs)) or '无'}）—— 手机端当时可能不在钉钉里")

    # 先判页面，再切消息：@面板/群信息这些页面同样是「带很多文本的滚动列表」，
    # 不判就会把人名、身份标签当成群消息原样发出去。
    frame.page, frame.page_reason = detect_page(root)
    if not frame.is_chat:
        frame.warnings.append(f"当前不是聊天页（{frame.page_reason}）——本帧不解析")
        return frame

    container = find_message_list(root)
    if container is None:
        frame.warnings.append("没找到消息列表容器（RecyclerView/可滚动区域）"
                              "—— 可能停在会话列表页，或消息区被自绘")
        return frame

    frame.container = container.short_class
    frame.rows = len(container.children)

    units: list[Unit] = []
    for unit in _container_units(container):
        if unit.kind == "time":
            frame.separators.append(unit.content)
        else:
            units.append(unit)

    # 列表子节点的文档顺序一般就是上→下，但嵌套布局偶尔不保证；按 y 再排一次。
    # （行级 bounds 在真机上是准的；只有深层子节点的 bounds 会失真，见
    # `_is_time_strip` 的说明。）
    units.sort(key=lambda u: u.y0)
    frame.units = units
    return frame


# ------------------------------------------------------------------ 帧序列 → 新消息

@dataclass
class Diff:
    """一帧的判定结果。"""
    units: list[Unit] = field(default_factory=list)
    baseline: bool = False        # 这一帧被当作基线（一条都不发）
    anchor_lost: bool = False     # 上一帧的锚点在本帧里找不到

    @property
    def count(self) -> int:
        return len(self.units)


class FrameDiffer:
    """把「一屏一屏」的节点树变成「这一屏新增了哪几条」。

    只有一套规则：**上一帧最后若干条消息 = 锚点，本帧只取锚点之后的部分。**
    第一帧没有锚点，用来建基线，一条都不发 —— 这就是「不转发历史」。
    """

    def __init__(self, tail: int = 20):
        self.tail = max(1, tail)
        self._anchor: list[str] = []
        self.baseline_built = False
        self.frames = 0

    # --- 对外 ---

    def feed(self, units: list[Unit]) -> Diff:
        self.frames += 1
        if not units:
            # 读空的一帧（切到别的 App、锁屏、节点树还没渲染完）。**什么都不做**：
            # 既不建基线也不动锚点。若在这里「记住空帧」，锚点会被清成空列表，
            # 之后每一帧都判成锚点丢失 —— 表现为程序在跑但一条都不转发。
            return Diff()
        if not self.baseline_built:
            self._remember(units)
            return Diff(baseline=True)

        idx = self._locate(units)
        if idx < 0:
            # 锚点没了（换群、清缓存、一屏全换）。这一帧不发，把锚点重建到
            # 当前帧 —— 不重建的话锚点会永久失效，之后每一帧都发不出去。
            self._remember(units)
            return Diff(anchor_lost=True)

        new = units[idx + 1:]
        self._remember(units)
        return Diff(units=new)

    def reset(self) -> None:
        """换群/重启采集时清空基线（下一帧重新建基线，不会补发历史）。"""
        self._anchor = []
        self.baseline_built = False

    # --- 内部 ---

    def _remember(self, units: list[Unit]) -> None:
        self._anchor = [u.fp for u in units[-self.tail:]]
        self.baseline_built = True

    def _locate(self, units: list[Unit]) -> int:
        """在 ``units`` 里找锚点（从尾部往前找，取最靠后的匹配）。"""
        if not units:
            return -1
        pos = {fp: i for i, fp in enumerate(u.fp for u in units)}   # 后出现的覆盖前面的
        for fp in reversed(self._anchor):
            if fp in pos:
                return pos[fp]
        return -1


# ------------------------------------------------------------------ 单元 → 消息

def units_to_messages(units: list[Unit], *, chat_name: str,
                      companies: list[str] | None = None,
                      now: datetime | None = None) -> list[Message]:
    """Unit 列表 → :class:`Message` 列表。

    ``raw_id`` 用 ``{指纹}:{同批内第几次}``，与网关 ``/messages`` 那条路径同一
    口径：同一条消息重传会得到同一个 uid，被下游 ``is_duplicate`` 挡掉。
    """
    now = now or datetime.now()
    companies = list(companies or [])
    seen: dict[str, int] = {}
    out: list[Message] = []
    for unit in units:
        fp = unit.fp
        seq = seen.get(fp, 0) + 1
        seen[fp] = seq
        out.append(Message(
            source="android",
            chat_name=chat_name,
            sender_name=unit.sender,
            company=infer_company(companies, chat_name, unit.sender, unit.content),
            content=unit.content,
            msg_type=unit.kind if unit.kind in ("text", "image", "file", "voice",
                                                "link") else "unknown",
            msg_time=now,
            raw_id=f"{fp}:{seq}",
        ))
    return out
