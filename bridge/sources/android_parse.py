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
TIME_ONLY_RE = re.compile(
    r"^(\d{1,2}:\d{2}(:\d{2})?|昨天.*|前天.*|星期[一二三四五六日]|"
    r"\d{1,2}-\d{1,2}|\d{4}-\d{1,2}-\d{1,2})$"
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
    warnings: list[str] = field(default_factory=list)

    @property
    def fps(self) -> list[str]:
        return [u.fp for u in self.units]


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


def _candidates(root: Node) -> list[Node]:
    return [n for n in walk(root)
            if n.scrollable or any(k in n.cls for k in SCROLL_CLASSES)]


def _is_inside(outer: Node, target: Node) -> bool:
    return any(n is target for n in walk(outer) if n is not outer)


def find_message_list(root: Node) -> Node | None:
    """挑出最像「消息列表」的容器：最里层、且带文本的子节点最多。

    外层 ScrollView 套内层 RecyclerView 很常见，所以先取最里层；同一层有多个
    候选时，取带文本子节点最多的那个（会话列表页的 RecyclerView 也在树里时，
    靠这一条把聊天区挑出来）。
    """
    cands = _candidates(root)
    if not cands:
        return None
    inner = [c for c in cands if not any(_is_inside(c, o) for o in cands if o is not c)]
    best: Node | None = None
    best_score = 0
    for c in (inner or cands):
        # 只算「真消息」的条数：时间分隔条每个列表都有，拿它凑数会让外面那层
        # 空壳容器胜出。
        score = sum(1 for ch in c.children if split_row(ch).kind in
                    ("text", "image", "file", "voice", "link", "unknown"))
        if score > best_score:
            best, best_score = c, score
    return best if best_score else None


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


def split_row(row: Node) -> Unit:
    """一个列表子节点 → 一个 :class:`Unit`（时间分隔条也会返回，kind="time"）。

    先切昵称、再判类型：只有切掉昵称之后才能正确认出「昵称 + [图片]」这种
    非文本消息 —— 否则整行会被当成 text，把 ``[图片]`` 当正文发出去。
    """
    parts = _leaves(row)
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
    struct = _split_by_structure(_unwrap(row))
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

    container = find_message_list(root)
    if container is None:
        frame.warnings.append("没找到消息列表容器（RecyclerView/可滚动区域）"
                              "—— 可能停在会话列表页，或消息区被自绘")
        return frame

    frame.container = container.short_class
    frame.rows = len(container.children)

    units: list[Unit] = []
    for row in container.children:
        unit = split_row(row)
        if unit.kind == "time":
            frame.separators.append(unit.content)
        elif unit.kind == "empty":
            continue
        else:
            units.append(unit)

    # 列表子节点的文档顺序一般就是上→下，但嵌套布局偶尔不保证；按 y 再排一次。
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
