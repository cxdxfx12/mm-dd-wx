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
from collections import Counter
from datetime import datetime, timedelta
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

# 气泡内按钮等 UI 字样（精确匹配整行才丢弃）
UI_WORDS = {"复制", "下载", "编辑", "转发", "收藏", "提醒", "未查收", "已读", "已签收",
            "群签到", "水印拍照", "员工档案", "任务进度", "使用帮助"}
SIZE_RE = re.compile(r"^\d+(\.\d+)?\s*(KB|MB|GB|B)$", re.I)
DIGIT_GAP_RE = re.compile(r"(?<=\d) (?=\d)")          # 「77035 18」→「7703518」
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
        text = " ".join(p["text"] for p in parts)
        out.append({
            "y0": ln["y0"], "y1": ln["y1"],
            "x0": min(p["x0"] for p in parts),
            "x1": max(p["x1"] for p in parts),
            "text": DIGIT_GAP_RE.sub("", text),
            "kind": ln["parts"][0]["kind"],
            "kinds": [p["kind"] for p in parts],
        })
    return out


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
        })
    return out


def unit_fp(unit: dict) -> str:
    """消息单元指纹（发送者 + 内容），用作增量差分与锚点的稳定标识。"""
    return hashlib.md5(
        f"{unit['sender']}|{unit['content']}".encode("utf-8")).hexdigest()


def pick_new_units(units: list[dict], fps: list[str],
                   prev_fps: list[str] | None, anchor_guard: bool = True):
    """从本帧中挑出**真正的新消息**索引（防线 A：锚点定位）。

    原理：新消息只会出现在「锚点」的**下方**。滚动把历史消息带进视口时，
    它们必然落在锚点之上，因而不会被选中。

    锚点的选取对 OCR 抖动很宽容：从上一帧的最下面往上前进，取**第一条仍出现在
    本帧里**的消息作为锚。这样即使最后一条因 OCR 波动而指纹改变，也能退而用
    倒数第二条锚定，不会因为一次抖动就整帧丢弃。

    返回 `(indexes, new_anchor)`；`indexes` 为 `None` 表示本帧应整体丢弃
    ——上一帧没有任何一条消息仍留在本帧中，说明视口发生了大幅位移（多半被
    滚动了），此时差集里混着大量历史消息，宁可漏一帧也不误发历史。
    """
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
        self._prev_fps: list[str] | None = None   # 上一帧按屏幕顺序的指纹
        self._raw_seq: Counter = Counter()
        self._last_title_ok: bool | None = None

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

    def _get_ocr(self):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            self._ocr = RapidOCR()
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
        """消息区矩形（窗口相对坐标）。返回 (left, top, right, bottom) 或 None。"""
        for cls in (MSG_AREA_CLASS, CHAT_BOX_CLASS):
            ctrl = wnd.Control(ClassName=cls, searchDepth=14)
            try:
                if not ctrl.Exists(0.5):
                    continue
                r = ctrl.BoundingRectangle
                wr = wnd.BoundingRectangle
                rect = (r.left - wr.left, r.top - wr.top, r.right - wr.left, r.bottom - wr.top)
                if rect[2] - rect[0] > 100 and rect[3] - rect[1] > 100:
                    return rect, cls
            except Exception:
                continue
        return None, ""

    # ---------- 每帧处理 ----------

    def _read_title(self, wnd, msg_rect) -> str:
        """OCR 窗口标题条（聊天面板顶部），返回原始文本。"""
        import numpy as np
        from PIL import Image

        wr = wnd.BoundingRectangle
        w = wr.right - wr.left
        left, top = msg_rect[0], msg_rect[1]
        strip = (left, max(20, top - 64), w, max(top - 4, top - 64 + 10))
        img = self._last_frame.crop(strip)
        img = img.resize((int(img.width * 2), int(img.height * 2)),
                         Image.LANCZOS)
        res, _ = self._get_ocr()(np.array(img))
        return "".join(x[1] for x in (res or []))

    def _title_matches(self, title: str) -> tuple[bool, float, str]:
        """标题与 chat_names 之一匹配则通过。

        两层判定（取更宽松的一个）：
          1. **包含**：标题里直接含有配置名（设置中心保存的往往是群名片段，
             如「数字质控」，必须走这条才判得中）；
          2. **模糊**：OCR 会认错字，用相似度兜底（实测误读「综学国控」
             对全名相似度 0.65 仍应通过）。
        """
        names = [n for n in (self.dt.chat_names or []) if n]
        if not names:
            return True, 1.0, ""
        norm = re.sub(r"[\s【】\[\]（）()·\-—_]", "", title)
        best_name, best = "", 0.0
        for n in names:
            nn = re.sub(r"[\s【】\[\]（）()·\-—_]", "", n)
            if nn and nn in norm:
                return True, 1.0, n
            ratio = difflib.SequenceMatcher(None, norm, nn).ratio()
            if ratio > best:
                best_name, best = n, ratio
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

        msg_rect, hit = self._find_msg_rect(wnd)
        if msg_rect is None:
            log.warning("定位不到钉钉消息区控件")
            return None

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
        res, _ = self._get_ocr()(arr)
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

        title = self._read_title(wnd, msg_rect) if self.title_check else ""
        return {"area_w": aw, "area_h": ah, "blocks": blocks, "title": title}

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
            return False, f"OCR 引擎不可用：{exc}（pip install rapidocr-onnxruntime）"
        wnd = self._find_window()
        if wnd is None:
            return False, "未找到钉钉主窗口，请确认客户端已启动并登录"
        rect, hit = self._find_msg_rect(wnd)
        if rect is None:
            return False, "定位不到消息区控件（ChatBubbleWidget），钉钉版本可能不兼容"
        return True, f"钉钉窗口与消息区定位正常（{hit}），OCR 引擎就绪"

    def run(self, on_message) -> None:
        chat_names = list(self.dt.chat_names or [])
        log.info("OCR 轮询启动，间隔 %.1fs，监控会话：%s", self.interval, chat_names)
        poll = max(self.interval, 4.0)
        while not self._stop.is_set():
            started = time.time()
            try:
                self._poll_once(on_message)
            except Exception as exc:
                log.exception("OCR 轮询异常：%s", exc)
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
                log.info("会话标题校验：%s（相似度 %.2f，标题=%r）",
                         "通过" if ok else "不通过——本帧不转发", ratio, frame["title"])
                self._last_title_ok = ok
            if not ok:
                # 切回来时重新对齐基线，避免把旧帧当新消息
                self._prev_fps = None
                return
            chat_name = matched or (self.dt.chat_names or [""])[0]
        else:
            chat_name = (self.dt.chat_names or [""])[0]

        lines = merge_lines(frame["blocks"])
        units = group_units(lines, frame["area_w"])
        fps = [unit_fp(u) for u in units]

        if self._prev_fps is None:
            # 首帧只建立基线，不转发（历史消息不倒灌）
            self._prev_fps = fps
            log.info("基线帧建立：当前可见 %d 条消息（不转发历史）", len(units))
            return

        # 防线 A：锚点定位。picked 为 None = 视口大幅位移，本帧整体丢弃。
        picked, _ = pick_new_units(units, fps, self._prev_fps, self.anchor_guard)
        self._prev_fps = fps               # 基线 = 当前帧，天然支持重复内容
        if picked is None:
            log.warning("锚点丢失（聊天区可能被滚动）：本帧不转发，已重建基线")
            return
        if not picked:
            return

        # 防线 B：时间维度过滤
        #   today  -> 丢弃被钉钉标注为往日（昨天/星期X/MM-DD）的消息
        #   window -> 在 today 的基础上，再要求落在配置的时段内
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

    def stop(self) -> None:
        self._stop.set()

    def __repr__(self) -> str:
        return f"<Source {self.name} interval={self.interval}s>"
