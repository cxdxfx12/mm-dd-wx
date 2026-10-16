"""钉钉截屏 + OCR 采集端（mode: ocr）。

适用场景
    钉钉机器人只能收到 @ 它的消息，拿不到群内全部对话；钉钉 PC 端又是
    Qt 自绘（实测消息内容不进 UIAutomation 控件树，`ChatBubbleWidget`
    下没有任何子节点）。要转发群里的**所有**消息，唯一不依赖管理员
    审批的办法就是把消息区截下来做 OCR。

工作原理
    1. UIAutomation 定位钉钉主窗口与消息滚动区（`im_chat::ChatBubbleWidget`，
       实测该矩形正好是纯消息区，不含工具栏和输入框）；
    2. 用 PrintWindow(PW_RENDERFULLCONTENT) 抓窗口位图——**不抢焦点**，
       窗口被遮挡也能截（最小化除外）；
    3. 裁出消息区，1.5x 放大后送 RapidOCR（PP-OCR 离线模型，中文实测可靠）；
    4. 按像素颜色给每个文本块分类：
         深黑(均值<70)=正文   中灰(90~145)+靠左=发送者   中灰+居中=时间戳
         浅灰(>150)=元数据(丢弃)   橙红/蓝色链接=横幅按钮(丢弃)/正文
    5. 同一水平线上的块合并为一行，行距大于阈值的多行归为一条消息；
    6. 帧间用多重集合差分发现新消息——重复内容、漏帧都不出错。

安全护栏
    每帧先 OCR 窗口顶部标题条，与配置的 `dingtalk.chat_names` 做模糊匹配，
    不匹配（比如钉钉里开着别的会话）就**拒绝转发**这一帧——保证只有
    指定群的内容会被送出去。

已知局限（务必了解）
    - 钉钉窗口最小化时截不到内容（PrintWindow 对最小化窗口返回旧帧）；
    - OCR 不是 100% 精确，长数字可能出错（已做数字内空格修复）；
    - 两条轮询之间被滚出屏幕的消息拿不到（轮询间隔越短越可靠）；
    - 图片/文件只能转发文字描述（文件名、大小），拿不到文件本体。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import difflib
import hashlib
import logging
import re
import sys
import threading
import time
from collections import Counter, deque
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from ..models import Message
from .base import Source, infer_company

log = logging.getLogger("bridge.source.ocr")

# ---- Win32（PrintWindow 截屏）----
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0


class _BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class _BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", _BMIH), ("bmiColors", wt.DWORD * 3)]


# ---- 钉钉窗口常量（实测于钉钉 PC 客户端）----
CLIENT_CLASS_CANDIDATES = ("StandardFrame_DingTalk", "DingTalkPCMainWindow")
MSG_AREA_CLASS = "im_chat::ChatBubbleWidget"     # 消息滚动区（不含工具栏/输入框）
CHAT_BOX_CLASS = "im_chat::DTIMChatBox"          # 聊天面板兜底

# ---- 解析参数（实测标定，见 tools/probe_dingtalk_capture.py 输出）----
OCR_SCALE = 1.5                 # 消息区放大倍数：1.0 会长数字偶尔断行，2.0 更慢
LINE_OVERLAP = 0.5              # 垂直重叠超过该比例的两块视为同一行
UNIT_GAP = 55                   # 行间距超过该像素视为新消息（实测行距 18~41，气泡间 80+）
CONTENT_LUM_MAX = 70            # 文字均值低于此 = 正文（实测 27）
SENDER_LUM = (88, 148)          # 中灰区间 = 发送者名/时间戳（实测 113）
SELF_LEFT_RATIO = 0.45          # 消息左缘超过宽度的该比例 = 自己发的
TIME_CENTER_TOL = 0.13          # 时间戳允许的水平中心偏移比例
MIN_CONF = 0.35                 # OCR 置信度下限

# ---- 视口滚动状态（判断「视口是否贴底」）----
# 实测钉钉 PC 的消息区里有一个 ddesign::SmoothScrollBar，通过
# RangeValuePattern 暴露精确的滚动偏移（负值，单位 px）：
#     贴底      Value == Maximum          （实测 -488 / -488）
#     向上滚后  Value <  Maximum          （实测 -1088 / -488）
# 这是判断「屏幕上是不是最新消息」的确定性信号 —— 比帧间内容重叠可靠得多，
# 因为消息很高时（本群以表格为主，一屏只装 1~2 条）新消息一来上一条会被
# 整条顶出视口，两帧内容零重叠。
SCROLLBAR_CLASS = "ddesign::SmoothScrollBar"
AT_BOTTOM_TOL = 2.0             # px：Value 与 Maximum 差小于它即视为贴底

# ---- 锚点模糊容错的参数 ----
ANCHOR_SIM = 0.75               # 内容相似度下限（容忍 OCR 错字/漏字）
SENDER_MISMATCH_PENALTY = 0.9   # 发送者也对不上时的相似度折扣（发送者同样会被 OCR 认错）
FUZZY_MAX_LEN = 4000            # 超过这个长度不做模糊比对（性能保护）
RECENT_FRAMES = 8               # 记忆最近多少帧的指纹（约 40s），用于兜底去重
MISSED_GROWTH_MIN = 40          # px：底部下移超过它却没识别出新消息 → 打自检日志
NO_SCROLL_WARN_AFTER = 5        # 滚动条连续读不到这么多帧 → 提示一次

# 气泡内按钮等 UI 字样（精确匹配整行才丢弃）
UI_WORDS = {"复制", "下载", "编辑", "转发", "收藏", "提醒", "未查收", "已读", "已签收",
            "群签到", "水印拍照", "员工档案", "任务进度", "使用帮助"}
SIZE_RE = re.compile(r"^\d+(\.\d+)?\s*(KB|MB|GB|B)$", re.I)
# 引擎把「日期 时间」粘死时的修复：「2026-10-1615:56:22」→「2026-10-16 15:56:22」。
# 模式要求完整日期+时间形状，不会误伤普通内容
DATETIME_GLUE_RE = re.compile(r"(\d{4}-\d{1,2}-\d{1,2})(\d{1,2}:\d{2}(?::\d{2})?)")
# 时间戳样式（配合居中位置判定；单纯居中不足以判定，中部的灰色文字会误判）
TIME_RE = re.compile(r"^(今天|昨天|前天)?\s*\d{1,2}:\d{2}$|^\d+\s*分钟前$|"
                     r"^\d+\s*小时前$|^刚刚$|^星期[一二三四五六日]\s*\d{1,2}:\d{2}$|"
                     r"^\d{1,2}月\d{1,2}日(\s*\d{1,2}:\d{2})?$")

# 钉钉的时间戳语义：**同一天的消息只显示 hh:mm**，跨天才用
# 「昨天 hh:mm」「星期X hh:mm」「MM-DD hh:mm」标记。据此可反推消息是否当天。
CROSS_DAY_RE = re.compile(r"^(昨天|前天|星期[一二三四五六日]|\d{1,2}月\d{1,2}日)")
HOURS_AGO_RE = re.compile(r"^(\d+)\s*小时前$")


def is_cross_day(t: str, now: datetime | None = None) -> bool:
    """判断钉钉时间戳是否指向「非当天」。

    无法判定（没有时间标记、或只有 hh:mm / N分钟前 / 刚刚）时返回 False，
    即**放行**——宁可交给锚点防线兜，也不因判不出来而漏消息。
    """
    s = (t or "").replace(" ", "")
    if not s:
        return False
    if s.startswith("今天") or s in ("刚刚",):
        return False
    if CROSS_DAY_RE.match(s):
        return True
    m = HOURS_AGO_RE.match(s)
    if m:
        # 「N 小时前」在凌晨会落到昨天
        return (now or datetime.now()).hour < int(m.group(1))
    return False


CLOCK_RE = re.compile(r"^(?:今天)?(\d{1,2}):(\d{2})$")
MINUTES_AGO_RE = re.compile(r"^(\d+)\s*分钟前$")
WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def norm_title(s: str) -> str:
    """标题/群名归一化：去掉空白与装饰括号，只留实质字符。"""
    return re.sub(r"[\s【】\[\]（）()·\-—_]", "", s or "")


def partial_ratio(needle: str, hay: str) -> float:
    """needle 在 hay 里的**最佳片段**相似度（0~1）。

    为什么不用整串 SequenceMatcher.ratio()：窗口标题常带前后缀
    （「③5 更新」「归属于 …」），整串比较会稀释相似度；而 OCR 又
    会错字漏字，简单的 ``needle in hay`` 判不中。这里取「最长公共
    连续块占 needle 长度」的比例做分——

        partial_ratio("数字质控", "教字质控】浙江杭州…")  -> 0.75
        partial_ratio("数字质控", "张三的会话")            -> 0.0
    """
    if not needle or not hay:
        return 0.0
    if needle in hay:
        return 1.0
    m = difflib.SequenceMatcher(None, needle, hay).find_longest_match(
        0, len(needle), 0, len(hay))
    return m.size / len(needle)


def parse_clock(t: str, now: datetime | None = None) -> tuple[int, int] | None:
    """把钉钉时间戳换算成当天时刻 ``(时, 分)``；判不出来返回 None。"""
    s = (t or "").replace(" ", "")
    if not s:
        return None
    base = now or datetime.now()
    m = CLOCK_RE.match(s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        return (h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None
    if s == "刚刚":
        return (base.hour, base.minute)
    m = MINUTES_AGO_RE.match(s)
    if m:
        d = base - timedelta(minutes=int(m.group(1)))
        return (d.hour, d.minute)
    m = HOURS_AGO_RE.match(s)
    if m:
        d = base - timedelta(hours=int(m.group(1)))
        return (d.hour, d.minute)
    return None


def in_time_window(t: str, window: str, now: datetime | None = None) -> tuple[bool, str]:
    """时间戳是否落在 ``HH:MM-HH:MM`` 时段内（支持跨零点）。

    返回 ``(是否通过, 用于解释的说明)``。

    说不上来时间时（钉钉对「最新几条」不显示时间戳）**按当前时刻判断**——
    这正好对应使用者真正想要的效果：设 08:00-18:00 = 只在下班前转发。
    """
    m = WINDOW_RE.match(window or "")
    if not m:
        return True, "时段配置为空或格式不对，本次不过滤"
    sh, sm, eh, em = (int(x) for x in m.groups())
    if not (0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60):
        return True, "时段数值非法，本次不过滤"
    base = now or datetime.now()
    cur = parse_clock(t, base)
    how = f"时间戳 {t!r}" if cur is not None and (t or "").strip() else "无时间戳→按当前时刻"
    if cur is None:
        cur = (base.hour, base.minute)
    cur_m = cur[0] * 60 + cur[1]
    start_m, end_m = sh * 60 + sm, eh * 60 + em
    if start_m <= end_m:
        ok = start_m <= cur_m <= end_m
    else:                                     # 跨零点，如 22:00-06:00
        ok = cur_m >= start_m or cur_m <= end_m
    return ok, f"{how} {cur[0]:02d}:{cur[1]:02d}，时段 {sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"


def print_window(hwnd: int):
    """PrintWindow 抓整个窗口，返回 PIL.Image（不抢焦点，被遮挡可用）。"""
    from PIL import Image

    rect = wt.RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    hwnd_dc = user32.GetWindowDC(wt.HWND(hwnd))
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        if not user32.PrintWindow(wt.HWND(hwnd), mem_dc, PW_RENDERFULLCONTENT):
            return None
        bmi = _BMI()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS) == 0:
            return None
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(wt.HWND(hwnd), hwnd_dc)


def _mean_rgb(arr, box, pad: int = 1):
    """取一块区域的暗像素均值（≈文字颜色）与亮像素均值（≈背景颜色）。"""
    import numpy as np

    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    crop = arr[y0:y1 + pad, x0:x1 + pad]
    if crop.size == 0:
        return 255.0, (255, 255, 255), (0, 0, 0)
    flat = crop.reshape(-1, 3).astype(int)
    lum = flat.mean(axis=1)
    mid = lum.mean()
    dark = flat[lum < mid]
    light = flat[lum >= mid]
    tc = tuple(int(v) for v in dark.mean(axis=0)) if len(dark) else (0, 0, 0)
    bc = tuple(int(v) for v in light.mean(axis=0)) if len(light) else (255, 255, 255)
    return float((tc[0] + tc[1] + tc[2]) / 3), tc, bc


def classify_block(text: str, lum: float, tc: tuple, box: tuple,
                   area_w: int) -> str:
    """把一个 OCR 文本块分成：content / sender / time / drop。

    颜色基准在**原始分辨率**上测得（放大插值会把文字洗浅）：
      正文 ≈27  发送者/时间 ≈113  元数据 ≈163（tools/probe_dingtalk_capture.py 实测）
    """
    r, g, b = tc
    t = text.strip()
    # 气泡内按钮字样优先丢弃（「复制」按钮带浅蓝底色，会骗过链接判定，必须先查）
    if not t or t in UI_WORDS or SIZE_RE.match(t):
        return "drop"
    # 橙红/棕色 = 气泡按钮、安全提醒横幅（实测 编辑(236,104,0)、横幅(242,81,12)）
    if r > 190 and int(r) - int(b) > 60:
        return "drop"
    # 蓝色链接（B 明显高于 R）当正文
    if int(b) - int(r) > 40 and lum < 160:
        return "content"
    if lum < CONTENT_LUM_MAX:
        return "content"
    if SENDER_LUM[0] <= lum <= SENDER_LUM[1]:
        # 中灰 + 居中 + 形似时间（「今天 11:25」「17分钟前」）才是时间戳
        cx = (box[0] + box[2]) / 2
        if (abs(cx - area_w / 2) < area_w * TIME_CENTER_TOL
                and TIME_RE.match(t.replace(" ", ""))):
            return "time"
        return "sender"
    return "drop"     # 浅灰元数据（未查收/文件大小，实测 163）


def merge_lines(blocks: list[dict]) -> list[dict]:
    """把垂直方向重叠的块合并成行，行内按 x 排序。"""
    lines: list[dict] = []
    for b in sorted(blocks, key=lambda x: (x["y0"], x["x0"])):
        placed = False
        for ln in lines:
            overlap = min(ln["y1"], b["y1"]) - max(ln["y0"], b["y0"])
            if overlap > LINE_OVERLAP * min(ln["y1"] - ln["y0"], b["y1"] - b["y0"]):
                ln["parts"].append(b)
                ln["y0"] = min(ln["y0"], b["y0"])
                ln["y1"] = max(ln["y1"], b["y1"])
                placed = True
                break
        if not placed:
            lines.append({"y0": b["y0"], "y1": b["y1"], "parts": [b]})
    out = []
    for ln in sorted(lines, key=lambda x: x["y0"]):
        parts = sorted(ln["parts"], key=lambda p: p["x0"])
        text = DATETIME_GLUE_RE.sub(r"\1 \2", _join_line_text(parts))
        out.append({
            "y0": ln["y0"], "y1": ln["y1"],
            "x0": min(p["x0"] for p in parts),
            "x1": max(p["x1"] for p in parts),
            "text": text,
            "kind": ln["parts"][0]["kind"],
            "kinds": [p["kind"] for p in parts],
        })
    return out


def _join_line_text(parts: list[dict]) -> str:
    """把一行里的 OCR 块按 x 顺序拼回文本。

    关键在**空格给不给**：两个纯数字块几乎贴着（间隙 ≤ 1.2×字宽），
    判为同一个数字被检测框拆开，直接连回去；否则保留空格。
    表格里「单号 | 票数」的列间距实测 ≥25px（≈3×字宽），不会被误连——
    旧的全局去空格规则会把「773446376739379 1」粘成 16 位数，
    单号和票数全错（2026-10-16 线上实测）。
    """
    text = ""
    prev: dict | None = None
    for p in parts:
        if prev is not None:
            gap = p["x0"] - prev["x1"]
            cw = ((prev["x1"] - prev["x0"]) / max(1, len(prev["text"]))
                  + (p["x1"] - p["x0"]) / max(1, len(p["text"]))) / 2
            if not (prev["text"].isdigit() and p["text"].isdigit()
                    and gap <= 1.2 * cw):
                text += " "
        text += p["text"]
        prev = p
    return text


def group_units(lines: list[dict], area_w: int) -> list[dict]:
    """把行聚成消息单元，并识别发送者 / 自己发的 / 时间。

    分组规则（按优先级）：
      1. 时间戳行 = 分隔符——钉钉的时间标记显示在「下一条消息」上方；
      2. 正文已开始后又出现发送者行 = 新消息（同屏两人前后发言）；
      3. 垂直间距超过 UNIT_GAP = 新消息。
    """
    units: list[list[dict]] = []
    for ln in lines:
        prev = units[-1][-1] if units else None
        if prev is None:
            units.append([ln])
            continue
        has_content = any(p["kind"] == "content" for p in units[-1])
        boundary = (
            ln["kind"] == "time"
            or (ln["kind"] == "sender" and has_content)
            or ln["y0"] - prev["y1"] > UNIT_GAP
        )
        if boundary:
            units.append([ln])
        else:
            units[-1].append(ln)

    out: list[dict] = []
    for grp in units:
        sender = ""
        content_lines: list[str] = []
        last_time = ""
        started_content = False
        for ln in grp:
            if ln["kind"] == "time":
                last_time = ln["text"]
                continue
            if ln["kind"] == "sender" and not started_content:
                sender = (sender + " " + ln["text"]).strip()
                continue
            if ln["kind"] == "content":
                started_content = True
                content_lines.append(ln["text"])
        if not content_lines:
            continue
        # 用**正文行**判断是否自己发的：时间戳行居中、发送者行贴左，都不能作准
        content_x0 = min(ln["x0"] for ln in grp if ln["kind"] == "content")
        is_self = content_x0 > area_w * SELF_LEFT_RATIO
        out.append({
            "sender": "" if is_self else sender,
            "content": "\n".join(content_lines),
            "is_self": is_self,
            "time": last_time,
            "y0": grp[0]["y0"],
            # y1/height 供诊断与「单条消息占满视口」判定使用（不参与指纹）
            "y1": max(ln["y1"] for ln in grp),
        })
    return out


_SENDER_SPACE_RE = re.compile(r"\s+")
_ROBOT_SUFFIX = "机器人"


def norm_sender(sender: str) -> str:
    """发送者归一化，**只用于指纹与相似度判定**，不改变入库/转发时的展示。

    两处实测过的读法抖动，都会把同一条消息算成两条：
      · 机器人昵称的后缀时有时无 —— 同一帧消息分别读成
        「签收跟单专员」和「签收跟单专员 机器人」，正文 md5 完全相同；
      · 昵称被 OCR 拆成两个文本框，拼起来时中间多一个空格。

    这里统一去掉空白与结尾的「机器人」，让它们对上。刻意**不**去掉
    更靠前的字，避免把「签收跟单专员」和「拦截跟单专员」这类不同机器人
    混成同一个。
    """
    s = _SENDER_SPACE_RE.sub("", sender or "")
    return s[:-len(_ROBOT_SUFFIX)] if s.endswith(_ROBOT_SUFFIX) else s


def unit_fp(unit: dict) -> str:
    """消息单元指纹（发送者 + 内容），用作增量差分、锚点与去重的稳定标识。

    发送者先过一遍 :func:`norm_sender` —— 否则机器人昵称多读/少读一个
    「机器人」后缀就会算出另一个指纹，同一条消息被当成两条重复采、重复转发。
    """
    return hashlib.md5(
        f"{norm_sender(unit['sender'])}|{unit['content']}".encode("utf-8")
    ).hexdigest()


def at_bottom(scroll: tuple[float, float] | None, tol: float = AT_BOTTOM_TOL) -> bool:
    """滚动条状态 ``(当前值, 最大值)`` 是否表示**视口贴底**（显示的是最新消息）。

    实测贴底时 ``Value == Maximum``（-488/-488），向上滚后 ``Value`` 变得更负
    （-1088/-488，越往上越负），所以判据就是「当前位置贴近最大位置」。
    """
    if not scroll:
        return False
    cur, mx = scroll
    if mx is None or cur is None:
        return False
    return abs(mx - cur) <= tol


def scroll_offset(scroll: tuple[float, float] | None) -> float | None:
    """滚动条位置换算成「距内容顶端的偏移」（正数，px）。"""
    if not scroll or scroll[0] is None:
        return None
    return abs(float(scroll[0]))


def _digits(s: str) -> tuple[str, ...]:
    """取出文本里所有数字串（顺序敏感），用于「数字必须一致」的判定。"""
    return tuple(re.findall(r"\d+", s or ""))


@lru_cache(maxsize=4096)
def _match_cached(ca: str, sa: str, cb: str, sb: str, sim: float) -> bool:
    """`units_match` 的带缓存实现（同一帧里同一对消息会被反复比较）。"""
    if ca == cb:
        return sa == sb or not sa or not sb
    if not ca or not cb:
        return False
    if _digits(ca) != _digits(cb):
        return False
    if max(len(ca), len(cb)) > FUZZY_MAX_LEN:
        return False
    if abs(len(ca) - len(cb)) > 0.35 * max(len(ca), len(cb)) + 3:
        return False
    r = difflib.SequenceMatcher(None, ca, cb).ratio()
    if sa and sb and sa != sb:
        r *= SENDER_MISMATCH_PENALTY
    return r >= sim


def units_match(a: dict, b: dict, sim: float = ANCHOR_SIM) -> bool:
    """两条消息是否「同一条」，容忍 OCR 抖动。

    关键取舍：**数字必须完全一致**。本群消息主体是表格（单号 + 票数），
    两张不同的表在版式上高度相似，任何纯字符相似度都会把它们混成一条 ——
    那会让新表格被静默吞掉，比重复转发严重得多。反之，OCR 认错一个汉字
    （质→教、漏「萧」）时数字不变，仍能正确对上。
    """
    return _match_cached(a.get("content", ""), norm_sender(a.get("sender", "")),
                         b.get("content", ""), norm_sender(b.get("sender", "")),
                         sim)


def _find_match(prev_units: list[dict], used: list[bool],
                unit: dict, sim: float) -> int | None:
    """在 prev_units 里找一条尚未被占用、且与 unit 判定为同一条的消息。"""
    for j, p in enumerate(prev_units):
        if not used[j] and units_match(p, unit, sim):
            return j
    return None


def _pick_with_units(units: list[dict], fps: list[str], prev_units: list[dict],
                     anchor_guard: bool = True, sim: float = ANCHOR_SIM):
    """带模糊容错的锚点定位（生产路径）。

    与精确版的关系：精确版把指纹当集合做多重差分，锚点必须精确命中 ——
    OCR 抖一个字锚点就丢、整帧被丢。这里改成「相似度匹配 + 逐条消费」，
    保留锚点语义（只在锚点**下方**取新），同时容忍抖动。

    返回 `(indexes, new_anchor)`；`indexes` 为 None 表示本帧应整体丢弃。
    """
    if not fps:
        return [], None
    new_anchor = fps[-1]
    if not prev_units:
        return [], new_anchor

    used = [False] * len(prev_units)
    start = 0
    if anchor_guard:
        # 锚点：从上一帧最下面往上前进，取第一条仍能在本帧找到对应者的消息。
        # 对应关系按「出现序号」定位，这样内容完全相同的连续消息也能认出新加的那条。
        start = None
        for pi in range(len(prev_units) - 1, -1, -1):
            rank = 1 + sum(1 for j in range(pi)
                           if units_match(prev_units[j], prev_units[pi], sim))
            hit, seen = None, 0
            for i, u in enumerate(units):
                if units_match(prev_units[pi], u, sim):
                    seen += 1
                    hit = i
                    if seen == rank:
                        break
            if hit is not None:
                start = hit + 1
                break
        if start is None:
            return None, new_anchor

    picked: list[int] = []
    for i, u in enumerate(units):
        j = _find_match(prev_units, used, u, sim)
        if j is None:
            if i >= start:
                picked.append(i)
        else:
            used[j] = True
    return picked, new_anchor


def pick_new_units(units: list[dict], fps: list[str],
                   prev_fps: list[str] | None, anchor_guard: bool = True,
                   prev_units: list[dict] | None = None, sim: float = ANCHOR_SIM):
    """从本帧中挑出**真正的新消息**索引（防线 A：锚点定位）。

    原理：新消息只会出现在「锚点」的**下方**。滚动把历史消息带进视口时，
    它们必然落在锚点之上，因而不会被选中。

    锚点的选取对 OCR 抖动很宽容：从上一帧的最下面往上前进，取**第一条仍出现在
    本帧里**的消息作为锚。这样即使最后一条因 OCR 波动而指纹改变，也能退而用
    倒数第二条锚定，不会因为一次抖动就整帧丢弃。

    传了 `prev_units` 时走模糊容错版（见 `_pick_with_units`）——OCR 抖字不再
    导致锚点丢失或新消息被重复转发。

    返回 `(indexes, new_anchor)`；`indexes` 为 `None` 表示本帧应整体丢弃
    ——上一帧没有任何一条消息仍留在本帧中，说明视口发生了大幅位移（多半被
    滚动了），此时差集里混着大量历史消息，宁可漏一帧也不误发历史。
    """
    if prev_units:
        return _pick_with_units(units, fps, prev_units, anchor_guard, sim)

    if not fps:
        return [], None

    cur = Counter(fps)
    cur_set = set(fps)
    new_anchor = fps[-1]

    if prev_fps is None:
        return [], new_anchor

    if anchor_guard:
        anchor_value = None
        for fp in reversed(prev_fps):
            if fp in cur_set:
                anchor_value = fp
                break
        if anchor_value is None:
            return None, new_anchor

        # 锚点在本帧中的落点：同样内容可能出现多次，按它在**上一帧里的出现序号**
        # 定位，这样「内容完全相同的连续消息」也能把新增的那条认出来。
        want = Counter(prev_fps)[anchor_value]
        seen = 0
        start = None
        for i, fp in enumerate(fps):
            if fp == anchor_value:
                seen += 1
                if seen == want:
                    start = i + 1
                    break
        if start is None:              # 出现次数变少（消息被撤回等），退到最后一个
            start = len(fps)
            for i in range(len(fps) - 1, -1, -1):
                if fps[i] == anchor_value:
                    start = i + 1
                    break
    else:
        start = 0                     # 关闭防护 = 退化为纯增量差分

    new_fp = cur - Counter(prev_fps)
    picked: list[int] = []
    for i in range(start, len(units)):
        fp = fps[i]
        if new_fp.get(fp, 0) > 0:
            new_fp.subtract({fp: 1})
            picked.append(i)
    return picked, new_anchor


class _OcrEngine:
    """统一两代 rapidocr 的调用形状：``engine(arr) -> [(box, text, conf), ...]``。

    为什么包一层：旧包 ``rapidocr_onnxruntime``（1.x，PP-OCRv3）返回
    ``[(box, text, conf), ...]``；新包 ``rapidocr``（3.x，PP-OCRv6）返回
    ``RapidOCROutput`` 对象（.boxes/.txts/.scores）。`_grab_frame` 与
    `_read_title` 两处调用点只想面对一种形状 —— 在这里归一，上层零改动。

    为什么优先新包（2026-10-16 实测，tools/ocr_bench.py 同图对比）：
    v3 把「引发」读成「引1发」并原样转发进微信群，v6 无此错字；v6 置信度
    普遍 0.94+；速度不降反升。且 v6 出厂参数即可检全表格数字 —— v3 那组
    「孤立数字」补丁参数搬到 v6 上反而引入碎块与新错字（「引|发」），不迁移。
    """

    def __init__(self, backend, name: str):
        self._backend = backend
        self.name = name

    def __call__(self, arr):
        out = self._backend(arr)
        # legacy 1.x 返回 (result, elapse) 元组，result 为 None 表示无内容
        if isinstance(out, tuple) and len(out) == 2 and not isinstance(out[1], str):
            out = out[0]
        if out is None:
            return []
        if hasattr(out, "txts"):                      # 3.x RapidOCROutput
            # ⚠ boxes/scores/txts 可能是 numpy 数组：不能用 `or` 判空
            #（多元素数组的真值是歧义的，会抛 ValueError），必须 is None。
            txts = list(out.txts) if out.txts is not None else []
            raw_scores = out.scores if out.scores is not None else None
            scores = ([float(s) for s in raw_scores] if raw_scores is not None
                      else [])
            scores += [1.0] * (len(txts) - len(scores))
            raw_boxes = out.boxes if out.boxes is not None else None
            boxes = (list(raw_boxes) if raw_boxes is not None else [])
            boxes += [()] * (len(txts) - len(boxes))
            return list(zip(boxes, txts, scores))
        return list(out)                              # 1.x 原生形状


def _build_ocr_engine() -> _OcrEngine:
    """构建 OCR 引擎：优先新版 rapidocr（PP-OCRv6），导入失败回退旧包。

    旧包那组参数是围绕 v3 的「表格孤立数字漏检」实测标定的（见下），
    **只对 v3 有效**，所以跟旧包绑死在一起，不作为全局配置。
    """
    try:
        from rapidocr import RapidOCR
    except ImportError:
        from rapidocr_onnxruntime import RapidOCR
        # 2026-10-16 实测标定（v3）：默认参数（limit_side_len=736, box_thresh=0.5）
        # 检不出表格里的孤立数字——「票数」列的「1」6 个全丢，转发内容缺列。
        # 把检测输入分辨率提到 1536、放宽阈值后 6/6 检出；
        # 代价是单帧 OCR 0.6→1.6s（轮询间隔 5s，仍富余）。
        # det_model_path=None 是绕过该版本 kwargs 覆盖逻辑的必填占位。
        backend = RapidOCR(det_limit_side_len=1536, det_box_thresh=0.35,
                           det_thresh=0.25, text_score=0.35,
                           det_model_path=None)
        return _OcrEngine(backend, "rapidocr_onnxruntime / PP-OCRv3（旧包回退）")
    # v6 出厂参数即最优（bench 实测：13/13 运单号、无新增错字、无碎块），
    # 不传任何参数；只把它的 INFO 日志压到 WARNING（每次构建引擎打 6 条
    # 「Using …onnx」，生产首帧的日志里全是噪音）。
    return _OcrEngine(
        RapidOCR(params={"Global.log_level": "WARNING"}),
        "rapidocr / PP-OCRv6")


class DingTalkOCRSource(Source):
    name = "dingtalk-ocr"

    def __init__(self, cfg):
        self.cfg = cfg
        self.dt = cfg.dingtalk
        self.interval = max(3.0, float(getattr(self.dt, "ui_poll_interval", 5.0)))
        self.title_check = bool(getattr(self.dt, "title_check", True))
        self.title_ratio = float(getattr(self.dt, "title_ratio", 0.55))
        self.time_filter = str(getattr(self.dt, "time_filter", "today")).lower()
        self.time_window = str(getattr(self.dt, "time_window", "08:00-18:00"))
        self.anchor_guard = bool(getattr(self.dt, "anchor_guard", True))
        self._stop = threading.Event()
        self._uia = None
        self._ocr = None
        self._prev_fps: list[str] | None = None   # 基线帧：按屏幕顺序的指纹
        self._prev_units: list[dict] = []         # 基线帧的消息单元（模糊比对用）
        self._prev_scroll: tuple[float, float] | None = None   # 基线帧的滚动条状态
        self._prev_area_h: int = 0                # 基线帧的消息区高度（防窗口缩放误判）
        self._recent: deque[list[str]] = deque(maxlen=RECENT_FRAMES)
        self._raw_seq: Counter = Counter()
        self._last_title_ok: bool | None = None
        self._was_at_bottom: bool | None = None
        self._no_scroll_streak = 0               # 滚动条连续读不到的次数

    # ---------- 底层 ----------

    def _load_uia(self):
        if self._uia is not None:
            return self._uia
        wxauto_path = Path(self.cfg.wechat.wxauto_path).resolve()
        if not wxauto_path.exists():
            raise FileNotFoundError(f"wxauto 库路径不存在：{wxauto_path}")
        if str(wxauto_path) not in sys.path:
            sys.path.insert(0, str(wxauto_path))
        from wxauto import uiautomation as uia
        self._uia = uia
        return uia

    def _get_ocr(self) -> _OcrEngine:
        if self._ocr is None:
            self._ocr = _build_ocr_engine()
            log.info("OCR 引擎：%s", self._ocr.name)
        return self._ocr

    def _find_window(self):
        uia = self._load_uia()
        for cls in CLIENT_CLASS_CANDIDATES:
            wnd = uia.WindowControl(ClassName=cls, searchDepth=1)
            if wnd.Exists(0.4):
                return wnd
        wnd = uia.WindowControl(Name="钉钉", searchDepth=1)
        return wnd if wnd.Exists(1) else None

    def _find_msg_rect(self, wnd):
        """消息区矩形（窗口相对坐标）。返回 ``(rect, 命中的类名, 控件)``。

        先在常规深度找；找不到时再放宽深度找一次 —— 不同会话/版本下
        `ChatBubbleWidget` 的层级会因为多一层容器而变深（线上出现过连续
        70 秒「定位不到钉钉消息区控件」，恢复正常后一切照旧）。
        """
        for cls in (MSG_AREA_CLASS, CHAT_BOX_CLASS):
            for depth in (14, 24):
                try:
                    ctrl = wnd.Control(ClassName=cls, searchDepth=depth)
                    if not ctrl.Exists(0.5 if depth == 14 else 0.3):
                        continue
                    r = ctrl.BoundingRectangle
                    wr = wnd.BoundingRectangle
                    rect = (r.left - wr.left, r.top - wr.top,
                            r.right - wr.left, r.bottom - wr.top)
                    if rect[2] - rect[0] > 100 and rect[3] - rect[1] > 100:
                        if depth != 14:
                            log.debug("消息区在第 %d 层才找到（控件树比常规更深）", depth)
                        return rect, cls, ctrl
                except Exception:
                    continue
        return None, "", None

    # ---------- 每帧处理 ----------

    def _read_title(self, wnd, msg_rect) -> list[str]:
        """OCR 窗口标题条（聊天面板顶部），返回多个候选读法。

        为什么返回列表：标题行下面紧挨着「归属于 XX集团」一行，整条
        一起识别时两行会被检测框合并、互相拖累——实测把
        「【数字质控】浙江杭州萧山区大江东公司」读成
        「学国控）新江州山区大江东公司」，护栏直接判死。
        所以除整条外，把上段（群名行）、下段（归属行）各单独识别一遍，
        由 _title_matches 取最像配置名的一个。
        """
        import numpy as np
        from PIL import Image

        wr = wnd.BoundingRectangle
        w = wr.right - wr.left
        left, top = msg_rect[0], msg_rect[1]
        strip = self._last_frame.crop(
            (left, max(20, top - 64), w, max(top - 4, top - 64 + 10)))
        h = strip.height

        parts = [strip, strip.crop((0, 0, strip.width, max(12, int(h * 0.6))))]
        if h > 30:
            parts.append(strip.crop((0, int(h * 0.35), strip.width, h)))
        texts: list[str] = []
        for part in parts:
            for scale in (2.0, 3.0):
                img = part.resize((int(part.width * scale),
                                   int(part.height * scale)), Image.LANCZOS)
                res = self._get_ocr()(np.array(img))
                text = "".join(x[1] for x in (res or [])).strip()
                if text:
                    if text not in texts:
                        texts.append(text)
                    break        # 该区域已读出内容，不必再放大
        return texts

    def _title_matches(self, title: str | list[str]) -> tuple[bool, float, str]:
        """标题（或候选读法列表）与 chat_names 之一匹配则通过。

        两层判定（取更宽松的一个）：
          1. **包含**：标题里直接含有配置名（设置中心保存的往往是群名
             片段，如「数字质控」，必须走这条才判得中）；
          2. **片段模糊**：OCR 会认错字、标题又带前后缀，用
             partial_ratio（配置名在标题里的最佳片段相似度）兜底，
             阈值 title_ratio。
        """
        names = [n for n in (self.dt.chat_names or []) if n]
        if not names:
            return True, 1.0, ""
        cands = [c for c in (norm_title(t) for t in
                             (title if isinstance(title, list) else [title]))
                 if c]
        best_name, best = "", 0.0
        for n in names:
            nn = norm_title(n)
            if not nn:
                continue
            if any(nn in c for c in cands):      # 包含即通过，零误杀
                return True, 1.0, n
            r = max((partial_ratio(nn, c) for c in cands), default=0.0)
            if r > best:
                best_name, best = n, r
        return best >= self.title_ratio, best, best_name

    def _grab_frame(self) -> dict | None:
        """截屏 + OCR + 解析。返回 {'units':…, 'title':…} 或 None。"""
        import numpy as np
        from PIL import Image

        wnd = self._find_window()
        if wnd is None:
            log.warning("未找到钉钉主窗口")
            return None
        hwnd = wnd.NativeWindowHandle
        if user32.IsIconic(wt.HWND(hwnd)):
            log.warning("钉钉窗口处于最小化状态，截不到内容")
            return None

        msg_rect, hit, area_ctrl = self._find_msg_rect(wnd)
        if msg_rect is None:
            log.warning("定位不到钉钉消息区控件")
            return None

        # 滚动位置必须与「截下来的像素」是同一瞬间的状态，所以紧贴
        # PrintWindow 读它 —— 放到 OCR 之后读会晚 1~2 秒（标题条还要多跑
        # 几次 OCR），期间若有新消息到达，Δ 就会算错。
        scroll = self._scroll_state(area_ctrl)
        frame = print_window(hwnd)
        if frame is None:
            log.warning("PrintWindow 失败")
            return None
        self._last_frame = frame

        area = frame.crop(msg_rect)
        scaled = area.resize((int(area.width * OCR_SCALE), int(area.height * OCR_SCALE)),
                             Image.LANCZOS)
        arr = np.array(scaled)
        # 颜色分析必须用原始分辨率——放大插值会把深色文字洗浅，破坏分类基准
        orig = np.array(area.convert("RGB"))
        t = time.time()
        res = self._get_ocr()(arr)
        log.debug("OCR %.2fs，%d 块", time.time() - t, len(res or []))

        blocks: list[dict] = []
        aw, ah = area.width, area.height
        for item in (res or []):
            try:
                conf = float(item[2])
            except (TypeError, ValueError):
                conf = 0.0
            if conf < MIN_CONF:
                continue
            xs = [p[0] for p in item[0]]
            ys = [p[1] for p in item[0]]
            x0, x1 = int(min(xs) / OCR_SCALE), int(max(xs) / OCR_SCALE)
            y0, y1 = int(min(ys) / OCR_SCALE), int(max(ys) / OCR_SCALE)
            lum, tc, _bc = _mean_rgb(orig, (x0, y0, x1, y1))
            kind = classify_block(item[1], lum, tc, (x0, y0, x1, y1), aw)
            if kind == "drop":
                continue
            blocks.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1,
                           "text": item[1].strip(), "kind": kind, "conf": conf})

        title = self._read_title(wnd, msg_rect) if self.title_check else []
        return {"area_w": aw, "area_h": ah, "blocks": blocks, "title": title,
                "scroll": scroll}

    def _scroll_state(self, area) -> tuple[float, float] | None:
        """读消息区滚动条 → ``(当前位置, 最大值)``（均为负 px），读不到返回 None。

        实测（2026-10-16）钉钉 PC 用 ``ddesign::SmoothScrollBar`` + RangeValuePattern
        暴露精确滚动偏移（贴底 ``Value == Maximum``，实测 -488/-488；向上滚 5 格
        后 ``Value = -1088`` 而 ``Maximum`` 仍是 -488），据此可**确定性地**判断
        「屏幕上是不是最新的消息」，不依赖帧间内容是否重叠。

        两个坑（都踩过）：
          1. `Control(ClassName=…)` **搜出来的是基类 Control 实例**，没有
             `GetRangeValuePattern()`，必须用 `GetPattern(PatternId.RangeValuePattern)`。
             原先直接 getattr 抛 AttributeError 被 except 吞掉，表现成「静默读不到」。
          2. 从窗口根搜索慢（0.18s，深度不足还会耗满超时），从消息区内部往下搜
             只要 0.00s，所以这里传消息区控件进来。
        """
        if area is None:
            return None
        try:
            bar = area.Control(ClassName=SCROLLBAR_CLASS, searchDepth=3)
            if not bar.Exists(0.5):
                self._note_no_scrollbar("消息区里没有滚动条控件")
                return None
            pat = None
            try:
                pat = bar.GetPattern(self._load_uia().PatternId.RangeValuePattern)
            except Exception as exc:
                log.debug("GetPattern(RangeValuePattern) 失败：%s", exc)
            if pat is None:
                self._note_no_scrollbar("滚动条不支持 RangeValuePattern")
                return None
            cur, mx = pat.Value, pat.Maximum
            if cur is None or mx is None:
                self._note_no_scrollbar("RangeValuePattern 的值不可读")
                return None
            self._no_scroll_streak = 0
            return float(cur), float(mx)
        except Exception as exc:
            self._note_no_scrollbar(f"读滚动条异常：{exc}")
            return None

    def _note_no_scrollbar(self, why: str) -> None:
        """滚动条连续读不到时提示一次。

        这条不是无关紧要的日志：读不到滚动条就只能退回旧的锚点判定，
        而「消息很高时新消息把上一条整条顶出视口」那种情况会漏转。
        """
        self._no_scroll_streak += 1
        if self._no_scroll_streak == NO_SCROLL_WARN_AFTER:
            log.warning("读不到钉钉消息区滚动条（%s）——将退回旧的锚点判定，"
                        "消息很高（一屏只装 1~2 条）时可能漏转新消息", why)

    # ---------- Source 接口 ----------

    def check(self) -> tuple[bool, str]:
        try:
            self._load_uia()
        except Exception as exc:
            return False, f"UIAutomation 不可用：{exc}"
        if not (self.dt.chat_names or []):
            return False, "dingtalk.chat_names 为空——OCR 模式靠它做标题护栏，必须配置"
        try:
            self._get_ocr()
        except Exception as exc:
            return False, f"OCR 引擎不可用：{exc}（pip install rapidocr）"
        wnd = self._find_window()
        if wnd is None:
            return False, "未找到钉钉主窗口，请确认客户端已启动并登录"
        rect, hit, area_ctrl = self._find_msg_rect(wnd)
        if rect is None:
            return False, "定位不到消息区控件（ChatBubbleWidget），钉钉版本可能不兼容"
        scroll = self._scroll_state(area_ctrl)
        tail = ("滚动条可读（贴底判定可用）" if scroll
                else "⚠ 读不到滚动条，消息很高时可能漏转")
        return True, f"钉钉窗口与消息区定位正常（{hit}），OCR 引擎就绪；{tail}"

    def run(self, on_message) -> None:
        chat_names = list(self.dt.chat_names or [])
        log.info("OCR 轮询启动，间隔 %.1fs，监控会话：%s", self.interval, chat_names)
        poll = max(self.interval, 4.0)
        # 心跳：没有新消息时日志一片安静，用户没法区分「在正常巡检」和
        # 「悄悄失效」。每 10 分钟报一次到帧情况，降低排查成本。
        beat_at = time.time()
        frames = 0
        while not self._stop.is_set():
            started = time.time()
            try:
                self._poll_once(on_message)
                frames += 1
            except Exception as exc:
                log.exception("OCR 轮询异常：%s", exc)
            if time.time() - beat_at >= 600:
                log.info("采集心跳：近 %.0f 分钟巡检 %d 帧，链路正常",
                         (time.time() - beat_at) / 60, frames)
                beat_at, frames = time.time(), 0
            # 等待切片化：停止指令（含看门狗抛出的 KeyboardInterrupt）能秒级生效，
            # 不必等满一个完整轮询周期。
            deadline = started + poll
            while not self._stop.is_set() and time.time() < deadline:
                self._stop.wait(min(0.3, max(0.05, deadline - time.time())))

    def _poll_once(self, on_message) -> None:
        frame = self._grab_frame()
        if frame is None:
            return

        # 标题护栏：只转发指定会话
        if self.title_check:
            ok, ratio, matched = self._title_matches(frame["title"])
            if ok != self._last_title_ok:
                shown = " / ".join(frame["title"]) or "(读不到标题)"
                log.info("会话标题校验：%s（相似度 %.2f，标题=%r）",
                         "通过" if ok else "不通过——本帧不转发", ratio, shown)
                self._last_title_ok = ok
            if not ok:
                # 基线作废（避免把旧帧当新消息）。但**保留滚动位置** ——
                # 用户去别的群聊一圈再切回来时，靠它把离开期间到达的消息补发。
                self._prev_fps = None
                self._prev_units = []
                return
            chat_name = matched or (self.dt.chat_names or [""])[0]
        else:
            chat_name = (self.dt.chat_names or [""])[0]

        lines = merge_lines(frame["blocks"])
        units = group_units(lines, frame["area_w"])
        fps = [unit_fp(u) for u in units]

        # 视口未贴底 = 用户正在往上翻看历史，屏幕上没有新消息：
        # 跳过本帧，并且**不覆盖基线** —— 否则用户滚回底部时就失去了对照，
        # 那段期间到达的新消息会被整批误判成历史而丢掉。
        sc = frame.get("scroll")
        if sc is not None and not at_bottom(sc):
            if self._was_at_bottom is not False:
                log.info("视口未贴底（正在浏览历史）：本帧跳过并保留基线")
                self._was_at_bottom = False
            return
        if self._was_at_bottom is False:
            log.info("视口已回到最新消息，恢复判定")
        self._was_at_bottom = True

        if not units:
            # 本帧没识别到任何文本（整屏都是图片/表情等非文本内容）：
            # **不要**拿它覆盖基线 —— 基线一旦被清空，下一帧整屏内容都会被
            # 当成「新消息」倒灌出去。
            if self._prev_fps:
                log.info("本帧未识别到文本（整屏可能是图片/表情），保留基线")
            return

        if self._prev_fps is None:
            # 基线缺失。两种来路：
            #   a) 程序刚启动 / 首次看到本群 —— 只建基线，不倒灌历史；
            #   b) **被标题护栏打断后又切回本群**（用户跑去别的群聊了一会）——
            #      手上还留着「上次贴底时」的滚动位置，用同一套几何办法把
            #      离开期间到达的消息补回来。
            # 区分方式：_prev_scroll 只由 _set_baseline 写入，启动时为 None。
            if self._prev_scroll is not None:
                picked, why = self._recover_lost_anchor(frame, units, fps)
                if picked:
                    log.info("切回本群：按滚动条差额补发离开期间的新消息（%s）", why)
                    self._emit(units, picked, fps, chat_name, on_message)
            self._set_baseline(frame, units, fps)
            log.info("基线帧建立：当前可见 %d 条消息（不转发历史）", len(units))
            return

        prev_off = scroll_offset(self._prev_scroll)
        cur_off = scroll_offset(sc)

        # 防线 A：锚点定位。picked 为 None = 视口内容整块换掉，靠滚动条几何兜底。
        picked, _ = pick_new_units(units, fps, self._prev_fps, self.anchor_guard,
                                   prev_units=self._prev_units)
        recovered = ""
        if picked is None:
            picked, why = self._recover_lost_anchor(frame, units, fps)
            if picked is None:
                # 无法判断新旧：宁可漏一帧也不误发历史。重建基线以免永久停滞。
                log.warning("锚点丢失（聊天区可能被滚动）：本帧不转发，已重建基线（%s）", why)
                self._set_baseline(frame, units, fps)
                return
            recovered = why
            log.info("锚点与基线零重叠，但已确认是新到达的内容：%s", why)

        grew = None
        if cur_off is not None and prev_off is not None:
            grew = cur_off - prev_off
        self._set_baseline(frame, units, fps)

        if not picked:
            # 自检：钉钉底部明明下移了、却没识别出新消息 —— 多半是图片/表情
            # 这类非文本内容。打出来，免得「漏了但没人知道」。
            if grew is not None and grew >= MISSED_GROWTH_MIN and not recovered:
                log.info("采集自检：消息区底部下移 %.0fpx 但本帧未识别到新消息"
                         "（可能是图片/表情等非文本内容）", grew)
            return

        self._emit(units, picked, fps, chat_name, on_message)

    def _emit(self, units: list[dict], picked: list[int], fps: list[str],
              chat_name: str, on_message) -> int:
        """把挑出来的消息过一遍时间过滤后投出去，返回实际投出条数。

        防线 B：时间维度过滤
          today  -> 丢弃被钉钉标注为往日（昨天/星期X/MM-DD）的消息
          window -> 在 today 的基础上，再要求落在配置的时段内
        """
        emitted = skipped_old = skipped_win = 0
        for i in picked:
            u = units[i]
            fp = fps[i]
            if self.time_filter in ("today", "window") and is_cross_day(u["time"]):
                skipped_old += 1
                log.info("丢弃跨天消息（时间标记 %r）：%s", u["time"], u["content"][:40])
                continue
            if self.time_filter == "window":
                ok, why = in_time_window(u["time"], self.time_window)
                if not ok:
                    skipped_win += 1
                    log.info("丢弃时段外消息（%s）：%s", why, u["content"][:40])
                    continue

            self._raw_seq[fp] += 1
            sender = "(我)" if u["is_self"] else u["sender"]
            msg = Message(
                source="dingtalk",
                chat_id="",
                chat_name=chat_name,
                sender_id="",
                sender_name=sender,
                company=infer_company(self.dt.companies, chat_name, sender, u["content"]),
                content=u["content"],
                msg_type="text",
                msg_time=datetime.now(),
                raw_id=f"ocr:{fp}:{self._raw_seq[fp]}",
            )
            on_message(msg)
            emitted += 1
        if emitted or skipped_old or skipped_win:
            log.info("识别到 %d 条新消息（丢弃跨天 %d 条、时段外 %d 条）",
                     emitted, skipped_old, skipped_win)
        return emitted

    def _set_baseline(self, frame: dict, units: list[dict], fps: list[str]) -> None:
        """把当前帧记为基线 —— 下次判断「新消息」的参照。

        基线、消息单元、滚动位置必须**同时**更新：滚动位置是「零重叠时算新内容
        高度」的参照，跟基线不是同一帧就会算出荒唐的 Δ。
        """
        self._prev_fps = fps
        self._prev_units = units
        self._prev_scroll = frame.get("scroll")
        self._prev_area_h = int(frame.get("area_h") or 0)
        self._recent.append(fps)

    def _recover_lost_anchor(self, frame: dict, units: list[dict],
                             fps: list[str]) -> tuple[list[int] | None, str]:
        """零重叠时的恢复：用滚动条几何判断这批内容是不是「新到达」的。

        典型故障（本群主要模式）：一条消息就是一张大表格，高度接近一屏，
        新消息一来上一条被**整条顶出视口** → 两帧内容零重叠 → 旧逻辑整帧
        丢弃并重建基线 → 新消息永久丢失（日志里「锚点丢失」刷屏即此）。

        判据：两帧都**贴底**说明屏幕上都是最新消息；此时底部下移了
        Δ = |max_cur| - |max_prev| 像素，新到达的内容就正好占当前可见内容
        最下面的 Δ 像素 —— 据此把「新消息」与「被顶上来但之前已见过的消息」
        干净分开，完全不依赖帧间内容是否重叠。

        返回 `(indexes, 说明)`；`(None, 原因)` 表示仍无法判定，本帧应丢弃。
        """
        cur = frame.get("scroll")
        if cur is None:
            return None, "读不到滚动条位置"
        if not at_bottom(cur):
            return None, "视口未贴底"
        prev = self._prev_scroll
        if prev is None or not at_bottom(prev):
            return None, "缺少「上次贴底」的参照"
        if int(frame.get("area_h") or 0) != self._prev_area_h:
            return None, "消息区高度变了（窗口被缩放）"
        if not units:
            return [], "本帧没识别到消息"

        cur_off, prev_off = scroll_offset(cur), scroll_offset(prev)
        if cur_off is None or prev_off is None:
            return None, "滚动位置不可读"
        delta = cur_off - prev_off
        if delta < 0:
            return None, f"内容反而变短 {abs(delta):.0f}px，参照已失效"

        seen: set[str] = set(self._prev_fps or ())
        for fs in self._recent:
            seen.update(fs)
        boundary = max(u["y1"] for u in units) - delta
        picked = [i for i, u in enumerate(units)
                  if u["y1"] > boundary and fps[i] not in seen]
        return picked, (f"视口贴底且底部下移 {delta:.0f}px，"
                        f"新内容占可见内容最下 {delta:.0f}px → 判定 {len(picked)} 条")

    def stop(self) -> None:
        self._stop.set()

    def __repr__(self) -> str:
        return f"<Source {self.name} interval={self.interval}s>"


def grab_frame_units(cfg) -> tuple[list[dict] | None, dict]:
    """「抓一帧 → 标题护栏 → 解析消息单元」，供各手动工具复用。

    返回 ``(units, info)``：

    * ``units`` —— `group_units` 的输出，**按屏幕顺序**（最后一个即最新一条）；
      自检不通过、截帧失败或护栏拦截时为 ``None``；
    * ``info``  —— 诊断信息：``ok`` / ``why`` / ``title`` / ``passed`` /
      ``ratio`` / ``matched``。

    标题护栏（「窗口不是配置里的目标群就拒绝转发」）刻意放在这里而不是各调用方：

    **手动转发没有「新消息」这层保护，一旦窗口切错群，会把别的群的对话原样发出去。**
    护栏必须与抓帧绑死，不能靠每个工具自己记得加。
    """
    src = DingTalkOCRSource(cfg)
    blank = {"title": "", "passed": False, "ratio": 0.0, "matched": "", "detail": ""}

    ok, detail = src.check()
    if not ok:
        return None, {**blank, "ok": False, "detail": detail,
                      "why": f"采集端自检未通过：{detail}"}

    frame = src._grab_frame()
    if frame is None:
        return None, {**blank, "ok": False, "detail": detail,
                      "why": "截帧失败（钉钉未启动 / 窗口最小化 / 定位不到消息区）"}

    title = " / ".join(frame["title"] or [])
    passed, ratio, matched = True, 1.0, ""
    if src.title_check:
        passed, ratio, matched = src._title_matches(frame["title"])
    info = {"ok": passed, "detail": detail,
            "why": "" if passed else "当前钉钉窗口不是配置里的目标会话，已拒绝转发",
            "title": title, "passed": passed, "ratio": ratio, "matched": matched}
    if not passed:
        return None, info

    units = group_units(merge_lines(frame["blocks"]), frame["area_w"])
    return units, info
