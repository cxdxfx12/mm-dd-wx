# -*- coding: utf-8 -*-
"""微信 4.x 投递端 —— **OCR + 模拟点击**（不依赖 UIA 控件树）。

## 为什么需要这个实现（2026-10-18 现场）

微信 4.x 从某天起不再向 UI Automation 暴露 `mmui::*` 控件树：主窗口在 Win32
层退化成 `Qt51514QWindowIcon`（Qt 给「没有自己窗口类」的顶层窗起的兜底名），
子节点只剩一个 1×1 占位窗和一个空的 `MMUIRenderSubWindowHW`。

排除过的方向（全部实测无效，别再重查）：
换 4.1.15.13 → 4.1.15.53、换全新安装路径、重装、重启电脑、切输入法、
`QT_ACCESSIBILITY=1`、点焦点、改窗口大小、DPI/多屏 —— 一律空壳。
**界面渲染完全正常，只是控件树不存在**，所以任何依赖 UIA 的方案都无解。

## 本实现的原理

微信界面既然能画出来，就用「眼睛 + 手」代替「读控件」：

1. `PrintWindow(PW_RENDERFULLCONTENT)` 抓窗口位图 —— **不抢焦点**，
   被其他窗口遮挡也能截（所以钉钉可以一直停在前台做采集）；
2. RapidOCR（PP-OCRv6）识别左侧会话列表，按文本模糊匹配找到目标群，
   拿到它在**截图坐标系**里的位置；
3. 换算成屏幕坐标（窗口矩形 + 截图坐标；本机 DPI 100%，1:1），
   `SetCursorPos` + `mouse_event` 点开会话；
4. 点输入框 → 剪贴板粘贴（绕开中文输入法）→ 回车发送；
5. 回读截图做校验：消息区**左下角**出现绿色气泡即视为成功。

## 关键实现约束（踩过的坑，改动前必读）

- **`GlobalLock`/`GlobalUnlock` 必须显式声明 `restype=ctypes.c_void_p`**，
  否则 64 位下句柄被截断成 int，`memmove` 直接 `access violation`。
- **剪贴板优先级**：`wechat.ocr.keep_clipboard=True`（默认）时先备份并恢复
  用户原有剪贴板内容，避免把用户正在复制的东西洗掉。
- **坐标必须按窗口尺寸比例换算**，不能写死像素：窗口被拉大拉小后
  (720×1088) 那组坐标就全错。比例基准见 `_LAYOUT`。
- **会话列表裁切区固定为窗口左侧**：`x ∈ [0.09W, 0.43W]`，`y ∈ [0.035H, H]`。
  列表项高度随字号变，所以只在**列表区**里做 OCR，不在整窗做。
- **发送后不清空输入框是危险的**：若发送失败，内容会留在输入框里，
  下一条消息粘上去就变成两条拼一起。`_send` 结束前必须确认输入框已空。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import time
from pathlib import Path

from .base import SendResult, Target

log = logging.getLogger("bridge.targets.wechat_ocr")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------------------
# Win32 函数签名 —— 集中声明，只此一处
#
# ⚠ 64 位下**所有返回句柄/指针的函数都必须显式 `restype = ctypes.c_void_p`**。
# ctypes 默认把返回值当 C `int`（32 位），高 32 位被静默截断：
#   HGLOBAL 0x248f72794b0 → 0x48f72794b0 → GlobalLock 锁到野地址 → access violation
# 本模块已因此崩过两次（GlobalAlloc/GlobalLock、GetClipboardData），改动时不要删。
# ---------------------------------------------------------------------------
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
user32.IsClipboardFormatAvailable.restype = wt.BOOL
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.restype = wt.BOOL
user32.CloseClipboard.restype = wt.BOOL
user32.EmptyClipboard.restype = wt.BOOL

kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p

PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
VK_A = 0x41
VK_BACK = 0x08
VK_ESCAPE = 0x1B

# 微信主窗口在 Win32 层的标题（类名带 Qt 版本号，不能写死）
MAIN_TITLES = ("微信", "WeChat")

# 各区域相对窗口尺寸的比例（基准窗口 720×1088 实测标定）
_LAYOUT = {
    # 会话列表：左侧栏。微信 4.x 的列表宽度约占窗口 34%
    "list": (0.092, 0.037, 0.430, 1.000),
    # 搜索框（左上角「🔍 搜索」）—— 1:1 截图实测框体 x∈[85,260]、y∈[45,72]，
    # 中心 (172,58) → 比例 (0.24, 0.053)。
    # ⚠ 历史坑：旧值 (0.27, 0.068) 的点击点 (194,74) 落在框**下沿外 2 像素**
    # 的缝隙里，永远点不中且无任何报错；后来试的 (0.119,0.026) 又落在标题栏。
    # 改这个值前必须先 1:1 截图量准，别用放大图估。
    "search": (0.24, 0.053),
    # 右侧聊天区标题栏（回读校验「当前打开的是哪个群」）：
    # 720 宽实测标题在 y∈[38,72]，避开 y<32 的系统按钮行
    "title": (0.45, 0.034, 0.90, 0.068),
    # 输入框点击点（相对比例）
    "input": (0.70, 0.90),
    # 发送按钮（相对比例，点不到时回落到回车）
    "send_btn": (0.925, 0.968),
    # 消息区（回读校验用）：右侧面板，不含标题栏
    "message": (0.44, 0.06, 1.00, 0.94),
}


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wt.DWORD * 3)]


class WeChatOcrTarget(Target):
    """用 OCR + 模拟点击往微信群里发消息。"""

    name = "wechat_ocr"

    def __init__(
        self,
        dry_run: bool = True,
        verify: bool = True,
        keep_clipboard: bool = True,
        match_threshold: float = 0.62,
        focus_delay: float = 0.35,
        send_delay: float = 0.8,
    ):
        self.dry_run = dry_run
        self.verify = verify
        self.keep_clipboard = keep_clipboard
        self.match_threshold = match_threshold
        self.focus_delay = focus_delay
        self.send_delay = send_delay
        self._engine = None
        self._hwnd = 0
        self._current = ""          # 当前已打开的会话名（避免重复点击）

    # ---------- 生命周期 ----------

    def close(self) -> None:
        self._hwnd = 0
        self._current = ""

    def __repr__(self) -> str:
        return f"<WeChatOcrTarget dry_run={self.dry_run}>"

    # ---------- 对外接口 ----------

    def send_text(self, target: str, text: str) -> SendResult:
        if not target:
            return SendResult(False, target, "目标为空")
        if not text:
            return SendResult(False, target, "内容为空")

        if self.dry_run:
            print(f"[dry-run] 发送到「{target}」:\n{text}\n{'-' * 40}")
            return SendResult(True, target, "dry-run 未真实发送")

        try:
            return self._send_live(target, text)
        except Exception as exc:
            log.exception("OCR 投递失败")
            return SendResult(False, target, f"{type(exc).__name__}: {exc}")

    def health_check(self) -> tuple[bool, str]:
        """只读探活：找窗口 → 截图 → OCR 列表 → 看能不能认出会话。"""
        try:
            hwnd = self._find_window()
        except Exception as exc:
            return False, f"查找微信窗口失败：{type(exc).__name__}: {exc}"
        if not hwnd:
            return False, (
                "未找到微信主窗口（标题「微信」/「WeChat」）—— 请确认微信已启动并登录，"
                "且主窗口没有被收进托盘（点任务栏图标打开）")
        try:
            img = self._grab(hwnd)
        except Exception as exc:
            return False, f"抓取微信窗口失败：{type(exc).__name__}: {exc}"
        if img is None:
            return False, (
                "抓不到微信窗口画面（PrintWindow 返回空）—— 窗口可能被最小化了，"
                "请还原窗口后重试")
        try:
            items = self._ocr_sessions(img)
        except Exception as exc:
            return False, f"OCR 引擎不可用：{type(exc).__name__}: {exc}"
        if not items:
            return False, (
                "会话列表 OCR 结果为空 —— 可能是窗口太窄、列表被折叠，"
                "或微信停在了「通讯录」等非消息页；切回「微信」页后重试")
        names = [it["text"] for it in items][:3]
        return True, (
            f"微信窗口可读（OCR 识别到 {len(items)} 项会话，"
            f"如：{'、'.join(names)}）")

    # ---------- 主流程 ----------

    def _send_live(self, target: str, text: str) -> SendResult:
        hwnd = self._find_window()
        if not hwnd:
            return SendResult(False, target, "未找到微信主窗口，请确认微信已登录且在桌面上")

        if not self._activate(hwnd):
            return SendResult(False, target, "无法把微信窗口切到前台（可能被系统禁止抢占焦点）")

        # 1. 切到目标会话
        ok, detail = self._open_chat(hwnd, target)
        if not ok:
            return SendResult(False, target, detail)

        # 2. 点输入框并粘贴
        if not self._click(hwnd, *_LAYOUT["input"]):
            return SendResult(False, target, "点击输入框失败")
        time.sleep(0.2)

        self._clear_input()
        if not self._paste(text):
            return SendResult(False, target, "写入剪贴板或粘贴失败")
        time.sleep(0.35)

        # 3. 发送：优先点「发送」按钮，点不到就回车
        if not self._click(hwnd, *_LAYOUT["send_btn"]):
            self._press(VK_RETURN)
        time.sleep(self.send_delay)

        # 4. 校验
        if not self.verify:
            return SendResult(True, target, "已发送（未校验）")

        verified, why = self._verify_sent(hwnd, text)
        if verified:
            return SendResult(True, target, why)
        # 兜底清输入框：**必须先确认会话还开着**。
        # 会话没开时点输入框区域 = 点在空白处，而 Ctrl+A/Backspace 会落到
        # 会话列表上，把列表滚动/状态弄乱（2026-10-18 实测踩过）。
        opened, _title = self._current_chat_title(hwnd)
        if opened:
            self._click(hwnd, *_LAYOUT["input"])
            self._clear_input()
        return SendResult(False, target, why)

    def _open_chat(self, hwnd: int, target: str) -> tuple[bool, str]:
        """在会话列表里找到目标群并点开。列表里没有就改用搜索框。

        ⚠ 点击后必须用标题栏确认会话真的打开了：本机微信对模拟点击
        偶尔不响应（点搜索框也遇到过），只信「点了」会静默丢消息。
        """
        img, items = self._snapshot(hwnd)
        if img is None:
            return False, (
                "抓不到微信窗口画面（窗口可能已最小化或收进托盘）——"
                "请点任务栏微信图标还原窗口后重试")
        if not items:
            return False, "会话列表 OCR 为空，无法定位目标群"

        hit, score = self._best_match(target, items)
        if hit is None:
            # ---- 列表里没有（未在首屏）→ 用搜索框 ----
            log.info("会话列表首屏未见「%s」（最高 %.2f），改用搜索框", target, score)
            ok, why = self._search_chat(hwnd, target)
            if ok:
                return True, why
            sample = "、".join(it["text"] for it in items
                               if it.get("kind") == "session")[:5] or "（无）"
            return False, (
                f"会话列表首屏没找到「{target}」（最高相似度 {score:.2f} < "
                f"{self.match_threshold}），搜索也没成功：{why}。"
                f"当前可见会话：{sample}")

        if self._current == hit["text"]:
            opened, title = self._current_chat_title(hwnd)
            if opened and self._title_matches(target, title):
                return True, "已在目标会话"

        # 点击（最多两次：本机微信偶尔吞掉第一次模拟点击）
        last_title = ""
        for attempt in (1, 2):
            if not self._click_abs(hwnd, hit["cx"], hit["cy"]):
                return False, f"点击会话「{hit['text']}」失败"
            time.sleep(0.6)
            opened, title = self._current_chat_title(hwnd)
            last_title = title
            if opened and self._title_matches(target, title):
                self._current = hit["text"]
                return True, (
                    f"已切到「{hit['text']}」（相似度 {score:.2f}，"
                    f"第 {attempt} 次点击，标题栏：{title[:20]}）")
            log.warning("点击会话后标题栏未确认（第 %d 次）：%r",
                        attempt, title)
        return False, (
            f"点击「{hit['text']}」后标题栏未出现目标会话"
            f"（标题栏读到：{last_title or '空'}）—— 右侧聊天区可能没有会话打开")

    @staticmethod
    def _title_matches(target: str, title: str) -> bool:
        """标题栏文本是否指向目标会话（群名可能带人数后缀/被截断）。"""
        import re
        t = re.sub(r"\s+", "", target)
        s = re.sub(r"\s+", "", title)
        s = re.sub(r"\(\d+\)$", "", s)           # 「群名 (84)」
        return bool(t) and (t in s or s[:len(t)] in t)

    def _search_chat(self, hwnd: int, target: str) -> tuple[bool, str]:
        """点搜索框 → 输入群名 → 回车选第一条 → 标题栏确认。

        ⚠ 两个坑（都在 2026-10-18 实测踩过）：
        1. 搜索框坐标必须 1:1 截图量准，偏 2 像素就是永远无响应；
        2. 搜索页里**搜索框自己**和「搜索框回显+网络结果」的混合行
           OCR 出来和目标一模一样（score 1.0），直接 OCR 匹配会点错
           （点回搜索框 → 假成功）。
           所以首选**粘贴后按回车**（微信会打开第一条精确匹配），
           回车失败才回落到 OCR 定位结果项，且匹配时排除搜索框区域
           （cy > 0.075H），最后必须标题栏确认才算成功。
        """
        if not self._click(hwnd, *_LAYOUT["search"]):
            return False, "点击搜索框失败"
        time.sleep(0.45)
        self._clear_input()
        if not self._paste(target):
            return False, "搜索词粘贴失败"
        time.sleep(0.9)                      # 等搜索结果返回

        # ---- 首选：回车选第一条结果 ----
        self._press(VK_RETURN)
        time.sleep(1.2)
        opened, title = self._current_chat_title(hwnd)
        if opened and self._title_matches(target, title):
            self._current = target
            return True, f"已通过搜索（回车）切到目标（标题栏：{title[:24]}）"

        log.info("回车未切到目标（标题栏 %r），回落到 OCR 点结果项", title)

        # ---- 回落：OCR 定位结果项（排除搜索框区域） ----
        img, items = self._snapshot(hwnd)
        if img is None:
            return False, "搜索结果页抓帧失败"

        img_h = img.size[1]
        below_box = [it for it in items
                     if it.get("cy", 0) > img_h * 0.075]
        if not below_box:
            self._press(VK_ESCAPE)
            return False, "搜索结果里没有可点的条目（只看到搜索框）"

        hit, score = self._best_match(target, below_box)
        if hit is None:
            self._press(VK_ESCAPE)
            return False, f"搜索结果里没认出目标群（最高 {score:.2f}）"

        if not self._click_abs(hwnd, hit["cx"], hit["cy"]):
            self._press(VK_ESCAPE)
            return False, f"点击搜索结果「{hit['text']}」失败"
        self._current = hit["text"]

        # 核对：结果页切换有延迟，重试读标题栏
        title_txt = ""
        for _ in range(3):
            time.sleep(0.6)
            ok, title_txt = self._current_chat_title(hwnd)
            if ok and self._title_matches(target, title_txt):
                return True, (
                    f"已通过搜索（OCR 定位）切到目标（标题栏：{title_txt[:24]}）")
        return False, (
            f"点击搜索结果「{hit['text']}」后标题栏未确认到目标"
            f"（读到：{title_txt or '空'}）")

    def _current_chat_title(self, hwnd: int) -> tuple[bool, str]:
        """OCR 右侧聊天区标题栏，确认当前打开的会话。"""
        import numpy as np

        img = self._grab(hwnd)
        if img is None:
            return False, "抓帧失败"
        W, H = img.size
        x0, y0, x1, y1 = _LAYOUT["title"]
        crop = img.crop((int(W * x0), int(H * y0), int(W * x1), int(H * y1)))
        if crop.width < 20 or crop.height < 10:
            return False, "标题栏区域过小"
        arr = np.array(crop.resize((crop.width * 3, crop.height * 3)))
        texts = []
        for b, text, conf in self._ocr(arr):
            text = (text or "").strip()
            if text and float(conf or 0) >= 0.6:
                texts.append(text)
        return (True, " ".join(texts)) if texts else (False, "标题栏无文字")

    def _verify_sent(self, hwnd: int, text: str) -> tuple[bool, str]:
        """回读校验：消息区左下角出现绿色气泡，且能 OCR 到正文。"""
        expect = text.replace("\n", " ").strip()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            img = self._grab(hwnd)
            if img is not None and self._has_outgoing_bubble(img):
                return True, "已发送（检测到发出气泡）"
            time.sleep(0.4)
        return False, (
            "发送后未检测到发出的消息气泡，请人工确认"
            f"（预期内容：{expect[:30]!r}）")

    # ---------- 窗口 ----------

    def _find_window(self) -> int:
        """按标题找微信主窗口（含最小化，不含隐藏）。"""
        if self._hwnd and user32.IsWindow(wt.HWND(self._hwnd)):
            r = self._rect(self._hwnd)
            if r and r[2] - r[0] > 200:
                return self._hwnd

        found = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(h, _l):
            n = user32.GetWindowTextLengthW(h)
            if not n:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if buf.value not in MAIN_TITLES:
                return True
            if not user32.IsWindowVisible(h):
                return True
            r = self._rect(h)
            # 排除登录小窗（176×199）和其他挂件
            if r and (r[2] - r[0]) > 400 and (r[3] - r[1]) > 400:
                found.append(h)
            return True

        user32.EnumWindows(cb, 0)
        if not found:
            return 0
        self._hwnd = found[0]
        return self._hwnd

    @staticmethod
    def _rect(hwnd: int):
        r = wt.RECT()
        if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
            return None
        return r.left, r.top, r.right, r.bottom

    def _activate(self, hwnd: int) -> bool:
        """把微信切到前台并确保可见。

        微信 4.x 有三种「看不见」形态，全部要处理：
        1. 最小化（IsIconic）→ SW_RESTORE；
        2. 收进托盘（IsWindowVisible=0、非最小化）→ 按句柄 SW_SHOW
          （不能靠热键，那是个开关，隐藏时唤不回）；
        3. 正常可见 → 只补前台。
        ⚠ PrintWindow 在窗口不可见时抓出来是**纯黑图**，OCR 全空，
        所以发送前必须确认 IsWindowVisible。
        """
        try:
            if not user32.IsWindowVisible(wt.HWND(hwnd)):
                if user32.IsIconic(wt.HWND(hwnd)):
                    user32.ShowWindow(wt.HWND(hwnd), 9)      # SW_RESTORE
                else:
                    user32.ShowWindow(wt.HWND(hwnd), 5)      # SW_SHOW
                time.sleep(0.4)
            if user32.IsIconic(wt.HWND(hwnd)):
                user32.ShowWindow(wt.HWND(hwnd), 9)          # SW_RESTORE
                time.sleep(0.3)
            user32.SetForegroundWindow(wt.HWND(hwnd))
            time.sleep(self.focus_delay)
            if not user32.IsWindowVisible(wt.HWND(hwnd)):
                log.error("微信窗口 SW_SHOW 后仍不可见，画面会抓成黑图")
                return False
            return True
        except Exception as exc:
            log.warning("激活微信窗口失败：%s", exc)
            return False

    # ---------- 截图 ----------

    def _grab(self, hwnd: int):
        """抓整窗画面，返回 PIL.Image；不抢焦点（背景模式）。

        ⚠ **PrintWindow 在这台机器的微信 4.x 上会返回「冻结的旧帧」**：
        2026-10-18 实测——真实点击后屏幕内容（ImageGrab 已变）与 PrintWindow
        返回的位图（md5 不变）不一致，微信的 Qt 渲染走 GPU 合成，
        不响应 PrintWindow 的重绘请求。**所以这里用 ImageGrab 抓窗口矩形**，
        前提是窗口没被遮挡 —— `_activate()` 已确保微信在前台。
        若日后要做「完全不抢前台」，需要另找 GPU 友好的抓帧路径（如
        Windows.Graphics.Capture），别改回 PrintWindow。
        """
        from PIL import Image, ImageGrab

        r = self._rect(hwnd)
        if not r:
            return None
        w, h = r[2] - r[0], r[3] - r[1]
        if w <= 0 or h <= 0:
            return None
        try:
            return ImageGrab.grab(bbox=r, all_screens=False)
        except Exception as exc:
            log.warning("ImageGrab 抓窗失败（%s），回退 PrintWindow", exc)
        # ---- 回退：PrintWindow（仅当 ImageGrab 不可用时） ----
        hwnd_dc = user32.GetWindowDC(wt.HWND(hwnd))
        mem_dc = ctypes.windll.gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = ctypes.windll.gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        old = ctypes.windll.gdi32.SelectObject(mem_dc, bmp)
        try:
            if not user32.PrintWindow(wt.HWND(hwnd), mem_dc, PW_RENDERFULLCONTENT):
                return None
            bmi = _BitmapInfo()
            bmi.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h          # 负值 = 自上而下
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            buf = ctypes.create_string_buffer(w * h * 4)
            if ctypes.windll.gdi32.GetDIBits(mem_dc, bmp, 0, h, buf,
                                             ctypes.byref(bmi), DIB_RGB_COLORS) == 0:
                return None
            return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()
        finally:
            ctypes.windll.gdi32.SelectObject(mem_dc, old)
            ctypes.windll.gdi32.DeleteObject(bmp)
            ctypes.windll.gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(wt.HWND(hwnd), hwnd_dc)

    # ---------- OCR ----------

    def _snapshot(self, hwnd: int):
        """抓帧 + OCR 会话列表，一次完成。返回 (img, items)。"""
        img = self._grab(hwnd)
        if img is None:
            return None, []
        return img, self._ocr_sessions(img)

    def _ocr(self, arr):
        """跑一次 OCR。引擎懒加载并缓存（首次加载模型约 1-2 秒）。

        ⚠ 缓存字段必须叫 `_engine`，不能叫 `_ocr` —— 那会和本方法同名，
        实例属性把方法覆盖掉，调用时报 `'NoneType' object is not callable`。
        """
        if self._engine is None:
            from ..sources.dingtalk_ocr import _build_ocr_engine
            self._engine = _build_ocr_engine()
        return self._engine(arr)

    def _ocr_sessions(self, img) -> list[dict]:
        """OCR 左侧会话列表。

        同一个会话在列表里是**两行**（第一行群名 + 时间，第二行最后一条消息
        摘要），OCR 会分别识别成两条，直接拿单条文本去匹配会把「群名 + 时间」
        当成一个奇怪的名字。所以这里按 y 聚成「行组」：先输出全部原始项
        （`kind="raw"`），再输出合并项（`kind="session"`，`text` = 行内拼接，
        `cx/cy` 取行中心）—— 点击/匹配都优先用 session 项。

        坐标一律是**截图内**像素。
        """
        import numpy as np

        W, H = img.size
        x0, y0, x1, y1 = _LAYOUT["list"]
        box = (int(W * x0), int(H * y0), int(W * x1), int(H * y1))
        crop = img.crop(box)
        if crop.width < 20 or crop.height < 20:
            return []
        # 2x 放大：小字号中文在这个倍率下识别率最高（钉钉侧同参数）
        arr = np.array(crop.resize((crop.width * 2, crop.height * 2)))
        items = self._ocr(arr)

        raw = []
        for b, text, conf in items:
            text = (text or "").strip()
            if not text:
                continue
            # ⚠ box 通常是 numpy 数组，`if b:` 会抛
            # "truth value of an array is ambiguous" —— 必须判 len()
            if b is None or len(b) == 0:
                continue
            xs = [float(p[0]) for p in b]
            ys = [float(p[1]) for p in b]
            if not xs or not ys:
                continue
            # 2x 缩放 → 回到裁切坐标 → 回到整图坐标
            raw.append({
                "text": text,
                "kind": "raw",
                "x0": min(xs) / 2.0 + box[0],
                "x1": max(xs) / 2.0 + box[0],
                "y0": min(ys) / 2.0 + box[1],
                "y1": max(ys) / 2.0 + box[1],
                "conf": float(conf or 0),
            })
        if not raw:
            return []

        raw.sort(key=lambda d: (d["y0"], d["x0"]))
        for d in raw:
            d["cx"] = (d["x0"] + d["x1"]) / 2.0
            d["cy"] = (d["y0"] + d["y1"]) / 2.0
            d["y"] = d["cy"]

        # ---- 聚行 ----
        heights = sorted(d["y1"] - d["y0"] for d in raw)
        line_h = heights[len(heights) // 2] or 20.0
        groups: list[list[dict]] = []
        for d in raw:
            placed = False
            for g in groups:
                cy = sum(x["cy"] for x in g) / len(g)
                if abs(d["cy"] - cy) <= line_h * 0.6:
                    g.append(d)
                    placed = True
                    break
            if not placed:
                groups.append([d])

        sessions = []
        for g in groups:
            g.sort(key=lambda d: d["x0"])
            sessions.append({
                "text": " ".join(x["text"] for x in g),
                "kind": "session",
                "parts": [x["text"] for x in g],
                "x0": min(x["x0"] for x in g),
                "x1": max(x["x1"] for x in g),
                "y0": min(x["y0"] for x in g),
                "y1": max(x["y1"] for x in g),
                "cx": (min(x["x0"] for x in g) + max(x["x1"] for x in g)) / 2.0,
                "cy": sum(x["cy"] for x in g) / len(g),
                "conf": max(x["conf"] for x in g),
            })
        for s in sessions:
            s["y"] = s["cy"]
        sessions.sort(key=lambda d: d["y"])

        # raw 在前、session 在后：调用方按 kind 自己筛
        return raw + sessions

    def _best_match(self, target: str, items: list[dict]):
        """在 OCR 结果里找最像目标群的项，返回 (item, score)。

        只比 `kind == "session"` 的行组：群名常被截断成
        「申通五星业务员培训...」，行组里同时含群名和时间，
        逐行比对才能把「群名 + 时间」整体对上。
        """
        import difflib
        import re

        def norm(s: str) -> str:
            # 去掉时长/时间戳/省略号/未读条数，只留名称
            s = re.sub(r"\s+", "", s)
            s = re.sub(r"^\[\d+条\]", "", s)
            s = re.sub(r"(下午|上午|昨天|星期[一二三四五六日天]|"
                       r"\d{1,2}[:：]\d{2}|\d{1,2}/\d{1,2}|\d+月\d+日|"
                       r"\.{2,}|…+)", "", s)
            return s

        tgt = norm(target)
        pool = [it for it in items if it.get("kind") == "session"] or items
        best, best_score = None, 0.0
        for it in pool:
            name = norm(it["text"])
            if not name:
                continue
            # 会话名常被截断成「申通五星业务员培训...」，所以双向包含都算命中
            if tgt and tgt in name:
                score = 1.0
            elif tgt and name in tgt:
                score = 0.95
            else:
                score = difflib.SequenceMatcher(None, tgt, name).ratio()
            if score > best_score:
                best, best_score = it, score
        if best_score >= self.match_threshold:
            return best, best_score
        return None, best_score

    # ---------- 输入 ----------

    def _click(self, hwnd: int, rx: float, ry: float) -> bool:
        """按窗口内相对比例点击。"""
        r = self._rect(hwnd)
        if not r:
            return False
        w, h = r[2] - r[0], r[3] - r[1]
        return self._click_screen(r[0] + int(w * rx), r[1] + int(h * ry))

    def _click_abs(self, hwnd: int, cx: float, cy: float) -> bool:
        """按截图内绝对像素点击（OCR 结果用这个）。"""
        r = self._rect(hwnd)
        if not r:
            return False
        return self._click_screen(r[0] + int(cx), r[1] + int(cy))

    @staticmethod
    def _click_screen(x: int, y: int) -> bool:
        try:
            user32.SetCursorPos(int(x), int(y))
            time.sleep(0.08)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return True
        except Exception as exc:
            log.debug("点击 (%s,%s) 失败：%s", x, y, exc)
            return False

    @staticmethod
    def _press(vk: int, ctrl: bool = False) -> None:
        """发一次按键（可带 Ctrl）。"""
        if ctrl:
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            time.sleep(0.04)
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(vk, 0, 2, 0)          # 2 = KEYEVENTF_KEYUP
        if ctrl:
            user32.keybd_event(VK_CONTROL, 0, 2, 0)

    def _clear_input(self) -> None:
        """清空输入框（Ctrl+A → Backspace），避免上一条残留拼接到下一条。"""
        self._press(VK_A, ctrl=True)
        time.sleep(0.05)
        self._press(VK_BACK)

    def _paste(self, text: str) -> bool:
        """把文本写进剪贴板 → Ctrl+V → 还原用户原有剪贴板。

        还原放在粘贴**之后**，因为 Ctrl+V 是异步投递到微信消息队列的，
        立即还原会和读取剪贴板抢时序。
        """
        backup = self._get_clipboard() if self.keep_clipboard else None
        if not self._put_clipboard(text):
            return False
        time.sleep(0.1)
        self._press(VK_V, ctrl=True)
        time.sleep(0.06)                       # 让微信先把剪贴板读完
        if backup is not None and backup != text:
            self._set_clipboard_raw(backup)
        return True

    def _put_clipboard(self, text: str) -> bool:
        """写剪贴板（不备份、不还原，只负责把 text 放进去）。"""
        if not user32.OpenClipboard(0):
            return False
        try:
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, (len(text) + 1) * 2)
            if not h:
                return False
            p = kernel32.GlobalLock(h)
            if not p:
                kernel32.GlobalFree(h)
                return False
            ctypes.memmove(p, ctypes.create_unicode_buffer(text), (len(text) + 1) * 2)
            kernel32.GlobalUnlock(h)
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(h)):
                kernel32.GlobalFree(h)          # 转移失败自己释放，否则泄漏
                return False
        finally:
            user32.CloseClipboard()
        return True

    def _get_clipboard(self):
        """读剪贴板文本；没有文本内容时返回 None。

        ⚠ 这里是本模块第一个 64 位陷阱的现场：`GetClipboardData` 的返回值是
        HGLOBAL（64 位指针），签名见模块顶部统一声明。
        """
        if not user32.OpenClipboard(0):
            return None
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return None
            p = kernel32.GlobalLock(h)
            if not p:
                return None
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()

    def _set_clipboard_raw(self, text: str) -> None:
        """还原剪贴板内容（不备份，避免递归）。"""
        if not user32.OpenClipboard(0):
            return
        try:
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, (len(text) + 1) * 2)
            if not h:
                return
            p = kernel32.GlobalLock(h)
            if not p:
                kernel32.GlobalFree(h)
                return
            ctypes.memmove(p, ctypes.create_unicode_buffer(text), (len(text) + 1) * 2)
            kernel32.GlobalUnlock(h)
            user32.EmptyClipboard()
            # ⚠ 成功后所有权交给系统，不能再 GlobalFree
            user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(h))
        finally:
            user32.CloseClipboard()

    # ---------- 回读校验 ----------

    @staticmethod
    def _has_outgoing_bubble(img) -> bool:
        """消息区里是否出现「我方发出的绿色气泡」。

        微信浅色主题下自己发的消息是 #95EC69 附近的绿；深色主题是暗绿。
        判据取「绿色通道显著高于红蓝 + 足够饱和」，对两种主题都成立。
        """
        import numpy as np

        W, H = img.size
        x0, y0, x1, y1 = _LAYOUT["message"]
        crop = np.array(img.crop((int(W * x0), int(H * y0),
                                  int(W * x1), int(H * y1))))
        if crop.size == 0:
            return False
        r = crop[:, :, 0].astype(int)
        g = crop[:, :, 1].astype(int)
        b = crop[:, :, 2].astype(int)
        # 浅色主题：#95EC69 → g 明显最大且偏亮
        light = (g > 150) & (g - r > 25) & (g - b > 60)
        # 深色主题：#3EB575 一类 → 中等亮度
        dark = (g > 90) & (g - r > 30) & (g - b > 40)
        green_px = int(np.count_nonzero(light | dark))
        # 一个气泡至少几百像素；阈值取宽度的 0.6%（720 宽 ≈ 4300 是气泡量级，
        # 这里放宽到 800 减少误判同时保留灵敏度）
        return green_px >= max(800, int(crop.shape[1] * 1.2))
