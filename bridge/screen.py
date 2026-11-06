"""Windows 多显示器 / DPI 缩放的统一几何层。

这个项目靠 ``GetWindowRect`` 定位窗口、``ImageGrab`` 抓像素、``SetCursorPos``
点鼠标，三者必须落在**同一套坐标**里，否则「识别都对、点击却偏一半」。

规则只有一条，所有写死像素的地方都要遵守：

    进程声明 Per-Monitor V2 之后，Win32 坐标 / UIA 矩形 / 截图 / 鼠标
    全部是**虚拟桌面的物理像素**（副屏在左时 x 可以是负数）。
    而界面控件的尺寸是**逻辑像素（DIP）**，物理尺寸 = DIP × 显示器缩放。

所以「720×1088 标定窗口」「316px 标题裁剪线」这类经验值都是 **100% 下
的 DIP**，用到别的机器必须乘以该窗口所在显示器的缩放比，并且夹进工作区。

2026-11-06 事故复盘：主屏 125% + 加副屏后 worker 没声明 DPI，
GetWindowRect 给逻辑坐标、ImageGrab 给物理像素，差 25%。修完 DPI 后
又发现固定窗口尺寸/固定像素裁剪线在别的分辨率上同样会失效 —— 本模块
把「屏幕拓扑 + 缩放 + 可见性」收成一处，供所有入口复用。

非 Windows 平台所有函数安全退化（返回 100% / 空拓扑），便于跑单测。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes as wt
from dataclasses import dataclass

from .win_dpi import awareness, enable

# GetSystemMetrics 索引
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SM_CMONITORS = 80

# GetMonitorInfo
MONITORINFOF_PRIMARY = 0x00000001
# GetDpiForMonitor 的 dpiType
MDT_EFFECTIVE_DPI = 0

# 窗口距工作区边缘的最小留白（物理像素，100% 下 8px）
EDGE_MARGIN = 8


@dataclass(frozen=True)
class Monitor:
    """一台显示器。所有矩形都是虚拟桌面的物理像素绝对坐标。"""

    rect: tuple[int, int, int, int]        # 整屏 rcMonitor
    work: tuple[int, int, int, int]        # 工作区 rcWork（扣掉任务栏）
    dpi: int                              # 有效 DPI，96 = 100%
    primary: bool = False

    @property
    def scale(self) -> float:
        """该屏的缩放比：1.0 = 100%，1.25 = 125%，1.5 = 150%。"""
        return self.dpi / 96.0

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    def label(self) -> str:
        tag = "主屏" if self.primary else "副屏"
        l, t, r, b = self.rect
        return (f"{tag} ({l},{t})-({r},{b}) {self.width}×{self.height} "
                f"缩放 {self.scale * 100:.0f}% (DPI {self.dpi})")


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
    ]


def _on_windows() -> bool:
    return sys.platform == "win32"


def monitors() -> list[Monitor]:
    """枚举全部显示器（含左侧负坐标副屏），按主屏优先、再按 x 排序。

    调用前会再声明一次 DPI（幂等）。若进程仍是 DPI-unaware，枚举结果会
    退化成逻辑坐标 —— ``describe()`` 会明确告警，别静默算错。
    """
    if not _on_windows():
        return []
    enable()
    user32 = ctypes.windll.user32
    shcore = ctypes.windll.shcore

    cb_t = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC,
                              ctypes.POINTER(wt.RECT), wt.LPARAM)
    found: list[Monitor] = []

    def _cb(hmon, _hdc, _lprc, _data):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(wt.HMONITOR(hmon), ctypes.byref(info)):
            return True
        dx, dy = wt.UINT(), wt.UINT()
        dpi = 96
        try:
            if shcore.GetDpiForMonitor(wt.HMONITOR(hmon), MDT_EFFECTIVE_DPI,
                                       ctypes.byref(dx), ctypes.byref(dy)) == 0:
                dpi = int(dx.value) or 96
        except Exception:                                   # noqa: BLE001
            pass                                            # Win7/8：回落 100%
        rc, wk = info.rcMonitor, info.rcWork
        found.append(Monitor(
            rect=(rc.left, rc.top, rc.right, rc.bottom),
            work=(wk.left, wk.top, wk.right, wk.bottom),
            dpi=dpi,
            primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
        ))
        return True

    try:
        user32.EnumDisplayMonitors(None, None, cb_t(_cb), 0)
    except Exception:                                       # noqa: BLE001
        return []
    found.sort(key=lambda m: (not m.primary, m.rect[0], m.rect[1]))
    return found


def virtual_rect() -> tuple[int, int, int, int]:
    """虚拟桌面并集（物理像素）。副屏在左时 left 为负。"""
    if not _on_windows():
        return (0, 0, 0, 0)
    u = ctypes.windll.user32
    try:
        x = u.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = u.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = u.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        return (x, y, x + w, y + h)
    except Exception:                                       # noqa: BLE001
        return (0, 0, 0, 0)


def primary_monitor() -> Monitor | None:
    ms = monitors()
    return ms[0] if ms else None


def monitor_for_rect(rect: tuple[int, int, int, int]) -> Monitor | None:
    """选与矩形重叠面积最大的屏；都不重叠时选中心最近的屏。"""
    ms = monitors()
    if not ms:
        return None
    l, t, r, b = rect
    best, best_area = None, -1
    for m in ms:
        ml, mt, mr, mb = m.rect
        area = max(0, min(r, mr) - max(l, ml)) * max(0, min(b, mb) - max(t, mt))
        if area > best_area:
            best, best_area = m, area
    if best_area > 0:
        return best
    cx, cy = (l + r) // 2, (t + b) // 2
    return min(ms, key=lambda m: (
        (cx - (m.rect[0] + m.rect[2]) // 2) ** 2
        + (cy - (m.rect[1] + m.rect[3]) // 2) ** 2))


def monitor_for_window(hwnd: int) -> Monitor | None:
    """按窗口矩形找屏；句柄无效时回落主屏。"""
    if not _on_windows() or not hwnd:
        return primary_monitor()
    r = wt.RECT()
    try:
        if not ctypes.windll.user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
            return primary_monitor()
        rect = (r.left, r.top, r.right, r.bottom)
    except Exception:                                       # noqa: BLE001
        return primary_monitor()
    return monitor_for_rect(rect) or primary_monitor()


def scale_for_rect(rect: tuple[int, int, int, int]) -> float:
    """矩形所在屏的缩放比；拿不到拓扑时返回 1.0（保持历史行为）。"""
    m = monitor_for_rect(rect)
    return m.scale if m else 1.0


def scale_for_window(hwnd: int) -> float:
    """窗口所在屏的缩放比。所有 100% 下标定的像素常量都要乘它。"""
    m = monitor_for_window(hwnd)
    return m.scale if m else 1.0


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if not _on_windows() or not hwnd:
        return None
    r = wt.RECT()
    try:
        if not ctypes.windll.user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
            return None
    except Exception:                                       # noqa: BLE001
        return None
    return (r.left, r.top, r.right, r.bottom)


def fit_rect(rect: tuple[int, int, int, int], want_w: int, want_h: int,
             margin: int = EDGE_MARGIN) -> tuple[int, int, int, int]:
    """把期望尺寸夹进「窗口当前所在屏」的工作区，保证整个窗口可见。

    · 小屏 / 高缩放：1088 DIP 高的窗口会被压到工作区高度以内，
      不会再把输入框顶到屏幕外；
    · 副屏在左（负 x）或副屏在右（x=2560+）：位置按该屏工作区夹，
      不会把窗口挪回主屏、也不会跨屏；
    · 拔掉副屏后窗口留在屏幕外：自动移到主屏工作区左上角。

    返回 ``(left, top, right, bottom)``，全部是物理像素绝对坐标。
    """
    mon = monitor_for_rect(rect) or primary_monitor()
    if mon is None:
        return (rect[0], rect[1], rect[0] + want_w, rect[1] + want_h)
    wl, wt_, wr, wb = mon.work
    max_w = max(320, wr - wl - 2 * margin)
    max_h = max(240, wb - wt_ - 2 * margin)
    w = min(max(320, int(want_w)), max_w)
    h = min(max(240, int(want_h)), max_h)
    # 尽量别挪窗口：左上角能保留就保留，越界了再夹回来
    l = max(wl + margin, min(rect[0], wr - margin - w))
    t = max(wt_ + margin, min(rect[1], wb - margin - h))
    return (l, t, l + w, t + h)


def is_fully_visible(rect: tuple[int, int, int, int]) -> bool:
    """矩形是否完整落在某一台显示器的工作区里。

    拿不到显示器拓扑时返回 True —— 不能因为枚举失败就反复去挪用户窗口。
    """
    m = monitor_for_rect(rect)
    if m is None:
        return True
    l, t, r, b = rect
    wl, wt_, wr, wb = m.work
    return (r - l) > 0 and (b - t) > 0 and wl <= l and wt_ <= t \
        and r <= wr and b <= wb


def describe() -> list[str]:
    """给 ``run.py --check`` 用的屏幕体检文本（逐行）。"""
    if not _on_windows():
        return ["屏幕   : 非 Windows 平台，跳过显示器拓扑检查"]
    aw = awareness()
    level = {0: "0 未声明（坐标会错位！）", 1: "1 System-DPI（混合缩放多屏不可靠）",
             2: "2 Per-Monitor V2（正常）"}.get(aw, f"{aw} 未知")
    ms = monitors()
    x0, y0, x1, y1 = virtual_rect()
    lines = [f"屏幕   : {len(ms)} 台显示器，虚拟桌面 ({x0},{y0})-({x1},{y1})，"
             f"进程 DPI 感知 {level}"]
    for i, m in enumerate(ms, 1):
        lines.append(f"         #{i} {m.label()}")
    if aw != 2:
        lines.append("         ⚠ 进程未声明 Per-Monitor V2：截图与窗口坐标会差一个缩放比，"
                     "请从 run.py / start.bat 启动")
    if len(ms) > 1 and len({m.scale for m in ms}) > 1:
        lines.append("         ⚠ 各屏缩放不同：微信窗口请不要跨屏拖动，"
                     "整个窗口留在同一台显示器上")
    return lines


def check_alignment(hwnd: int) -> tuple[bool, str]:
    """实测坐标自洽：抓一帧窗口图，尺寸必须等于 ``GetWindowRect``。

    这是 DPI / 多屏事故最直接的判据 —— 差 25% 就说明两套坐标系没对齐，
    这时候宁可报警也别继续投递（否则点到别的程序）。
    """
    rect = window_rect(hwnd)
    if rect is None:
        return False, "拿不到窗口矩形（微信未启动？）"
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if w <= 0 or h <= 0:
        return False, f"窗口矩形异常 {rect}"
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=rect, all_screens=True)
    except Exception as exc:                                # noqa: BLE001
        return False, f"抓帧失败：{type(exc).__name__}: {exc}"
    if img is None:
        return False, "抓帧返回空"
    if img.size != (w, h):
        return False, (f"坐标不对齐：窗口 {w}×{h}，截图 {img.size[0]}×{img.size[1]}"
                       f"（差 {img.size[0] / w:.2f}×）—— DPI 缩放没统一，投递会错位")
    return True, f"坐标自洽（窗口与截图同为 {w}×{h} 物理像素）"
