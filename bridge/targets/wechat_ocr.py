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

from ..logfun import tail
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


class _WindowPlacement(ctypes.Structure):
    """GetWindowPlacement 的出参（幽灵窗唤醒时取「正常矩形」用）。"""

    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint),
                ("ptMinPosition", wt.POINT), ("ptMaxPosition", wt.POINT),
                ("rcNormalPosition", wt.RECT)]


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
# 剪贴板发文件用：CF_HDROP（一串文件路径）。往微信输入框 Ctrl+V 就等于
# 「拖一个文件进来」，聊天里落地的是**原文件卡片**（可下载、可另存），
# 不是截图 —— 用户 2026-10-31 明确要求附件一律发原文件。
CF_HDROP = 15
BI_RGB = 0

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_CONTROL = 0x11
VK_MENU = 0x12          # ALT：单独按一下不上屏，用来破前台锁
KEYEVENTF_KEYUP = 0x0002
VK_V = 0x56
VK_F = 0x46                             # Ctrl+F = 微信「搜索」快捷键
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
    # ⚠ x0 不能再往右收：微信 4.x 的会话列表是**固定像素宽**（~310px，不随
    #   窗口等比缩放），标题紧跟在列表右侧。0.45 这个比例是在 720 宽标定的
    #   （0.45×720=324，刚好在标题起点后面）；窗口一旦是 799 宽，
    #   0.45×799=360 就把「文件传输助手」的「文件传」切进裁剪线左边，
    #   OCR 稳定读成「专输助手」→ _title_matches 永远不中 → 开会话确认
    #   永远失败（2026-10-20 真机抓裁剪图实锤）。0.40 在 720~900 宽都
    #   盖得住标题起点；左边多扫进来的列表时间戳杂字靠 _title_matches
    #   的「目标 ⊆ 标题」包含判据兜住。
    "title": (0.40, 0.030, 0.92, 0.078),
    # ⚠ 2026-10-31 再踩（22:02 投递「申通五星业务员培训群」失败）：
    #   窗口被用户拉宽到 972 时，0.40×972=388 落在标题起点（实测 324）
    #   **右边 64px**，把群名开头「申通五星」整段切掉 —— 标题栏稳定读成
    #   「业务员培训群(85)」，_title_matches 永远不中 → 投递失败。
    #   根因与 2026-10-20 同源：**列表是固定像素宽，标题起点不随窗口
    #   等比移动**，比例线在宽窗口必然切字。修法：x0 封顶在固定像素
    #   （见 TITLE_X0_MAX_PX），窄窗口（<800）仍用比例值不变。
    # 输入框点击点（相对比例）
    "input": (0.70, 0.90),
    # 发送按钮（相对比例，点不到时回落到回车）
    "send_btn": (0.925, 0.968),
    # 消息区（回读校验用）：右侧面板，不含标题栏
    "message": (0.44, 0.06, 1.00, 0.94),
}


# 标题栏裁剪线 x0 的固定像素上限。
# 实测（2026-10-31，972×1332 窗口）：会话列表右缘 ~310px、标题文字起点
# x≈324。封顶 316 = 起点左侧留 8px 余量：
#   · 窗口 ≥ 790 宽时 x0 = 316，恰好盖住标题起点；
#   · 窗口更窄时仍用 0.40 比例（720 宽 → 288，同样在起点左侧）；
#   · 高 DPI 机器列表按 DIP 放大、标题起点更靠右，316 只会多扫进来
#     一点空白/列表边缘，靠 _title_matches 的包含判据兜住，不会切字。
TITLE_X0_MAX_PX = 316


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
        show_hotkey: str = "",
    ):
        self.dry_run = dry_run
        self.verify = verify
        self.keep_clipboard = keep_clipboard
        self.match_threshold = match_threshold
        self.focus_delay = focus_delay
        self.send_delay = send_delay
        self.show_hotkey = show_hotkey
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
                "未找到微信主窗口（标题「微信」/「WeChat」）—— 请确认微信已启动并登录")
        # ⚠ 找到句柄 ≠ 能抓图：_find_window 现在**故意**连最小化/托盘态的
        #   窗口一起返回（否则投递会误报「未找到微信主窗口」，2026-10-20 事故）。
        #   探活要真去抓画面，所以这里得自己把窗口唤到可见 —— 不然自检会在
        #   微信好好在托盘里的时候报「抓不到画面」。
        self._activate(hwnd)
        try:
            img = self._grab(hwnd)
        except Exception as exc:
            return False, f"抓取微信窗口失败：{type(exc).__name__}: {exc}"
        if img is None:
            return False, (
                "抓不到微信窗口画面（PrintWindow 返回空）—— 窗口可能被最小化或"
                "退化成自绘空壳，请手动还原窗口后重试")
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
            log.info("已打开目标会话「%s」，跳过会话列表扫描%s", target,
                     tail("chat_open"))
            return True, f"已在目标会话（标题栏：{fast_title[:24]}）"
        # 点击前是否「确认开着一个别的会话」
        other_open_before = fast_ok and not self._title_matches(target, fast_title)

        img, items = self._snapshot(hwnd)
        if img is None:
            return False, (
                "抓不到微信窗口画面（窗口可能已最小化、收进托盘，或被其他"
                "窗口遮挡）——请把微信还原到桌面可见，或移开压在它上面的窗口")
        if not items:
            return False, "会话列表 OCR 为空，无法定位目标群"

        hit, score = self._best_match(target, items)
        # ---- 相似度过低 = 画面大概率不是微信（2026-11-04 20:07 教训）----
        # `_snapshot` 的「会话数 < 3」自检挡不住这种帧：喵喵自己的窗口 /
        # 采集助手界面 OCR 也能认出二十多条假会话。真正可靠的判据是
        # 「跟目标群的最佳相似度」——真微信列表哪怕没有目标群，也会有个
        # 0.3+ 的擦边命中；抓到别人的界面就只有 0.1 出头。低到可疑就置顶
        # 重抓一帧，谁高用谁，并把坏帧落盘留证。
        if hit is None and score < 0.35:
            img2 = self._grab_pinned(hwnd)
            if img2 is not None:
                items2 = self._ocr_sessions(img2)
                _, score2 = self._best_match(target, items2)
                if score2 > score:
                    log.warning("首帧相似度仅 %.2f（疑似抓到遮挡者），"
                                "置顶重抓得 %.2f —— 采用新帧", score, score2)
                    self._dump_frame(img, "suspect")
                    img, items, score = img2, items2, score2
            if score < 0.35:
                self._dump_frame(img, "suspect")
        if hit is None:
            # ---- 列表里没有（未在首屏）→ 用搜索框 ----
            log.info("会话列表首屏未见「%s」（最高 %.2f），改用搜索框", target, score)
            ok, why = self._search_chat(hwnd, target)
            if ok:
                return True, why
            sample = "、".join(it["text"] for it in items
                               if it.get("kind") == "session")[:5] or "（无）"
            # 相似度极低 + 文本碎片化，多半是「检查时微信在前台、抓帧瞬间又被
            # 别的程序压回去」（2026-11-04 实机：运单采集助手反复弹 Excel 抢
            # 前台，OCR 只读到被盖后的残字，相似度 0.11 却查不出遮挡者）。
            # 把此刻的遮挡状态如实带上，一眼看出是谁在捣乱。
            occ = self._occluder(hwnd)
            occ_note = f"⚠ 此刻微信被「{occ}」挡住了！" if occ else ""
            return False, (
                f"会话列表首屏没找到「{target}」（最高相似度 {score:.2f} < "
                f"{self.match_threshold}），搜索也没成功：{why}。"
                f"当前可见会话：{sample}{occ_note}")

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
        r"""标题栏文本是否指向目标会话（群名可能带人数后缀/被截断）。

        2026-11-04 实锤（20:31 投递连挂）：目标群名 15 个字，微信标题栏
        只放得下「申通五星业务员群（重... (86)」——**永远不可能包含完整
        群名**，原「t in s」判据必挂；搜索页还会把窗口撑宽、标题起点
        左移，裁剪线切掉首字后读成「通五星...」。结果：回车明明已经把
        目标群打开了，却被判成「没切过去」，转入 OCR 点结果项继续空转。
        新增截断感知：标题里出现「...」时，取省略号**之前**的名字段，
        与目标（或目标去掉首字——防裁剪切字）做前缀比对。

        2026-11-05 实锤（14:22 起投递连挂半小时）：标题栏 OCR 把左侧的
        ×（关闭/清除按钮）读成前导「X」——「X 申通五星业务员群 (重...
        (86)」，截断判据只剥**尾部**残渣，前导 X 让比对从第一个字符就
        错位，明明已切到目标群却每次都判失败，重试循环空转。head_of
        因此同时剥**头部**杂字符（x/×/+ 等）；剥掉后即使群名真以 x 开头
        也不会误判 —— t[1:] 兜底分支会接住。
        """
        import re

        def norm(x: str) -> str:
            x = re.sub(r"\s+", "", x)
            # 全角括号归一成半角（OCR 两种都可能出）
            return x.replace("（", "(").replace("）", ")")

        t = norm(target)
        s = norm(title)
        # 空标题（OCR 没读到字）不许命中 —— "" 是任何串的子串，原「s in t」
        # 会误判 True，只靠调用方 opened 标志挡着太悬（2026-11-05 补）。
        if not t or not s:
            return False
        if t in s or s in t:                     # 完整包含（原有判据）
            return True

        def head_of(x: str) -> str:
            r"""剥掉标题尾部的截断残渣（人数后缀/省略号/杂字符），循环剥到稳
            定 —— OCR 会把「重... (86)」读成「重. (86) +」这类乱序组合，
            一层正则剥不干净。两个细节：
            * **数字不进杂字符类**：群名本身可能以数字结尾，乱剥会把
              「xx群2」误配成「xx群」；
            * **括号也不进杂字符类**：否则会先剥掉 «(86)» 的右括号，
              人数规则 `[（(]\d+[)）]$` 就永远匹配不上了（实测踩过）；
            * **头部同样要剥**：OCR 把标题栏左侧的 × 读成前导 X（2026-11-05
              实况）。x 不区分大小写；剥过头（群名真以 x 开头）也没关系，
              下方 t[1:] 兜底分支会接住。
            """
            prev, h = None, x
            while prev != h:
                prev = h
                h = re.sub(r"[（(]\d+[)）]$", "", h)          # 人数 (86)
                h = re.sub(r"[.。…⋯+·\\|:,，、;；\s-]+$", "", h)
                h = re.sub(r"^[.。…⋯+·\\|:,，、;；()（）\sxX×✕]+", "", h)
            return h

        # 候选 1：显式省略号截断 —— 取第一个「...」前的名字段
        # 候选 2：整段尾部剥残渣（OCR 读丢省略号时兜底，20:57 实况）
        cands = []
        m = re.search(r"\.{2,}|…+|⋯+", s)
        if m:
            cands.append(s[:m.start()])
        cands.append(s)
        for cand in cands:
            head = head_of(cand)
            # 前缀长度门槛 6 防短串误配；首字被裁剪线切掉时放宽到 t[1:]，
            # 群名以双 Latin 字母开头（XX快运…）且头部杂字符一并被剥掉时
            # 再放宽到 t[2:]（2026-11-05）。
            if len(head) >= 6 and any(t[k:].startswith(head) for k in (0, 1, 2)):
                return True
        return False

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

    def _dismiss_search_overlay(self, hwnd: int) -> None:
        """退出搜索态，关掉右侧搜索结果栏。

        2026-11-04 实锤：搜索页会把窗口从 901 撑到 1204 宽（右侧多出一条
        搜索结果栏）。不关掉的话，下一步点输入框用的是 ``_LAYOUT["input"]``
        的比例坐标 —— 0.70×1204=843 正好落进搜索结果栏而不是聊天输入框，
        后面的粘贴/发送全会打进搜索框，表现为「发了但群里没收到」。
        """
        try:
            self._press(VK_ESCAPE)
            time.sleep(0.45)
        except Exception:                          # noqa: BLE001
            log.warning("退出搜索态失败（忽略）", exc_info=True)

    def _search_open_hotkey(self, hwnd: int, target: str) -> bool:
        """Ctrl+F 打开搜索 → 粘贴群名 → **验证真的上屏了**。

        2026-11-02 实锤：模拟点击搜索框会被微信**静默吞掉** —— ``_click``
        返回 True，微信毫无反应，搜索框还是灰色的 placeholder「搜索」；
        后面的粘贴全部落进聊天窗口（等于往输入框打字），回车反而切到
        会话列表第一项 —— 表现成「搜索也没成功：搜索结果里没认出目标群」。

        Ctrl+F 是微信自带的「搜索」快捷键，键盘注入不依赖点击命中。
        粘贴后 OCR 顶部条验证：空搜索框的 placeholder 只有「搜索」两字，
        群名上屏后是一长串文本 —— 长度对不上就是没进搜索态。
        """
        self._press(VK_F, ctrl=True)
        time.sleep(0.5)
        self._clear_input()
        if not self._paste(target):
            return False
        time.sleep(1.0)
        img = self._grab(hwnd)
        if img is None:
            return False
        import numpy as np
        w, h = img.size
        top = img.crop((int(w * 0.04), 0, int(w * 0.55), int(h * 0.06)))
        try:
            texts = self._ocr(np.array(top))
        except Exception:                      # noqa: BLE001
            return False
        joined = "".join((x[1] or "") for x in texts if x and len(x) > 1)
        joined = joined.replace(" ", "")
        return len(joined) >= max(4, len(target) // 2)

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
        if not self._search_open_hotkey(hwnd, target):
            # Ctrl+F 没能让搜索词上屏（窗口没焦点/被吞），退回点击路径。
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
        # 回车后窗口布局同样要收窄沉降，多读几轮再下结论
        opened, title = False, ""
        for _i in range(3):
            opened, title = self._current_chat_title(hwnd)
            if opened and self._title_matches(target, title):
                break
            time.sleep(0.7)
        if opened and self._title_matches(target, title):
            self._dismiss_search_overlay(hwnd)
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

        # 核对：结果页切换有延迟，且点击结果会关掉搜索侧栏、窗口从
        # 搜索宽版（~1204）收窄回普通宽度（~900）—— 布局抖动期间标题
        # OCR 读出的是「×」这类碎片（2026-11-04 20:4x 实测：群明明已
        # 打开，3×0.6s 全读空，白白判失败）。首读前先沉降，轮数放宽。
        title_txt = ""
        for _i in range(5):
            time.sleep(0.9 if _i == 0 else 0.7)
            ok, title_txt = self._current_chat_title(hwnd)
            if ok and self._title_matches(target, title_txt):
                self._dismiss_search_overlay(hwnd)
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
                        self._dismiss_search_overlay(hwnd)
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
        """按标题找微信主窗口：最小化、收进托盘（隐藏）都算。

        ── 为什么「隐藏」必须算（2026-10-20 真机事故）──────────────────
        用户报「21:51:01 投递失败 | 未找到微信主窗口，请确认微信已登录且在
        桌面上」，可当时微信**根本没退出**：句柄 67596 还在，标题「微信」，
        类名 ``Qt51514QWindowIcon``，只是 `IsIconic=1`（最小化）。真机量了
        最小化窗口的矩形是 ``(-32000,-32000,-31840,-31972)`` —— **宽 160**，
        被下面那句「宽度 > 400 排除登录小窗」直接判掉，而枚举回调里那句
        `if not IsWindowVisible: return True` 又会把托盘态（隐藏）整个跳过。
        两条路一起堵死，于是「窗口在、微信在、就是找不到」。

        ⚠ 这个函数**只负责给出句柄**，不要求窗口此刻可用：
        `_activate()` 本来就会处理三种形态（最小化→RESTORE、托盘→SHOW、
        正常→补前台），`_ensure_layout_safe()` 也会把尺寸拉回 720×1088。
        在这里提前用「可见性 + 尺寸」筛掉窗口，等于把 _activate 的活抢了，
        还抢错了。
        ⚠ 「排除登录小窗」的 400 像素阈值只对**可见**窗口成立：登录窗是
        176×199 可见小窗；最小化窗口的矩形是假的，不能再拿尺寸筛。
        """
        if self._hwnd and user32.IsWindow(wt.HWND(self._hwnd)):
            # ⚠ 缓存短路不能只判宽度：最小化的缓存句柄 IsWindow 仍是 1、
            #   矩形宽度只有 160，这条 if 一旦按宽度卡，等于每次都要重新枚举。
            r = self._rect(self._hwnd)
            if user32.IsIconic(wt.HWND(self._hwnd)):
                return self._hwnd
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
            # ⚠ 这里**不能**再 `if not IsWindowVisible: return True`：
            #   收进托盘的微信就是 IsWindowVisible=0，句柄和标题都在，
            #   _activate() 一句 SW_SHOW 就能唤回。见方法注释。
            iconic = bool(user32.IsIconic(h))
            r = self._rect(h)
            if iconic:
                # 最小化时矩形是 (-32000,...) 这种假值，不能拿来量尺寸
                found.append(h)
            elif not user32.IsWindowVisible(h):
                # 收进托盘的微信：句柄和标题都在，_activate 一句 SW_SHOW
                # 就能唤回。不能在这里丢掉，见方法注释。
                found.append(h)
            # 排除登录小窗（176×199）和其他挂件
            elif r and (r[2] - r[0]) > 400 and (r[3] - r[1]) > 400:
                found.append(h)
            elif r and (r[2] - r[0]) <= 0 and (r[3] - r[1]) <= 0:
                # 幽灵窗（2026-10-27）：可见、非最小化，但矩形归零 —— 微信
                # 4.x 主窗口会偶尔退化成这个形态。登录小窗是 176×199 的
                # **真实**矩形，不会被这条误收；幽灵窗交给 _activate 唤醒。
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
                    # 2026-11-04 20:07 真机：用户点 X 把微信收进托盘后，
                    # SW_SHOW 偶发唤不回（连挂 40 分钟，报「被 Program
                    # Manager 挡住」——其实是微信根本不在屏幕上，矩形处
                    # 露出的是桌面/别的窗口）。微信的「显示主窗口」全局
                    # 热键是显示/隐藏开关，隐藏态按下即为显示，用它兜底。
                    if not user32.IsWindowVisible(wt.HWND(hwnd)):
                        log.warning("SW_SHOW 唤不回托盘态微信，改发热键 %s",
                                    self.show_hotkey or "(未配置)")
                        self._press_show_hotkey()
                time.sleep(0.4)
            if user32.IsIconic(wt.HWND(hwnd)):
                user32.ShowWindow(wt.HWND(hwnd), 9)          # SW_RESTORE
                time.sleep(0.3)

            # 幽灵窗检查（2026-10-27 真机）：可见、非最小化，但矩形归零。
            # 这种窗口激活「成功」也白搭 —— 画面根本不渲染，先唤醒再说。
            r = self._rect(hwnd)
            if not r or r[2] - r[0] <= 0 or r[3] - r[1] <= 0:
                log.info("微信主窗口矩形退化（%s）—— 幽灵窗，先唤醒", r)
                self._revive_ghost(hwnd)

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

    def _revive_ghost(self, hwnd: int) -> bool:
        """把退化成幽灵窗的微信主窗口唤醒，返回唤醒后矩形是否恢复正常。

        幽灵窗形态（2026-10-27 真机 17:19 事故实测）：``IsWindowVisible=1``、
        非最小化、`SetForegroundWindow` 照样「成功」（回读前台也是它），
        但**窗口不渲染** —— 严重时矩形归零，轻时矩形完好而屏幕像素是别的
        窗口（遮挡检测 5 处全中、错误地报「被遮挡」）。激活三连对它全部
        「无效地成功」，只有强制 Windows/Qt 重建窗口表面才救得回来。
        恢复梯（真机验证有效）：SW_HIDE→SW_SHOW 逼 Qt 重建表面 →（仍退化）
        按 placement 的正常矩形 SetWindowPos 强制重排。
        """
        hwnd_w = wt.HWND(hwnd)
        user32.ShowWindow(hwnd_w, 0)                     # SW_HIDE
        time.sleep(0.3)
        user32.ShowWindow(hwnd_w, 5)                     # SW_SHOW
        time.sleep(0.6)
        r = self._rect(hwnd)
        if r and r[2] - r[0] > 200 and r[3] - r[1] > 200:
            log.info("微信幽灵窗已唤醒（HIDE→SHOW，矩形 %s）", r)
            return True
        p = _WindowPlacement()
        p.length = ctypes.sizeof(_WindowPlacement)
        if user32.GetWindowPlacement(hwnd_w, ctypes.byref(p)):
            n = p.rcNormalPosition
            w, hgt = n.right - n.left, n.bottom - n.top
            if w > 200 and hgt > 200:
                user32.SetWindowPos(hwnd_w, 0, n.left, n.top, w, hgt,
                                    SWP_NOACTIVATE | SWP_SHOWWINDOW)
                time.sleep(0.8)
                r = self._rect(hwnd)
                if r and r[2] - r[0] > 200 and r[3] - r[1] > 200:
                    log.info("微信幽灵窗已唤醒（SetWindowPos，矩形 %s）", r)
                    return True
        log.warning("微信幽灵窗唤醒失败（矩形 %s）", self._rect(hwnd))
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
        # 微信已在前台、采样点却还不是它的像素 —— 2026-10-27 17:19 真机：
        # 主窗口退化成幽灵窗（能激活、有矩形、不渲染），「遮挡」只是假象。
        # 拦下投递是对的（不拦就会把别的窗口像素 OCR 后乱发），但先唤醒一次。
        if self._is_foreground(hwnd):
            log.warning("微信已在前台但 %d 处采样点不是它的像素 —— 疑似幽灵窗，"
                        "尝试唤醒后重测", len(bad))
            if self._revive_ghost(hwnd):
                time.sleep(0.5)
                bad = self._occluded_points(hwnd)
                if not bad:
                    return True, ""
        return False, (f"微信窗口被其它窗口遮挡（{len(bad)} 处，如坐标 "
                       f"{bad[0]}）—— 抓到的画面不是微信，请关掉挡在微信前面的"
                       f"窗口后重试")

    # ---------- 截图 ----------

    def _occluder(self, hwnd: int) -> str | None:
        """矩形采样点上最顶层的窗口若不是微信，返回它的标题（被谁盖住了）。

        2026-11-04 事故：微信收进托盘/被其他窗口压住时，ImageGrab 仍按
        微信的（陈旧）矩形抓屏，抓到的是压在那些坐标上的**别的窗口**，
        OCR 全是无关文字、相似度 0.00，报错却甩锅给「会话列表没找到」，
        排查极难。抓帧前先验证这块屏幕确实归微信：不是就如实上报遮挡者。
        """
        r = self._rect(hwnd)
        if not r:
            return None
        cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
        # 会话列表在左侧栏：左 1/4、垂直中点再采一个点
        lx = r[0] + max(8, (r[2] - r[0]) // 4)
        ly = r[1] + (r[3] - r[1]) // 2
        for px, py in ((cx, cy), (lx, ly)):
            child = user32.WindowFromPoint(_POINT(px, py))
            if not child:
                continue
            root = user32.GetAncestor(child, 2)      # GA_ROOT
            if root and root != hwnd:
                n = user32.GetWindowTextLengthW(root)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(root, buf, n + 1)
                return (buf.value or f"窗口#{root}")[:40]
        return None

    def _grab_pinned(self, hwnd: int):
        """被别的窗口压住时的强抓：临时把微信顶到 TOPMOST，抓完立刻摘。

        ── 为什么需要这条路径（2026-11-04 真机事故）────────────────────
        用户一边用 WorkBuddy / WPS 表格、一边让桥投递时，微信被压在下面。
        ``SetForegroundWindow`` 对后台进程会被前台锁**静默拒绝**（只闪一下
        任务栏），微信始终上不来；``ImageGrab`` 按矩形抓的是屏幕像素，
        于是抓到遮挡者的界面。表现极具误导性：OCR 读出「耳(E)」这类碎片、
        相似度 0.13，报错却写成「会话列表没找到」，看起来像群名写错。

        ``SetWindowPos(HWND_TOPMOST)`` 不走前台锁，能把微信临时提到所有
        普通窗口之上；抓完立即摘掉，用户最多看到微信闪一下（约 0.2 秒）。

        ⚠ 摘除必须写在 ``finally``：中途抛异常也不能让微信长期置顶压着
        用户的窗口 —— 那比投递失败更糟。
        """
        from PIL import ImageGrab

        r = self._rect(hwnd)
        if not r:
            return None
        hwnd_w = wt.HWND(hwnd)
        try:
            user32.SetWindowPos(hwnd_w, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                                | SWP_SHOWWINDOW)
            time.sleep(0.18)          # 等 DWM 重新合成，别抓到旧帧
            occ = self._occluder(hwnd)
            if occ is not None:
                log.warning("置顶后微信仍被「%s」压住（对方也是置顶窗口）", occ)
                return None
            return ImageGrab.grab(bbox=r, all_screens=False)
        except Exception as exc:                       # noqa: BLE001
            log.warning("置顶抓帧失败：%s", exc)
            return None
        finally:
            self._unpin(hwnd, hwnd_w)

    @staticmethod
    def _unpin(hwnd: int, hwnd_w=None) -> None:
        """摘掉微信的 TOPMOST（``finally`` 里调用，异常也不许漏）。"""
        try:
            user32.SetWindowPos(hwnd_w or wt.HWND(hwnd), HWND_NOTOPMOST,
                                0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:                              # noqa: BLE001
            log.warning("摘掉 TOPMOST 失败 —— 微信可能一直压在其它窗口上")

    def _press_show_hotkey(self) -> None:
        """发微信「显示/隐藏主窗口」全局热键（``wechat.show_hotkey``）。

        格式 ``{Ctrl}{Alt}w``；未配置则不动。全局热键由微信自己注册，
        不吃前台锁 —— 这是托盘态 SW_SHOW 失效时唯一可靠的唤回手段。
        """
        seq = (getattr(self, "show_hotkey", "") or "").strip()
        if not seq:
            return
        specials = {"ctrl": VK_CONTROL, "alt": VK_MENU, "shift": 0x10,
                    "win": 0x5B}
        vks = []
        i = 0
        while i < len(seq):
            if seq[i] == "{":
                j = seq.find("}", i)
                if j < 0:
                    return                     # 格式坏，宁可不发
                name = seq[i + 1:j].strip().lower()
                if name in specials:
                    vks.append(specials[name])
                elif len(name) == 1:
                    vks.append(ord(name.upper()))
                i = j + 1
            else:
                vks.append(ord(seq[i].upper()))
                i += 1
        if not vks:
            return
        try:
            for vk in vks:
                user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.06)
            for vk in reversed(vks):
                user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        except Exception as exc:               # noqa: BLE001
            log.warning("发送微信热键失败：%s", exc)

    def _grab(self, hwnd: int):
        """抓整窗画面，返回 PIL.Image；不抢焦点（背景模式）。

        ⚠ **PrintWindow 在这台机器的微信 4.x 上会返回「冻结的旧帧」**：
        2026-10-18 实测——真实点击后屏幕内容（ImageGrab 已变）与 PrintWindow
        返回的位图（md5 不变）不一致，微信的 Qt 渲染走 GPU 合成，
        不响应 PrintWindow 的重绘请求。

        ⚠ **抓帧顺序：WGC → ImageGrab(遮挡时置顶) → PrintWindow**。
        WGC（Windows Graphics Capture）直接取窗口自己的合成表面，被别的
        窗口盖住、被拖出屏幕都能抓到，且不需要把微信置顶（不会闪屏）。
        2026-11-04 实测：用窗口严丝合缝盖住微信，WGC 抓到的仍是微信完整
        界面；ImageGrab 同场景只能抓到遮挡者。WGC 不可用时自动回落老路径。
        """
        from PIL import Image, ImageGrab

        # ---- 首选：WGC（遮挡无关，坐标系已对齐到窗口矩形） ----
        try:
            from . import wgc_capture
        except ImportError:                    # 不在包内被导入时（脚本调试）
            import wgc_capture                 # type: ignore
        img = wgc_capture.grab(hwnd)
        if img is not None:
            return img

        r = self._rect(hwnd)
        if not r:
            return None
        w, h = r[2] - r[0], r[3] - r[1]
        if w <= 0 or h <= 0:
            return None
        # ---- 遮挡守卫：矩形上压着别的窗口时，抓到的一定是别人的画面 ----
        # 上一轮先再给 _activate 一次机会（托盘/最小化/前台被占都会在这里救回），
        # 仍然被盖就放弃抓帧，让上层报「窗口被遮挡」而不是拿垃圾去 OCR。
        occ = self._occluder(hwnd)
        if occ is not None:
            if self._activate(hwnd):
                occ = self._occluder(hwnd)
            if occ is not None:
                # 2026-11-04：用户正用 WorkBuddy / WPS 表格时，微信被压在
                # 下面，SetForegroundWindow 被前台锁静默拒绝 —— 以前这里
                # 直接放弃，投递全挂。改成置顶强抓，见 _grab_pinned。
                log.warning("微信窗口被「%s」遮挡 —— 改用置顶抓帧", occ)
                return self._grab_pinned(hwnd)
        try:
            frame = ImageGrab.grab(bbox=r, all_screens=False)
        except Exception as exc:
            log.warning("ImageGrab 抓窗失败（%s），回退 PrintWindow", exc)
        else:
            # ---- 抓帧后复查：守卫检查到抓屏之间有百毫秒级空档，别的程序
            # （2026-11-04：运单采集助手反复弹 WPS 表格抢前台）可能趁虚而入，
            # 抓回一帧「半张别人的脸」。复查发现被盖就整帧作废，让上层重试，
            # 绝不把垃圾喂给 OCR。
            occ2 = self._occluder(hwnd)
            if occ2 is not None:
                log.warning("抓帧后微信被「%s」压住 —— 本帧作废，等待重试", occ2)
                return None
            return frame
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
        items = self._ocr_sessions(img)
        # ---- 画面内容自检（2026-11-04）--------------------------------
        # `_occluder` 只在窗口中心 + 左侧 1/4 两个点看归属：遮挡者只盖住
        # 一半、或刚好切走又被切回来时都会漏检，抓回来的仍是别人的界面。
        # 真实微信会话列表总有好几条，只剩零星碎片就说明这一帧不可信 ——
        # 置顶重抓一帧，谁认出更多会话用谁，并把可疑帧落盘留证据。
        n1 = sum(1 for it in items if it.get("kind") == "session")
        if n1 < 3:
            img2 = self._grab_pinned(hwnd)
            if img2 is not None:
                items2 = self._ocr_sessions(img2)
                n2 = sum(1 for it in items2 if it.get("kind") == "session")
                if n2 > n1:
                    log.info("首帧可疑（会话仅 %d 条）—— 置顶重抓得 %d 条",
                             n1, n2)
                    self._dump_frame(img, "suspect")
                    return img2, items2
            self._dump_frame(img, "suspect")
        return img, items

    @staticmethod
    def _dump_frame(img, tag: str) -> None:
        """把可疑帧存到 ``data/debug/``，事后能直接看到 OCR 到底看到了什么。

        2026-11-04 排查「相似度 0.13」时，现场没有留下任何画面，只能靠
        猜是遮挡还是群名错 —— 这种证据不该再缺。
        """
        try:
            # ⚠ ``paths.py`` 在 ``app/`` 下，只有 GUI 进程会把 app/ 放进
            # sys.path。工作进程（run.py）只加了仓库根目录，因此这里
            # 直接 ``import paths`` 必炸 ModuleNotFoundError —— 异常被下面
            # 的 log.debug 吞掉，结果 2026-11-04 一整晚的失败现场一张图
            # 都没留下（data/debug 压根没生成）。两条腿一起走：
            try:
                import paths                              # type: ignore
            except ImportError:
                import sys as _sys
                from pathlib import Path as _Path
                _app = _Path(__file__).resolve().parents[2] / "app"
                if str(_app) not in _sys.path:
                    _sys.path.insert(0, str(_app))
                import paths                              # type: ignore
            d = paths.data_dir("debug")
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"wechat-{tag}-{time.strftime('%Y%m%d-%H%M%S')}.png"
            img.save(p)
            old = sorted(d.glob("wechat-*.png"))[:-40]     # 只留最近 40 张
            for f in old:
                try:
                    f.unlink(missing_ok=True)
                except Exception:                          # noqa: BLE001
                    pass
            log.info("可疑画面已存 %s", p)
        except Exception as exc:                           # noqa: BLE001
            log.warning("保存可疑画面失败：%s", exc)

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
        if guide_name == "title":
            # 会话列表固定像素宽、标题起点不随窗口等比移动（详见
            # _LAYOUT["title"] 的 2026-10-31 注）：宽窗口下比例线会切掉
            # 群名开头，这里封顶到固定像素。窄窗口取 min 后仍是比例值。
            box = (min(box[0], TITLE_X0_MAX_PX), box[1], box[2], box[3])
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
        """按窗口内相对比例点击。

        ⚠ 点击是**真实鼠标事件**：那个坐标上是谁，就点谁。微信被别的程序
        压住时盲点，等于点进用户的 WorkBuddy / WPS —— 后果比投递失败严重
        得多（可能误删、误发、误改）。所以点之前先确认这块屏幕归微信；
        不归就置顶让它临时压上去，点完立刻摘掉。
        """
        r = self._rect(hwnd)
        if not r:
            return False
        w, h = r[2] - r[0], r[3] - r[1]
        x, y = r[0] + int(w * rx), r[1] + int(h * ry)

        pinned = False
        if self._occluder(hwnd) is not None:
            self._activate(hwnd)
            if self._occluder(hwnd) is not None:
                hwnd_w = wt.HWND(hwnd)
                user32.SetWindowPos(hwnd_w, HWND_TOPMOST, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
                time.sleep(0.15)
                pinned = True
                user32.SetForegroundWindow(hwnd_w)
                time.sleep(0.1)
                occ = self._occluder(hwnd)
                if occ is not None:
                    log.warning("点击前微信仍被「%s」遮挡 —— 放弃本次点击，"
                                "避免误点用户正在用的程序", occ)
                    self._unpin(hwnd)
                    return False
        try:
            return self._click_screen(x, y)
        finally:
            if pinned:
                self._unpin(hwnd)

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

    def _put_clipboard_files(self, paths: list[str]) -> bool:
        """把文件路径列表以 CF_HDROP 写进剪贴板（等价于在资源管理器里 Ctrl+C）。

        DROPFILES 结构（20 字节头 + 路径串）：
            pFiles(4) pt.x(4) pt.y(4) fNC(4) fWide(4) 然后是多条 UTF-16LE
            路径，每条以 \\0 结尾，整块再以 \\0 收尾。

        ⚠ fWide 必须是 1（Unicode）：写 0 微信收到的是 ANSI 路径，
          中文文件名会乱码，粘贴表现为「没有任何反应」。
        """
        import struct

        if not paths:
            return False
        blob = b""
        for p in paths:
            blob += str(Path(p).resolve()).encode("utf-16-le") + b"\x00\x00"
        blob += b"\x00\x00"                     # 列表结束（双 \\0）
        header = struct.pack("IiiII", 20, 0, 0, 0, 1)
        data = header + blob

        hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not hmem:
            return False
        p = kernel32.GlobalLock(hmem)
        if not p:
            kernel32.GlobalFree(hmem)
            return False
        try:
            ctypes.memmove(p, data, len(data))
        finally:
            kernel32.GlobalUnlock(hmem)

        if not user32.OpenClipboard(0):
            kernel32.GlobalFree(hmem)
            return False
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_HDROP, ctypes.c_void_p(hmem)):
                kernel32.GlobalFree(hmem)       # 转移失败自己释放
                return False
            # 成功后所有权归系统，不再 GlobalFree
        finally:
            user32.CloseClipboard()
        return True

    def send_file(self, target: str, path: str) -> SendResult:
        """把**原文件**发进会话（文件放剪贴板 → Ctrl+V → 发送）。

        走的是微信自己的「粘贴文件」通路，聊天里出现的是可下载的文件卡片，
        不是截图 —— 表格类附件（周报/月报 xlsx）用户要的是能打开的原始表格，
        渲染成长图反而丢了列、丢了筛选能力。
        """
        if not target:
            return SendResult(False, target, "目标为空")
        f = Path(path)
        if not f.is_file():
            return SendResult(False, target, f"文件不存在：{path}")
        if self.dry_run:
            print(f"[dry-run] 发送文件到「{target}」: {path}")
            return SendResult(True, target, "dry-run 未真实发送")
        try:
            return self._send_file_live(target, f)
        except Exception as exc:               # noqa: BLE001
            log.exception("OCR 文件投递失败")
            return SendResult(False, target, f"{type(exc).__name__}: {exc}")

    def _send_file_live(self, target: str, path: Path) -> SendResult:
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

        before = self._grab(hwnd)              # 发之前的消息区，给校验留个底
        if not self._click(hwnd, *_LAYOUT["input"]):
            return SendResult(False, target, "点击输入框失败")
        time.sleep(0.2)
        if not self._put_clipboard_files([str(path)]):
            return SendResult(False, target, "写入剪贴板（文件）失败")
        time.sleep(0.1)
        self._press(VK_V, ctrl=True)
        # 文件比文本/图片都慢：微信要把文件读进来生成卡片缩略图，
        # 太早回车会发出一条空消息（输入框还没挂上文件）。
        time.sleep(0.9)
        if not self._click(hwnd, *_LAYOUT["send_btn"]):
            self._press(VK_RETURN)
        time.sleep(self.send_delay)
        if not self.verify:
            return SendResult(True, target, "已发送文件（未校验）")
        return self._verify_file_sent(hwnd, target, path, before)

    def _verify_file_sent(self, hwnd: int, target: str, path: Path,
                          before) -> SendResult:
        """文件投递校验。

        文件卡片不是绿色气泡（`_has_outgoing_bubble` 认不出来），OCR 认文件名
        又常常把扩展名读花 —— 所以两级判据：
          1) 消息区 OCR 出文件名主干（最硬的证据：卡片真的出现了）
          2) 消息区底部像素相对发送前有变化（退一步的弱证据，挡住了
             「文件名被 OCR 读错 → 误报失败 → 补投发两遍」这条路）
        """
        probe = path.stem.strip()[:8]
        frame = None
        deadline = time.time() + 6.0
        while time.time() < deadline:
            frame = self._grab(hwnd)
            if frame is not None and probe:
                items, _box, _zoom = self._ocr_region(frame, "message", zoom=2)
                joined = "".join((t or "").replace(" ", "")
                                 for _b, t, c in items
                                 if float(c or 0) >= 0.5)
                if probe.replace(" ", "") in joined:
                    return SendResult(True, target, "已发送文件（回读到文件名）")
            time.sleep(0.5)
        if frame is not None and before is not None and self._bottom_changed(
                before, frame):
            return SendResult(True, target, "已发送文件（检测到消息区变化）")
        return SendResult(False, target,
                          f"发送文件后未检测到新内容，请人工确认（{path.name}）")

    @staticmethod
    def _bottom_changed(before, after) -> bool:
        """消息区底部 18% 是否明显变化 —— 新卡片落地的弱判据。"""
        import numpy as np

        try:
            b = np.asarray(before.convert("L"), dtype=np.int16)
            a = np.asarray(after.convert("L"), dtype=np.int16)
        except Exception:                      # noqa: BLE001
            return False
        if b.shape != a.shape:
            return True
        h = b.shape[0]
        top = int(h * 0.82)
        diff = np.abs(a[top:] - b[top:])
        return float((diff > 24).mean()) > 0.012

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
