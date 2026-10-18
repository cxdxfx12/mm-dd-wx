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

# ── 遮挡检测（`_occluded_points`）──
# WindowFromPoint 收的是**按值传的 POINT 结构体**，不是指针；不声明 argtypes
# 时 ctypes 会把两个 int 当两个独立参数传，栈上的值会被读错 → 拿到的 hwnd
# 是随机的，表现为「随机说被遮挡」。这几个签名必须写死。
class _POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


user32.WindowFromPoint.argtypes = [_POINT]
user32.WindowFromPoint.restype = ctypes.c_void_p
user32.GetAncestor.argtypes = [ctypes.c_void_p, wt.UINT]
user32.GetAncestor.restype = ctypes.c_void_p
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
user32.BringWindowToTop.restype = wt.BOOL
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
# 硬抢前台用（_grab_foreground）：挂到前台线程的输入队列上，前台锁就不拦了
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                            ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
kernel32.GetCurrentThreadId.restype = wt.DWORD

kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p

# 剪贴板贴图（CF_DIB）
user32.GetClipboardData.argtypes = [wt.UINT]          # 已声明，重申给读者
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

PW_RENDERFULLCONTENT = 0x00000002

# ── 窗口层级（`_activate` 用来把被挡住的微信提到最前）──────────────
# HWND_TOPMOST / HWND_NOTOPMOST 在 Win32 里是 ``(HWND)-1`` / ``(HWND)-2``，
# 用 c_void_p 的补码形式传（别写成 -1，argtypes 声明的是 c_void_p）。
HWND_TOPMOST = ctypes.c_void_p(-1)
HWND_NOTOPMOST = ctypes.c_void_p(-2)
GA_ROOT = 2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

DIB_RGB_COLORS = 0
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
# 剪贴板贴图用：CF_DIB（设备无关位图）。微信输入框 Ctrl+V 认它。
# 不用 CF_DIBV5 / PNG 格式：实测微信对 CF_DIB 的兼容最好。
CF_DIB = 8
BI_RGB = 0

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_CONTROL = 0x11
VK_MENU = 0x12          # ALT：单独按一下不上屏，用来破前台锁
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
    # ⚠ 2026-10-18 加宽：旧值 (0.034, 0.068) 裁出来只有 37px 高，标题文字
    #   **顶着裁剪区上沿**——窗口刚被顶回前台时位置会抖一两个像素，文字被
    #   切掉半截 OCR 就返回空，表现为「标题栏无文字」（一天 17 次，每次
    #   白等一轮重试）。改成 (0.030, 0.075)：上沿多让出避按钮行的余量、
    #   下沿多含一行缓冲，文字落在裁剪区中间，抖动也切不到。
    "title": (0.45, 0.030, 0.90, 0.075),
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

    # 已验证 OCR 坐标可用的窗口尺寸范围（实测 520~1100 宽双栏布局正常）。
    # ⚠ 最大化（如 2576 宽）时微信把列表压成 ~90px 固定窄条，**不是等比
    # 缩放**，比例坐标全部失效 → 匹配不到任何会话。所以发送前把窗口
    # 恢复到标准尺寸，用户平时怎么拉窗口都不影响。
    STD_W, STD_H = 720, 1088
    MIN_OK_W, MAX_OK_W = 520, 1100
    MIN_OK_H = 800

    def _ensure_layout_safe(self, hwnd: int) -> None:
        """窗口尺寸不在已验证范围内就恢复到标准尺寸（720×1088）。

        最大化 → SW_RESTORE 后再 MoveWindow（只还原不恢复，位置会乱）；
        过小 → 可能触发微信「单栏模式」折叠列表；过大 → 比例坐标失效。
        恢复动作用户可见（窗口会跳一下），但只发生在发送前的一瞬间。
        """
        r = self._rect(hwnd)
        if not r:
            return
        w, hgt = r[2] - r[0], r[3] - r[1]
        style = user32.GetWindowLongW(wt.HWND(hwnd), -16)   # GWL_STYLE
        maximized = bool(style & 0x01000000)                # WS_MAXIMIZE
        in_range = (self.MIN_OK_W <= w <= self.MAX_OK_W
                    and hgt >= self.MIN_OK_H)
        if in_range and not maximized:
            return
        if maximized:
            user32.ShowWindow(wt.HWND(hwnd), 9)             # SW_RESTORE
            time.sleep(0.5)
        user32.MoveWindow(wt.HWND(hwnd), r[0], r[1],
                          self.STD_W, self.STD_H, True)
        time.sleep(0.8)
        log.info("微信窗口尺寸 %dx%d 超出已验证范围，已恢复为 %dx%d",
                 w, hgt, self.STD_W, self.STD_H)

    def _send_live(self, target: str, text: str) -> SendResult:
        hwnd = self._find_window()
        if not hwnd:
            return SendResult(False, target, "未找到微信主窗口，请确认微信已登录且在桌面上")

        if not self._activate(hwnd):
            return SendResult(False, target, "无法把微信窗口切到前台（可能被系统禁止抢占焦点）")
        self._ensure_layout_safe(hwnd)

        # ⚠ **必须在这里查遮挡**：`_grab` 抓的是屏幕像素，窗口被压住一块就
        #   抓到别人的画面，之后每一步都建立在错的识别上 —— 表现成「会话列表
        #   里找不到目标（相似度 0.14）」这种像识别失败的假象，白跑一遍点击，
        #   还可能点到被压住位置下面的别的窗口上。
        free, why = self._ensure_unoccluded(hwnd)
        if not free:
            return SendResult(False, target, why)

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
        # ---- 短路：当前已经打开的就是目标，不必扫列表 ----
        #
        # ⚠ 这一步**必须放在 `_snapshot()` 之前**。历史实现是先全量扫一遍
        #   会话列表（抓帧 + 3x OCR，300~500ms），扫完才在 `self._current`
        #   上判「已在目标会话」—— 钱已经花了。同一批消息连发多条时，
        #   除第一条外每次都在为一个已知答案付全价。
        #   代价只是先读一次标题栏（一次裁剪 + 小图 OCR，约 1/5 成本）。
        #
        # ⚠ 只信 OCR 标题栏，不信 `self._current`：后者是内存态，重启后为空，
        #   而且可能因为用户手动切了会话而失真。两者一致才放行。
        #
        # ⚠ `fast_*` 还有第二个用途：记录**点击之前**右侧开着的是什么。
        #   如果点击前标题栏可读且**不是目标**，后面标题栏偶发读不到时
        #   **不能用面板兜底** —— 面板非空可能只是「本来就开着的那个群」，
        #   兜底放行 = 把消息发进别人的群（见 `_open_chat` 尾部注释）。
        fast_ok, fast_title = self._current_chat_title(hwnd)
        if fast_ok and self._title_matches(target, fast_title):
            self._current = target
            log.info("已打开目标会话「%s」，跳过会话列表扫描", target)
            return True, f"已在目标会话（标题栏：{fast_title[:24]}）"
        # 点击前是否「确认开着一个别的会话」
        other_open_before = fast_ok and not self._title_matches(target, fast_title)

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
            # 上面的短路已经查过一次标题栏，走到这里说明标题栏**没读到**或
            # **对不上**（读者：读不到时 fast_ok=False）。所以这里只认
            # `self._current` 与列表命中的**双重一致**，且不再单独放行 ——
            # 统一交给下面的点击+确认流程，避免「标题栏明明对不上却跳过点击」。
            opened, title = self._current_chat_title(hwnd)
            if opened and self._title_matches(target, title):
                return True, "已在目标会话"

        # 点击（最多两次点击：本机微信偶尔吞掉第一次模拟点击）
        #
        # ⚠ 每次点击后**分两段等**，别把「点击没生效」和「界面没画完」混为一谈
        #   （2026-10-18：日志里「点击会话后标题栏未确认」一天 17 次，每次都要
        #   白等一轮才成功 —— 微信切会话要加载消息、重绘聊天区，固定 0.6 秒
        #   经常不够，但直接拉长又会让「真被吞点击」的情况变得极慢）：
        #     第一段 0.6 秒：绝大多数会话这时已切完，正常路径不变快也不变慢；
        #     第二段再多等 ~1.5 秒：**只在第一段没读到时才补等**，纯增容错。
        #   只加等待、不加点击次数 —— 多点是危险的（可能点进别的会话）。
        last_title = ""
        for attempt in (1, 2):
            if not self._click_abs(hwnd, hit["cx"], hit["cy"]):
                return False, f"点击会话「{hit['text']}」失败"
            time.sleep(0.6)
            opened, title = self._current_chat_title(hwnd)
            if not (opened and self._title_matches(target, title)):
                # 界面可能还在重绘：补等一轮再看，别急着判失败
                for _ in range(3):
                    time.sleep(0.5)
                    opened, title = self._current_chat_title(hwnd)
                    if opened and self._title_matches(target, title):
                        break
            last_title = title
            if opened and self._title_matches(target, title):
                self._current = hit["text"]
                return True, (
                    f"已切到「{hit['text']}」（相似度 {score:.2f}，"
                    f"第 {attempt} 次点击，标题栏：{title[:20]}）")

            # ---- 第二段判据：标题读不到，就看聊天面板里到底有没有会话 ----
            #
            # ⚠ 这里**只在「标题栏读不到」时才兜底**，标题读到但**对不上**
            #   的情况绝不能放行 —— 那说明点进了别的会话，放过去就是把
            #   消息发错群（比投递失败严重得多）。
            # ⚠ 还有一道闸：**点击前就确认开着别的会话时也不兜底**。
            #   那种情况下「面板非空」证明不了点击生效 —— 可能点击被吞、
            #   面板显示的还是原来那个群。只有点击前没有可确认的会话
            #   （fast 读不到/没开），点击后面板出现内容，才说明是这次
            #   点击打开的。
            if not opened and not other_open_before:
                img = self._grab(hwnd)
                if img is not None:
                    pane_open, why = self._pane_is_open(img)
                    if pane_open:
                        self._current = hit["text"]
                        log.info("会话「%s」标题栏读不到（%s），但聊天面板已打开"
                                 "（%s）—— 按成功处理，第 %d 次点击",
                                 hit["text"], title, why, attempt)
                        return True, (
                            f"已切到「{hit['text']}」（相似度 {score:.2f}，"
                            f"第 {attempt} 次点击；标题栏读不到，"
                            f"按聊天面板判据确认）")
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

    def _pane_is_open(self, img) -> tuple[bool, str]:
        """右侧聊天区**是否真的打开了一个会话**（不看标题栏）。

        这是标题栏读不到时的兜底判据 —— 思路来自 wxauto 4.x 复刻版的
        `_pane_has_content`：标题栏 OCR 会因重绘/抖动/字体渲染偶发返回空，
        但那不代表会话没打开，直接判失败会白重试一轮（历史上一天 17 次）。

        ⚠ 判据不能用「消息区有文字」：微信没打开任何会话时，右侧是
        「微信」大 logo 加一句提示，OCR 照样有字。真正区分两者的是
        **有没有消息气泡**——用「消息区里存在非背景色的成片像素」代替
        「有文字」，比 OCR 便宜（纯像素统计，不用跑模型）而且更稳。

        返回 `(是否打开, 说明)`，说明用于日志定位是哪一路救回来的。
        """
        import numpy as np

        W, H = img.size
        x0, y0, x1, y1 = _LAYOUT["message"]
        crop = np.array(img.crop((int(W * x0), int(H * y0),
                                  int(W * x1), int(H * y1))))
        if crop.size == 0:
            return False, "消息区裁剪为空"

        g = crop[:, :, 1].astype(int)
        r = crop[:, :, 0].astype(int)
        b = crop[:, :, 2].astype(int)

        # ① 我方绿气泡（最硬的证据：能投递说明多半刚发过）
        if self._has_outgoing_bubble(img):
            return True, "消息区有发出气泡"

        # ② 背景色估计：取四边一圈的中位数。微信浅色主题近白、深色主题近黑，
        #    两种都靠「和边缘不一样」来判，不写死具体色值。
        edge = np.concatenate([
            crop[0, :, :].reshape(-1, 3), crop[-1, :, :].reshape(-1, 3),
            crop[:, 0, :].reshape(-1, 3), crop[:, -1, :].reshape(-1, 3),
        ])
        bg = np.median(edge, axis=0)
        diff = (np.abs(r - bg[0]) + np.abs(g - bg[1]) + np.abs(b - bg[2]))
        # 只看明显偏离背景的像素（阈值 60 可滤掉渐变和压缩噪点）
        strong = diff > 60
        # 气泡/头像/文字块会形成较大连片区域；空面板只有零星细节。
        #
        # ⚠ 阈值 2% 是**用真机量出来的**（2026-10-18，720×1088）：
        #   打开着的真实聊天（头像+气泡+大段文字）≈ 53%，离阈值一个量级；
        #   合成的稀疏画面（4 个白气泡+深色文字行）≈ 5.4%。
        #   首版写的 4% 会把「只有气泡、文字被 OCR 遮住」的极端画面判死，
        #   降到 2% 给头像/图片消息留余量 —— 纯空白是 0.0%，不会误放行。
        #   改这个值之前先真机抓帧量一遍，别拍脑袋。
        frac = float(np.count_nonzero(strong)) / float(strong.size)
        if frac >= self.PANE_MIN_FRAC:
            return True, f"消息区非空白（内容占比 {frac:.1%}）"
        return False, f"消息区基本空白（内容占比 {frac:.1%}）"

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
            # 标题读不到 → 看聊天面板（同 `_open_chat` 的兜底逻辑）
            if not ok:
                img = self._grab(hwnd)
                if img is not None:
                    pane_open, why = self._pane_is_open(img)
                    if pane_open:
                        log.info("搜索结果「%s」标题栏读不到，但聊天面板已打开"
                                 "（%s）—— 按成功处理", hit["text"], why)
                        return True, (
                            f"已通过搜索（OCR 定位）切到目标"
                            f"（标题栏读不到，按聊天面板判据确认）")
        return False, (
            f"点击搜索结果「{hit['text']}」后标题栏未确认到目标"
            f"（读到：{title_txt or '空'}）")

    def _current_chat_title(self, hwnd: int) -> tuple[bool, str]:
        """OCR 右侧聊天区标题栏，确认当前打开的会话。"""
        img = self._grab(hwnd)
        if img is None:
            return False, "抓帧失败"
        items, _box, _zoom = self._ocr_region(img, "title")
        texts = []
        for _b, text, conf in items:
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
        """把微信切到前台并确保**没被别的窗口挡住**。

        微信 4.x 有三种「看不见」形态，全部要处理：
        1. 最小化（IsIconic）→ SW_RESTORE；
        2. 收进托盘（IsWindowVisible=0、非最小化）→ 按句柄 SW_SHOW
          （不能靠热键，那是个开关，隐藏时唤不回）；
        3. 正常可见 → 只补前台。
        ⚠ PrintWindow 在窗口不可见时抓出来是**纯黑图**，OCR 全空，
        所以发送前必须确认 IsWindowVisible。

        ── 为什么光 SetForegroundWindow 不够（2026-10-18 用户报「被挡住一点
        就投递失败」）────────────────────────────────────────────────
        `_grab()` 走的是 `ImageGrab.grab(bbox=窗口矩形)`，抓的是**屏幕像素**，
        不是窗口自己的内容。窗口只要被压住一块，那块抓到的就是别人的画面。
        而 `SetForegroundWindow` 有两道坎：

          ① **前台锁**：当前前台属于别的进程时，系统**静默拒绝**，只在任务
             栏闪一下，返回值也没法信。所以调完必须**回读**
             `GetForegroundWindow()` 确认前台真的是 hwnd，不是就换招。
          ② **置顶窗口**：即便切到前台，`HWND_TOPMOST` 的窗口照样压在它上面
             （常见：各种悬浮球、录屏框、聊天工具的小窗），
             `GetForegroundWindow()` 还会说是微信 —— 只看它照样误判。

        所以这里做三级兜底：SetForegroundWindow → 校验 → (`BringWindowToTop`
        + `SetWindowPos(NOACTIVATE/TOP`) → 再校验。最后交给 :meth:`_grab`
        的**画面自检**兜底（遮挡到像素级仍能发现），不指望这一层包打天下。
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
            if self._is_foreground(hwnd):
                return self._check_visible(hwnd)

            # ── 前台锁拒了（或被子窗口/置顶窗口挡着）：自己想办法抢 ────
            if self._grab_foreground(hwnd):
                return self._check_visible(hwnd)
            log.warning("微信仍不是前台窗口（当前 hwnd=%s）—— 抓帧可能抓到"
                        "其它窗口的画面，交给遮挡检测兜底",
                        user32.GetForegroundWindow())
            return self._check_visible(hwnd)
        except Exception as exc:
            # ⚠ 必须打堆栈。2026-10-18 真机：这里原本只 warning 一句
            # 「激活微信窗口失败：module 'ctypes.wintypes' has no attribute
            # 'HWND_TOPMOST'」—— 完全看不出是哪一行（是兜底分支里把常量写成了
            # `wt.HWND_TOPMOST`），排查时白试了三次单点调用才定位。堆栈一行就够。
            log.exception("激活微信窗口失败：%s", exc)
            return False

    def _grab_foreground(self, hwnd: int, rounds: int = 3) -> bool:
        """硬抢前台，抢到为止（最多 ``rounds`` 轮）。返回是否真的到了前台。

        用户 2026-10-18 的要求：「要能自己找到焦点」。``SetForegroundWindow``
        被系统前台锁拒掉以后，光 `SetWindowPos` 不一定够（置顶窗口还在上面）。
        Windows 的前台锁有一条明文豁免：**发起调用的进程最近收到过用户的键盘
        输入**时自动放行。所以这里按「从轻到重」逐级升级，每轮都**回读校验**：

          ① ``SetWindowPos(TOPMOST→NOTOPMOST, NOACTIVATE)``：不抢焦点也提到
             最前，够用就走，对用户打扰最小；
          ② ``AttachThreadInput`` 挂到当前前台线程再 ``SetForegroundWindow``
             —— 跨线程输入队列共享后，前台锁形同虚设（老牌做法，Win10/11
             仍有效）；
          ③ 注入一次 ``ALT`` 空击（``keybd_event``）伪造「用户刚有输入」，
             再 ``SetForegroundWindow``。这是最后的办法，会有一次极短的真实
             按键，但用户感知不到内容（单独 ALT 不上屏）。

        ⚠ TOPMOST **必定立刻摘掉**（哪怕中间某步抛异常），这个程序长期后台跑，
        不能让微信永远压着用户的窗口。
        """
        hwnd_w = wt.HWND(hwnd)
        for i in range(max(1, rounds)):
            # ① 不抢焦点地提到最前
            self._topmost_pulse(hwnd_w)
            if self._focus_and_check(hwnd, hwnd_w):
                log.info("已把微信顶回前台（第 %d 轮，置顶法）", i + 1)
                return True

            # ② 挂到前台线程再抢
            try:
                fg = user32.GetForegroundWindow()
                tid_fg = user32.GetWindowThreadProcessId(wt.HWND(fg), None)
                tid_me = kernel32.GetCurrentThreadId()
                if tid_fg and tid_fg != tid_me:
                    user32.AttachThreadInput(tid_me, tid_fg, True)
                    try:
                        self._focus_and_check(hwnd, hwnd_w)
                    finally:
                        user32.AttachThreadInput(tid_me, tid_fg, False)
                    if self._is_foreground(hwnd):
                        log.info("已把微信顶回前台（第 %d 轮，AttachThreadInput）",
                                 i + 1)
                        return True
            except Exception:                   # noqa: BLE001
                log.debug("AttachThreadInput 抢前台失败（忽略，继续下一招）",
                          exc_info=True)

            # ③ 伪造用户输入后硬抢
            try:
                user32.keybd_event(VK_MENU, 0, 0, 0)
                user32.keybd_event(VK_MENU, 0, 2, 0)        # 2 = KEYUP
            except Exception:                   # noqa: BLE001
                pass
            if self._focus_and_check(hwnd, hwnd_w):
                log.info("已把微信顶回前台（第 %d 轮，模拟用户输入）", i + 1)
                return True
        return False

    def _focus_and_check(self, hwnd: int, hwnd_w) -> bool:
        """SetForegroundWindow 一次并回读校验。"""
        try:
            user32.SetForegroundWindow(hwnd_w)
        except Exception:                       # noqa: BLE001
            return False
        time.sleep(max(self.focus_delay, 0.12))
        return self._is_foreground(hwnd)

    @staticmethod
    def _topmost_pulse(hwnd_w) -> None:
        """把窗口临时提到 TOPMOST 再立刻摘掉（不抢焦点）。

        ⚠ 摘除必须写在 ``finally`` 里：中间抛异常也不能让微信留在 topmost，
        那会让用户的窗口永远被它压着 —— 比投递失败更糟。
        """
        try:
            user32.SetWindowPos(hwnd_w, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                                | SWP_SHOWWINDOW)
            time.sleep(0.05)
        finally:
            try:
                user32.SetWindowPos(hwnd_w, HWND_NOTOPMOST, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            except Exception:                   # noqa: BLE001
                log.warning("摘掉 TOPMOST 失败 —— 微信可能一直压在其它窗口上")

    @staticmethod
    def _is_foreground(hwnd: int) -> bool:
        """当前前台是不是这个窗口（含它的子窗口）。"""
        try:
            fg = user32.GetForegroundWindow()
            if not fg:
                return False
            if fg == hwnd:
                return True
            # 前台可能是微信的某个子窗口 / 弹出层，向上找到顶层再比
            return user32.GetAncestor(wt.HWND(fg), GA_ROOT) == hwnd
        except Exception:                       # noqa: BLE001
            return False

    @staticmethod
    def _check_visible(hwnd: int) -> bool:
        if not user32.IsWindowVisible(wt.HWND(hwnd)):
            log.error("微信窗口 SW_SHOW 后仍不可见，画面会抓成黑图")
            return False
        return True

    # ---------- 遮挡检测 ----------

    def _occluded_points(self, hwnd: int) -> list[tuple[int, int]]:
        """窗口矩形里被**别的窗口**压住的采样点（空 = 没被挡）。

        为什么需要这个：``_grab`` 是 ``ImageGrab.grab(bbox=窗口矩形)``，
        抓的是**屏幕像素**。窗口被压住一块，那块抓到的就是别人的画面，
        OCR 随之全错 —— 但流程不会报错，只会表现为「会话列表里找不到目标
        （相似度 0.14）」「标题栏读到别的东西」这类**看起来像识别问题**的
        假象。2026-10-18 用户报「被挡住一点就投递失败」就是这个。

        判据用 ``WindowFromPoint`` 而不是比对像素：它直接回答「这个屏幕坐标
        上最顶层的窗口是谁」，不受主题/深色模式/缩放影响。点落在微信自己的
        子窗口上也返回那个子窗口 —— 所以要 `GetAncestor(GA_ROOT)` 归到顶层
        再比。

        采样点刻意避开窗口最外圈（阴影/圆角）与标题栏最右上（可能是别家的
        悬浮按钮），取**内容区**的几个关键位置：会话列表、聊天区、输入框。
        """
        r = self._rect(hwnd)
        if not r:
            return []
        left, top, right, bottom = r
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            return []
        # 相对坐标（0~1）：左侧会话列表、右上标题栏、中间聊天区、底部输入框
        probes = ((0.10, 0.10), (0.50, 0.05), (0.10, 0.50),
                  (0.60, 0.50), (0.60, 0.90))
        bad: list[tuple[int, int]] = []
        for fx, fy in probes:
            x = left + int(w * fx)
            y = top + int(h * fy)
            try:
                at = user32.WindowFromPoint(_POINT(x, y))
            except Exception:                    # noqa: BLE001
                continue
            if not at:
                continue
            try:
                root = user32.GetAncestor(wt.HWND(at), GA_ROOT)
            except Exception:                    # noqa: BLE001
                root = at
            if (root or at) != hwnd:
                bad.append((x, y))
        return bad

    def _ensure_unoccluded(self, hwnd: int) -> tuple[bool, str]:
        """确保没被挡住；挡了就再顶一次，还不行就如实报错。"""
        bad = self._occluded_points(hwnd)
        if not bad:
            return True, ""
        log.info("微信窗口有 %d 处被别的窗口压着（如 %s），再顶一次",
                 len(bad), bad[0])
        self._activate(hwnd)
        bad = self._occluded_points(hwnd)
        if not bad:
            return True, ""
        return False, (f"微信窗口被其它窗口遮挡（{len(bad)} 处，如坐标 "
                       f"{bad[0]}）—— 抓到的画面不是微信，请关掉挡在微信前面的"
                       f"窗口后重试")

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

    # 小字号区域放大倍数。
    #
    # ⚠ 这个值不是随手定的，来源是 wxauto 4.x 复刻版的实测结论：
    #   **中文放大 3 倍识别率最高，超过 5 倍反而整块返回空**（模型对
    #   过大的字有感受野上限，切碎后拼不回来）。
    #   标题栏原本就是 3 倍（历史值，实测有效）；会话列表是 2 倍 ——
    #   列表行高更小、字更小，同样 3 倍更稳。
    #   调这个值前先在真机上各跑一遍「标题栏读 20 次 + 列表认 20 次」，
    #   别只凭一次成功就改。
    OCR_ZOOM = 3

    # 「聊天面板已打开」的最小非背景像素占比（见 `_pane_is_open` 的量测记录）。
    PANE_MIN_FRAC = 0.02

    def _ocr_region(self, img, guide_name: str, zoom: int = 0):
        """按 `_LAYOUT[guide_name]` 裁一块区域，放大 `zoom` 倍后 OCR。

        返回 `(items, box, zoom)`：`items` 是引擎原始三元组
        `(box_points, text, conf)`，坐标是**放大后**的，调用方自己除。

        为什么要抽成一个方法：放大倍率和坐标还原是「多除一个 N」的关系，
        散在各处写死迟早出现「一处 2 倍一处 3 倍，坐标还原漏除」——
        这类 bug 的表现是「识别都对、点击却偏了一半」，极难排查。
        """
        zoom = zoom or self.OCR_ZOOM
        W, H = img.size
        x0, y0, x1, y1 = _LAYOUT[guide_name]
        box = (int(W * x0), int(H * y0), int(W * x1), int(H * y1))
        crop = img.crop(box)
        if crop.width < 20 or crop.height < 10:
            return [], box, zoom
        if zoom != 1:
            crop = crop.resize((crop.width * zoom, crop.height * zoom))
        import numpy as np
        return self._ocr(np.array(crop)), box, zoom

    def _ocr_sessions(self, img) -> list[dict]:
        """OCR 左侧会话列表。

        同一个会话在列表里是**两行**（第一行群名 + 时间，第二行最后一条消息
        摘要），OCR 会分别识别成两条，直接拿单条文本去匹配会把「群名 + 时间」
        当成一个奇怪的名字。所以这里按 y 聚成「行组」：先输出全部原始项
        （`kind="raw"`），再输出合并项（`kind="session"`，`text` = 行内拼接，
        `cx/cy` 取行中心）—— 点击/匹配都优先用 session 项。

        坐标一律是**截图内**像素。
        """
        # 放大倍率见 `OCR_ZOOM`（3x）。旧值是写死的 2x —— 统一走
        # `_ocr_region` 之后，坐标还原的除数跟着 zoom 走，不会再漏除。
        items, box, zoom = self._ocr_region(img, "list")
        if not items:
            return []

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
            # zoom 缩放 → 回到裁切坐标 → 回到整图坐标
            raw.append({
                "text": text,
                "kind": "raw",
                "x0": min(xs) / zoom + box[0],
                "x1": max(xs) / zoom + box[0],
                "y0": min(ys) / zoom + box[1],
                "y1": max(ys) / zoom + box[1],
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

    # ---------- 贴图 ----------

    def _put_clipboard_image(self, img) -> bool:
        """把 PIL Image 以 CF_DIB 写进剪贴板。

        DIB 的结构 = BITMAPINFOHEADER + 像素（自下而上的 BGRX 行序）。
        PIL 自带 ``ImageWin.Dib`` 能干这事，但它的构造要 HWND 且老版本行为
        不一 —— 手工组内存更可控：32 行代码，换来对每个字节的确切了解。

        ⚠ 24 位图每行要 4 字节对齐，32 位（BGRX）天然对齐，所以统一转 RGB
        后直接按 32 位写，别为了省内存用 24 位 —— 那个 stride 计算是本模块
        两次 access violation 的来源。
        """
        from PIL import Image

        w, h = img.size
        rgba = img.convert("RGB")
        # BGRX：每像素 4 字节，行序自下而上（biHeight 为正）
        rows = []
        for y in range(h - 1, -1, -1):          # 底行在前
            row = bytearray()
            px = rgba.load()
            for x in range(w):
                r, g, b = px[x, y][:3]
                row += bytes((b, g, r, 0))
            rows.append(bytes(row))
        pixels = b"".join(rows)
        header_size = 40                        # BITMAPINFOHEADER
        total = header_size + len(pixels)

        hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, total)
        if not hmem:
            return False
        p = kernel32.GlobalLock(hmem)
        if not p:
            kernel32.GlobalFree(hmem)
            return False
        try:
            # BITMAPINFOHEADER（40 字节，小端）
            import struct

            bi = struct.pack(
                "<IiiHHIIiiII",
                40,             # biSize
                w,              # biWidth
                h,              # biHeight（正 = 底行在前）
                1,              # biPlanes
                32,             # biBitCount
                BI_RGB,         # biCompression
                len(pixels),    # biSizeImage
                0, 0, 0, 0,     # 分辨率与颜色表
            )
            ctypes.memmove(p, bi, header_size)
            ctypes.memmove(p + header_size, pixels, len(pixels))
        finally:
            kernel32.GlobalUnlock(hmem)

        if not user32.OpenClipboard(0):
            kernel32.GlobalFree(hmem)
            return False
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_DIB, ctypes.c_void_p(hmem)):
                kernel32.GlobalFree(hmem)       # 转移失败自己释放
                return False
            # 成功后所有权归系统，不再 GlobalFree
        finally:
            user32.CloseClipboard()
        return True

    def send_image(self, target: str, image_path: str) -> SendResult:
        """把一张本地图片发进会话（复制图 → Ctrl+V → 发送）。

        与 send_text 共用「切窗口 → 开会话 → 点输入框 → 粘贴 → 发送 →
        回读校验」这条主流程，只在「放什么进剪贴板」和「怎么校验」两处
        分岔 —— 文本校验靠 OCR 出正文，图片校验只看绿色气泡出现
        （OCR 认不出图里的内容，硬认就是自欺）。
        """
        from PIL import Image

        if not target:
            return SendResult(False, target, "目标为空")
        path = Path(image_path)
        if not path.is_file():
            return SendResult(False, target, f"图片不存在：{image_path}")
        try:
            img = Image.open(path)
            img.load()
        except Exception as exc:
            return SendResult(False, target, f"图片读取失败：{exc}")
        if self.dry_run:
            print(f"[dry-run] 发送图片到「{target}」: {image_path}")
            return SendResult(True, target, "dry-run 未真实发送")
        try:
            return self._send_image_live(target, img)
        except Exception as exc:
            log.exception("OCR 图片投递失败")
            return SendResult(False, target, f"{type(exc).__name__}: {exc}")

    def _send_image_live(self, target: str, img) -> SendResult:
        hwnd = self._find_window()
        if not hwnd:
            return SendResult(False, target,
                              "未找到微信主窗口，请确认微信已登录且在桌面上")
        if not self._activate(hwnd):
            return SendResult(False, target,
                              "无法把微信窗口切到前台（可能被系统禁止抢占焦点）")
        self._ensure_layout_safe(hwnd)
        ok, detail = self._open_chat(hwnd, target)
        if not ok:
            return SendResult(False, target, detail)
        if not self._click(hwnd, *_LAYOUT["input"]):
            return SendResult(False, target, "点击输入框失败")
        time.sleep(0.2)
        # 图片模式下不清输入框：Ctrl+A 在图和字混排时会连字一起选中再删，
        # 其实清不清都一样（粘贴图会替换选区），跳过少一次按键风险。
        if not self._put_clipboard_image(img):
            return SendResult(False, target, "写入剪贴板（图片）失败")
        time.sleep(0.1)
        self._press(VK_V, ctrl=True)
        time.sleep(0.4)          # 图片比文本粘贴慢，微信要解码
        if not self._click(hwnd, *_LAYOUT["send_btn"]):
            self._press(VK_RETURN)
        time.sleep(self.send_delay)
        if not self.verify:
            return SendResult(True, target, "已发送图片（未校验）")
        deadline = time.time() + 5.0
        while time.time() < deadline:
            frame = self._grab(hwnd)
            if frame is not None and self._has_outgoing_bubble(frame):
                return SendResult(True, target, "已发送图片（检测到发出气泡）")
            time.sleep(0.4)
        return SendResult(False, target, "发送图片后未检测到气泡，请人工确认")

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
