"""微信 4.x 投递端 —— 直连 `mmui::` 控件树。

为什么需要这个模块
    微信 4.0 起改用 Qt Quick/QML 渲染，主窗口类名由 `WeChatMainWndForPC`
    变为 `mmui::MainWindow`，wxauto 3.9 的窗口定位逻辑直接失效
    （wxauto 已于 2025-10-28 归档停止维护）。
    但**实测微信 4.1.x 仍然向系统暴露 UIA 控件树**，因此可以按 `mmui::*`
    类名重新实现收发，不必降级微信版本。

实测控件对照表（微信 4.1.15.13 / Windows）
    主窗口      `mmui::MainWindow`
    会话表格    `mmui::XTableView`  → `mmui::ChatSessionCell`
    搜索框      `mmui::XValidatorTextEdit`
    消息列表    `mmui::RecyclerListView`
                  ├ `mmui::ChatTextItemView`   name = 文本内容
                  ├ `mmui::ChatItemView`       name = 时间戳
                  ├ `mmui::ChatBubbleItemView` name = 文件 / 图片描述
    输入框      `mmui::ChatInputField`
    发送按钮    `mmui::XOutlineButton` name = '发送'

可靠性约束（务必了解）
    - 需要微信窗口可见且能被切到前台；最小化时 SendKeys 不会生效。
    - 发送用「剪贴板 + Ctrl+V」而非逐字输入，以规避中文输入法干扰。
    - 发送后默认回读消息列表做校验，确认消息真的落到目标会话。
    - **绝不按 Esc**：搜索面板关掉之后再发 Esc，微信 4.x 会把主窗口整个
      收进托盘，窗口对象随之从 UIA 树里消失，之后所有投递都会失败。
    - 微信若在后续版本关闭 UIA 暴露，本模块立即失效 ——
      用 `health_check()` 与 `tools/probe_wechat_ui.py` 可提前发现。

三种「窗口看起来在、其实用不了」的状态（都有实测记录）
    1. 收进托盘     窗口对象从 UIA 里整个消失（`Exists()` → False）。
                    可用微信的「显示/隐藏主窗口」全局热键（默认 Ctrl+Alt+W）唤回。
    2. 点了最小化   窗口对象与坐标都照常，但点击与按键都不会送到微信，
                    会落到该屏幕坐标上的其它程序上；`IsIconic()` 可判定。
    3. 控件没渲染完 刚唤回的窗口、或某些面板弹开时，子控件矩形会是 0×0，
                    此时 uiautomation 的 Click 会抛 LookupError。
    `ensure_visible()` + `_click_safely()` 就是针对这三条加的护栏。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from .base import SendResult, Target

try:                                   # 非 Windows 环境下仅跑逻辑测试时降级
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
except Exception:                      # pragma: no cover - 只在非 Windows 触发
    ctypes = None                      # type: ignore[assignment]
    wintypes = None                    # type: ignore[assignment]
    _user32 = None
    _kernel32 = None

log = logging.getLogger("bridge.target.wechat4")

SW_RESTORE = 9                         # ShowWindow 的还原指令
SW_SHOW = 5                            # ShowWindow 的「显示」指令（隐藏的窗口用这个）
MIN_WINDOW_SIDE = 200                  # 窗口矩形小于这个值视为「没渲染完」
CLICK_RETRIES = 3                      # 会话格点击重试次数（首次点击可能被激活吞掉）


class WeChat4Target(Target):
    name = "wechat4"

    WINDOW_CLASS = "mmui::MainWindow"
    # 主窗口在 Win32 层的标题（Qt 壳窗口类名形如 Qt51514QWindowIcon，带版本号，
    # 不能写死；认标题更稳）。用于窗口被隐藏后按句柄唤出。
    MAIN_TITLES = ("微信", "WeChat")
    SEARCH_BOX = "mmui::XValidatorTextEdit"
    SESSION_TABLE = "mmui::XTableView"
    SESSION_CELL = "mmui::ChatSessionCell"
    MESSAGE_LIST = "mmui::RecyclerListView"
    TEXT_ITEM = "mmui::ChatTextItemView"
    BUBBLE_ITEM = "mmui::ChatBubbleItemView"
    INPUT_FIELD = "mmui::ChatInputField"
    SEND_BUTTON = "mmui::XOutlineButton"
    TITLE_BAR = "mmui::ChatTitleBarChatRoomView"
    TITLE_TEXT = "mmui::XTextView"
    TITLE_PANEL_OFFSET = 300      # 聊天面板相对窗口左缘的偏移（回读标题用）
    TITLE_SEARCH_DEPTH = 28       # 标题文本控件实测在第 21-22 层

    # 控件树较深（输入框约第 19 层），搜索深度要给足
    SEARCH_DEPTH = 30

    # 微信「显示/隐藏主窗口」的全局热键（用于把收进托盘的窗口唤回来）。
    # 注意它是**开关**：窗口在的时候发它会反过来把窗口收起来，所以只在
    # 确认窗口不见了之后才发。
    DEFAULT_SHOW_HOTKEY = "{Ctrl}{Alt}w"
    SCROLL_TO_TOP_TURNS = 12      # 会话列表归位到顶部需要的滚轮格数（够多即可）
    # 搜索面板打开时会话表格里会出现这些分组头，用来判断「列表被搜索结果顶掉了」
    SEARCH_PANEL_MARKERS = ("搜索网络结果", "最常使用")

    def __init__(
        self,
        wxauto_path: str = "../wxauto-main",
        dry_run: bool = True,
        open_timeout: float = 6.0,
        verify: bool = True,
        show_hotkey: str = "",
    ):
        self.wxauto_path = Path(wxauto_path).resolve()
        self.dry_run = dry_run
        self.open_timeout = open_timeout
        self.verify = verify
        self.show_hotkey = (show_hotkey or self.DEFAULT_SHOW_HOTKEY).strip()
        self._uia = None
        self._wnd = None
        self._last_hwnd = 0          # 上次成功定位到的窗口句柄，隐藏后靠它唤醒
        self._current = ""

    # ---------- 底层 ----------

    def _load_uia(self):
        if self._uia is not None:
            return self._uia
        if not self.wxauto_path.exists():
            raise FileNotFoundError(f"wxauto 库路径不存在：{self.wxauto_path}")
        if str(self.wxauto_path) not in sys.path:
            sys.path.insert(0, str(self.wxauto_path))
        from wxauto import uiautomation as uia   # 延迟导入
        self._uia = uia
        return uia

    @staticmethod
    def _qt_shell_present() -> bool:
        """屏幕上是否有一个「微信的 Qt 壳窗口」——有它却没 mmui 主窗口，是已知故障态。

        实测（2026-10-17）：微信 4.1.15.13 在某些启动/托盘往返之后，主窗口会变成
        ``Qt51514QWindowIcon`` 的**自绘壳**（里面嵌 ``MMUIRenderSubWindowHW``，
        UI 自动化子树是空的）。此时屏幕上看得到微信、用起来一切正常，但程序一个
        控件都读不到。**完全退出微信再重新打开**即恢复，不需要改代码。

        没有这条判断，现场表现就是「读不到主窗口，请把窗口从托盘唤回」——而窗口
        根本没在托盘里，人会把时间全花在按热键上。
        """
        if _user32 is None:
            return False
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _lparam):
            try:
                buf = ctypes.create_unicode_buffer(256)
                _user32.GetClassNameW(hwnd, buf, 256)
                cls = buf.value or ""
            except Exception:
                return True
            if cls.startswith("Qt5") and cls.endswith("QWindowIcon"):
                n = _user32.GetWindowTextLengthW(hwnd)
                if n:
                    title = ctypes.create_unicode_buffer(n + 1)
                    _user32.GetWindowTextW(hwnd, title, n + 1)
                    if "微信" in (title.value or ""):
                        found.append(hwnd)
            return True

        _user32.EnumWindows(cb, 0)
        return bool(found)

    def _window(self):
        """定位微信 4.x 主窗口，带缓存与重连。"""
        if self._wnd is not None:
            try:
                if self._wnd.Exists(0.2):
                    return self._wnd
            except Exception:
                pass
            self._wnd = None

        wnd = self._find_window()
        if wnd is None:
            if self._qt_shell_present():
                raise RuntimeError(
                    "微信窗口在屏幕上，但**自动化读不到里面的控件** —— 微信这次的"
                    "主窗口是个自绘空壳（已知故障态）。请**完全退出微信**（托盘图标"
                    "右键 → 退出）再重新打开，程序会自动恢复，不用改任何配置")
            raise RuntimeError(
                f"未找到微信 4.x 主窗口（{self.WINDOW_CLASS}）。请确认微信已启动并登录，"
                "且**主窗口没有被收进托盘**（按 Esc 或点最小化都会这样，窗口在系统里会"
                "整个消失）。可运行 tools/probe_wechat_state.py 查看当前窗口状态。"
            )
        self._wnd = wnd
        self._remember(wnd)
        return wnd

    def _find_window(self):
        """找主窗口，找不到返回 None（不像 `_window` 那样抛异常）。

        **只吞「找不到窗口」这一类异常**。``_load_uia`` 抛出的 ImportError /
        FileNotFoundError 必须原样往上走 —— 那是「环境不对」，跟微信窗口无关；
        以前一律 return None，现场就变成「读不到微信主窗口，请把窗口从托盘唤回」，
        人照着点托盘、按热键折腾半天，真正的原因是解释器里没有 comtypes。
        """
        uia = self._load_uia()
        try:
            wnd = uia.WindowControl(ClassName=self.WINDOW_CLASS, searchDepth=1)
        except Exception:
            return None
        return wnd if wnd.Exists(1) else None

    def _wnd_present(self) -> bool:
        """主窗口当前是否**存在于系统里**（不含最小化、含被其它窗口遮挡）。"""
        if self._wnd is not None:
            try:
                if self._wnd.Exists(0.2):
                    return True
            except Exception:
                pass
            self._wnd = None
        try:
            return self._find_window() is not None
        except Exception:
            # UI 自动化库都加载不了，谈不上「窗口在不在」；health_check 会先报这个。
            return False

    # ---------- 窗口可见性护栏 ----------

    @staticmethod
    def _hwnd(wnd) -> int:
        try:
            return int(wnd.NativeWindowHandle)
        except Exception:
            return 0

    @staticmethod
    def _is_minimized(wnd) -> bool:
        """是否「点了最小化」。注意最小化时 UIA 里坐标照旧有效，不能只看坐标。"""
        hwnd = WeChat4Target._hwnd(wnd)
        if not hwnd or _user32 is None:
            return False
        try:
            return bool(_user32.IsIconic(wintypes.HWND(hwnd)))
        except Exception:
            return False

    @staticmethod
    def _restore_window(wnd) -> None:
        hwnd = WeChat4Target._hwnd(wnd)
        if not hwnd or _user32 is None:
            return
        try:
            _user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        except Exception as exc:
            log.debug("还原微信窗口失败：%s", exc)

    def _remember(self, wnd) -> None:
        """记下窗口句柄 —— 窗口一旦被隐藏，UIA 就再也给不出它，只能靠这个。"""
        h = self._hwnd(wnd)
        if h:
            self._last_hwnd = h

    def _hwnd_by_class(self) -> int:
        """找主窗口句柄，**隐藏（收进托盘）的窗口也能找到**。

        ⚠ 不能拿 UIA 的 ClassName 去 FindWindow：UIA 报 ``mmui::MainWindow``，
        而 Win32 层的真实类名是 ``Qt51514QWindowIcon``、标题「微信」，
        `FindWindowW("mmui::MainWindow")` 必然返回 0（2026-10-17 实测）。

        顺序：① 上次成功定位时记下的句柄 ② 枚举 Qt 顶层窗口按标题认。
        """
        if _user32 is None:
            return 0
        if self._last_hwnd:
            try:
                if _user32.IsWindow(wintypes.HWND(self._last_hwnd)):
                    return self._last_hwnd
            except Exception:
                pass
            self._last_hwnd = 0

        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _lparam):
            try:
                buf = ctypes.create_unicode_buffer(256)
                _user32.GetClassNameW(hwnd, buf, 256)
                cls = buf.value or ""
                if not (cls.startswith("Qt5") and cls.endswith("QWindowIcon")):
                    return True
                n = _user32.GetWindowTextLengthW(hwnd)
                if not n:
                    return True
                title = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, title, n + 1)
                if (title.value or "") in self.MAIN_TITLES:
                    found.append(int(hwnd))
            except Exception:
                pass
            return True

        try:
            _user32.EnumWindows(cb, 0)
        except Exception as exc:
            log.debug("枚举微信窗口失败：%s", exc)
            return 0
        if not found:
            return 0
        self._last_hwnd = found[0]
        return found[0]

    @staticmethod
    def _show_hidden(hwnd: int) -> None:
        if not hwnd or _user32 is None:
            return
        try:
            _user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOW)
        except Exception as exc:
            log.debug("显示微信窗口失败：%s", exc)

    def _wait_present(self, timeout: float) -> bool:
        """等窗口重新出现在 UIA 里（刚 ShowWindow 的那一下还读不到）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._wnd_present():
                return True
            time.sleep(0.25)
        return self._wnd_present()

    def _wait_rendered(self, timeout: float = 4.0) -> bool:
        """等窗口把控件渲染出来。

        实测：主窗口刚被唤回、或搜索面板刚弹开的那一小段时间里，会话表格的
        `BoundingRectangle` 会是 (0,0,0,0)，此时任何 Click 都会抛 LookupError。
        转发是长跑，这里多等一会儿比点错地方划算。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            wnd = self._find_window()
            if wnd is not None:
                try:
                    r = wnd.BoundingRectangle
                    if (r.right - r.left >= MIN_WINDOW_SIDE
                            and r.bottom - r.top >= MIN_WINDOW_SIDE):
                        table = wnd.ListControl(ClassName=self.SESSION_TABLE,
                                                searchDepth=self.SEARCH_DEPTH)
                        if table.Exists(0.3) and self._rect_of(table) is not None:
                            self._wnd = wnd
                            return True
                except Exception:
                    pass
            time.sleep(0.2)
        return False

    def ensure_visible(self) -> tuple[bool, str]:
        """确保微信主窗口**存在、可见、未最小化**；必要时自动唤回 / 还原。

        这是所有投递动作的前置条件。返回 (是否可用, 说明)，说明会直接写进
        投递失败的 detail 里，方便用户在日志里看到「到底卡在哪一步」。
        """
        # 情形 A：UIA 里看不到窗口 —— 收进托盘（隐藏）或已退出
        if not self._wnd_present():
            # A1：按窗口类名拿句柄直接亮出来。隐藏的窗口句柄还在，这一步最可靠。
            hwnd = self._hwnd_by_class()
            if hwnd:
                log.info("微信主窗口不可见（多半收进了托盘），按窗口类名直接唤出")
                self._show_hidden(hwnd)
                if self._wait_present(4.0):
                    log.info("微信主窗口已唤出")
                else:
                    log.warning("已 ShowWindow 但 UIA 里仍读不到窗口，改用热键兜底")

            # A2：还是看不到，才退回全局热键（老路，热键被改过就失效）
            if not self._wnd_present():
                log.info("尝试用热键 %s 唤回微信窗口", self.show_hotkey)
                try:
                    self._load_uia().SendKeys(self.show_hotkey)
                except Exception as exc:
                    return False, f"唤回微信窗口失败：{exc}"
                if not self._wait_present(6.0):
                    return False, (
                        f"微信主窗口找不到，热键 {self.show_hotkey} 也没能唤回 —— "
                        "微信可能已经退出，或「显示/隐藏主窗口」的快捷键被改过。"
                        "请在微信「设置 → 快捷键」里确认该快捷键，"
                        "或手动打开微信主窗口")
                log.info("微信主窗口已唤回")

        # 情形 B：窗口在，但被点了最小化
        wnd = self._window()
        if self._is_minimized(wnd):
            log.info("微信窗口处于最小化状态，自动还原")
            self._restore_window(wnd)
            time.sleep(0.4)
            if self._is_minimized(wnd):
                return False, "微信窗口被最小化且无法自动还原，请手动点开微信主窗口"

        # 情形 C：窗口在但控件还没渲染完
        if not self._wait_rendered(timeout=2.0):
            log.debug("会话表格暂未就绪，继续按原流程尝试")
        return True, "窗口正常"

    def _activate(self, wnd) -> None:
        """把微信切到前台 —— SendKeys / Click 都依赖前台焦点。

        注意 Windows 的「前台锁」：**不是前台的进程**调用 SetForegroundWindow
        时，系统只会让任务栏按钮闪一下，窗口并不上浮。生产环境里桥基本都在
        后台（前台是钉钉/本程序界面），于是紧接着的那一次点击会被系统当成
        「激活窗口」的点击吞掉，会话切不过去 —— 表现为「已点击 XXX 但标题
        未匹配」。这里按业内通行做法绕：先把输入队列挂到当前前台线程上，
        再 SetForegroundWindow；还不够就补一次 Alt 键（系统把按键当用户活动）。
        """
        hwnd = self._hwnd(wnd)
        if self._is_minimized(wnd):
            self._restore_window(wnd)
            time.sleep(0.3)
        if self._foreground_hwnd() != hwnd:
            if not self._force_foreground(hwnd):
                try:                                   # Alt 键解除前台锁
                    self._load_uia().SendKeys("{Alt}")
                except Exception as exc:
                    log.debug("发送 Alt 键失败：%s", exc)
                self._force_foreground(hwnd)
        time.sleep(0.25)

    @staticmethod
    def _foreground_hwnd() -> int:
        if _user32 is None:
            return 0
        try:
            return int(_user32.GetForegroundWindow())
        except Exception:
            return 0

    def _force_foreground(self, hwnd: int) -> bool:
        """尽最大努力把窗口切到前台，返回是否成功。

        AttachThreadInput 的作用是把本线程的输入队列临时挂到当前前台线程上，
        这样系统就认为调用方「本来就是前台」，从而放行 SetForegroundWindow。
        """
        if not hwnd or _user32 is None:
            return False
        if self._foreground_hwnd() == hwnd:
            return True
        attached = False
        fg_thread = 0
        try:
            fg = self._foreground_hwnd()
            if fg:
                fg_thread = int(_user32.GetWindowThreadProcessId(wintypes.HWND(fg), None))
            cur_thread = int(_kernel32.GetCurrentThreadId())
            if fg_thread and fg_thread != cur_thread:
                attached = bool(_user32.AttachThreadInput(cur_thread, fg_thread, True))
            _user32.BringWindowToTop(wintypes.HWND(hwnd))
            _user32.SetForegroundWindow(wintypes.HWND(hwnd))
        except Exception as exc:
            log.debug("强制切前台失败：%s", exc)
        finally:
            if attached:
                try:
                    _user32.AttachThreadInput(int(_kernel32.GetCurrentThreadId()),
                                              fg_thread, False)
                except Exception:
                    pass
        return self._foreground_hwnd() == hwnd

    # ---------- 控件点击护栏 ----------

    @staticmethod
    def _rect_of(ctrl):
        """取控件矩形；0×0 / 取不到都返回 None。"""
        try:
            r = ctrl.BoundingRectangle
        except Exception:
            return None
        if r is None or (r.right - r.left) <= 1 or (r.bottom - r.top) <= 1:
            return None
        return r

    def _click_safely(self, ctrl, what: str) -> bool:
        """控件矩形有效才点击。

        ⚠ 微信在「面板弹开 / 刚被唤回」等状态下会把控件矩形报成 0×0，
        此时 `Click` 要么抛 LookupError，要么把点击落到屏幕边上 ——
        对一个后台长跑的桥来说都不可接受，所以先卡这一道。
        """
        if self._rect_of(ctrl) is None:
            log.warning("%s 当前没有有效位置（微信界面可能没渲染完），放弃本次点击", what)
            return False
        try:
            ctrl.Click(simulateMove=False)
            return True
        except Exception as exc:
            log.warning("点击%s失败：%s", what, exc)
            return False

    # ---------- 会话定位 ----------

    @staticmethod
    def _first_line(text: str) -> str:
        return (text or "").split("\n")[0].strip()

    def _session_cells(self, wnd) -> list:
        table = wnd.ListControl(ClassName=self.SESSION_TABLE, searchDepth=self.SEARCH_DEPTH)
        if not table.Exists(1):
            return []
        try:
            return [c for c in table.GetChildren()
                    if c.ControlTypeName == "ListItemControl"]
        except Exception:
            return []

    def _find_session(self, wnd, target: str, scroll_tries: int = 3):
        """在会话列表里找**点击点落在表格内**的目标单元格。

        ⚠ 安全要点：会话列表是虚拟滚动的，未渲染或已滚出视口的单元格，
        其 BoundingRectangle 会落在表格区域之外（顶部甚至出现负坐标）。
        直接 Click 会把点击落到窗口其他位置甚至别的应用上，
        因此这里必须校验「单元格中心点位于表格可视矩形内」才允许返回。

        ⚠ 滚动方向：会话列表是「新的在上、旧的在下」。只往下滚会离目标越来越
        远 —— 上一版首次没找到就直接往下滚 3 格，一旦目标排在列表上方，之后
        每次投递都必然失败（日志表现：「会话列表中未渲染 XXX」反复出现）。
        现在改成：第一次没找到先归位到顶部，之后才逐屏向下找。
        """
        table = wnd.ListControl(ClassName=self.SESSION_TABLE, searchDepth=self.SEARCH_DEPTH)
        if not table.Exists(1):
            return None
        if self._rect_of(table) is None:
            log.warning("会话列表当前没有有效位置（微信界面没渲染完），本次不查找")
            return None

        for attempt in range(scroll_tries + 1):
            try:
                t = table.BoundingRectangle
                for cell in table.GetChildren():
                    if cell.ControlTypeName != "ListItemControl":
                        continue
                    if self._first_line(cell.Name) != target:
                        continue
                    r = cell.BoundingRectangle
                    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
                    if (r.height() > 4 and t.left <= cx <= t.right
                            and t.top <= cy <= t.bottom):
                        return cell
                    log.debug("找到「%s」但单元格不在可视区：%s", target, r)
            except Exception as exc:
                log.debug("扫描会话列表失败：%s", exc)
                return None

            if attempt < scroll_tries:
                try:
                    if attempt == 0:
                        # 首次没找到：先归位到顶部（最新会话），再往下找
                        table.WheelUp(wheelTimes=self.SCROLL_TO_TOP_TURNS)
                    else:
                        table.WheelDown(wheelTimes=3)
                except Exception:
                    break
                time.sleep(0.35)
        return None

    def locate_session(self, target: str) -> dict:
        """只定位不点击：返回目标会话的可点击性诊断信息。"""
        wnd = self._window()
        table = wnd.ListControl(ClassName=self.SESSION_TABLE, searchDepth=self.SEARCH_DEPTH)
        info: dict = {"target": target, "found": False, "visible": False,
                      "cell_rect": None, "table_rect": None, "total_cells": 0}
        if not table.Exists(1):
            return info
        t = table.BoundingRectangle
        info["table_rect"] = (t.left, t.top, t.right, t.bottom)
        try:
            cells = [c for c in table.GetChildren()
                     if c.ControlTypeName == "ListItemControl"]
        except Exception:
            return info
        info["total_cells"] = len(cells)
        info["first_lines"] = [self._first_line(c.Name) for c in cells]
        for cell in cells:
            if self._first_line(cell.Name) != target:
                continue
            info["found"] = True
            r = cell.BoundingRectangle
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            info["cell_rect"] = (r.left, r.top, r.right, r.bottom)
            info["click_point"] = (cx, cy)
            info["visible"] = bool(
                r.height() > 4 and t.left <= cx <= t.right and t.top <= cy <= t.bottom
            )
            break
        return info

    def _chat_title(self, wnd) -> str:
        """回读当前会话标题。

        实测不同会话类型的标题栏类名不同：
          群聊 `mmui::ChatTitleBarChatRoomView`，单聊/文件传输助手则没有这个包装，
          标题直接是一个 `mmui::XTextView`。因此先走类名路径，失败后
          退回「按位置找标题条里的 XTextView」——标题条位于聊天面板顶部
          （窗口相对 y ∈ [20, 90]，x 在会话列表右侧）。
        """
        bar = wnd.GroupControl(ClassName=self.TITLE_BAR, searchDepth=self.SEARCH_DEPTH)
        if bar.Exists(0.5):
            tv = bar.TextControl(ClassName="mmui::XTextView", searchDepth=10)
            if tv.Exists(0.5) and (tv.Name or "").strip():
                return (tv.Name or "").strip()

        # 位置兜底：在聊天面板顶部条带内做剪枝 DFS 找 XTextView。
        # （内嵌版 uiautomation 没有 FindAll/复数查找方法，只能手动遍历；
        #   剪枝条件：只进入「纵向落在标题条带内」的子树，避开庞大的消息列表。）
        try:
            wr = wnd.BoundingRectangle
        except Exception:
            return ""
        strip_top, strip_bottom = wr.top + 20, wr.top + 90
        panel_left = wr.left + self.TITLE_PANEL_OFFSET
        best = ""

        def dfs(ctrl, depth: int) -> None:
            nonlocal best
            if best or depth > self.TITLE_SEARCH_DEPTH:
                return
            try:
                r = ctrl.BoundingRectangle
            except Exception:
                return
            if r.height() <= 0 or r.bottom < strip_top or r.top > strip_bottom:
                return
            if (ctrl.ClassName == self.TITLE_TEXT and r.left >= panel_left
                    and (ctrl.Name or "").strip()):
                best = (ctrl.Name or "").strip()
                return
            try:
                children = ctrl.GetChildren()
            except Exception:
                return
            for ch in children:
                dfs(ch, depth + 1)
                if best:
                    return

        try:
            dfs(wnd, 0)
        except Exception as exc:
            log.debug("位置法回读标题失败：%s", exc)
        return best

    def _title_matches(self, title: str, target: str) -> bool:
        return bool(title) and (target in title or title.startswith(target))

    def _search_panel_open(self, wnd) -> bool:
        """会话表格里出现「搜索结果 / 最常使用」分组头，说明搜索面板是开着的。"""
        try:
            names = {self._first_line(c.Name) for c in self._session_cells(wnd)}
        except Exception:
            return False
        return bool(names & set(self.SEARCH_PANEL_MARKERS))

    def _dismiss_search_panel(self, wnd) -> bool:
        """把搜索面板收起来。

        搜索面板一旦弹开，会话列表会被「搜索结果」整块顶掉，`_find_session`
        再也找不到任何会话 —— 日志里「会话列表中未渲染 XXX」反复出现的根源
        就是它。收起方式**不能用 Esc**（见 `_open_chat_by_search` 的说明），
        改成点一下右侧聊天区，实测能收起且不影响窗口状态。
        """
        wnd_rect = self._rect_of(wnd)
        if wnd_rect is None:
            return False
        lst = wnd.ListControl(ClassName=self.MESSAGE_LIST, searchDepth=self.SEARCH_DEPTH)
        r = self._rect_of(lst) if lst.Exists(0.3) else None
        if r is not None:
            x, y = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        else:
            # 当前没有打开的会话时点标题条那一带（空白区），同样能收起面板
            x = min(wnd_rect.left + self.TITLE_PANEL_OFFSET + 60, wnd_rect.right - 10)
            y = wnd_rect.top + 45
        try:
            self._load_uia().Click(x, y)
        except Exception as exc:
            log.debug("收起搜索面板失败：%s", exc)
            return False
        time.sleep(0.4)
        return not self._search_panel_open(wnd)

    def _wait_title(self, wnd, target: str, timeout: float = 0.0) -> bool:
        """轮询等标题切到目标会话。"""
        deadline = time.time() + (timeout or self.open_timeout)
        while time.time() < deadline:
            if self._title_matches(self._chat_title(wnd), target):
                return True
            time.sleep(0.25)
        return False

    def open_chat(self, target: str) -> bool:
        """把目标会话切到前台。成功返回 True。

        进门前先过一遍 `ensure_visible()`：微信被最小化 / 收进托盘时，
        UIA 里看到的坐标和标题**都是陈旧的**，照着点/贴会把内容发到别的
        程序上，所以这里必须先确认窗口真的可用。
        """
        ok, why = self.ensure_visible()
        if not ok:
            log.error("微信窗口不可用，无法切换会话：%s", why)
            return False

        wnd = self._window()
        self._activate(wnd)

        if self._current == target and self._title_matches(self._chat_title(wnd), target):
            return True

        # 搜索面板开着的话会话列表是被顶掉的，先收起再找
        if self._search_panel_open(wnd):
            log.info("检测到微信搜索面板处于打开状态，先收起（否则会话列表不可用）")
            self._dismiss_search_panel(wnd)

        cell = self._find_session(wnd, target)
        if cell is None:
            return self._open_chat_by_search(wnd, target)

        # 首次点击可能被系统当成「激活窗口」的点击吞掉（微信本来在后台）；
        # 控件矩形也可能因为界面没渲染完而暂时无效。两种情况都重新定位重试。
        for attempt in range(CLICK_RETRIES):
            # 最后一次给足超时，前面几次快速试
            budget = self.open_timeout if attempt == CLICK_RETRIES - 1 else 2.0
            if (self._click_safely(cell, f"会话「{target}」")
                    and self._wait_title(wnd, target, timeout=budget)):
                self._current = target
                return True
            if attempt < CLICK_RETRIES - 1:
                log.info("点击「%s」未生效（当前标题 %r），重新定位后再试一次",
                         target, self._chat_title(wnd))
                time.sleep(0.3)
                cell = self._find_session(wnd, target, scroll_tries=0)
                if cell is None:
                    return self._open_chat_by_search(wnd, target)
        log.warning("已点击「%s」但标题未匹配（当前标题：%r）", target, self._chat_title(wnd))
        return False

    def _search_results_cell(self, wnd, target: str):
        """在搜索结果面板里找目标。

        搜索面板是**分组的**，同名会有好几行，类名不一样，点错行不会切会话：
          `mmui::SearchContentCellView` —— 真正的「聊天」条目（双行、更高），点它才切会话
          `mmui::XTableCell`           —— 「功能 / 聊天记录」等文本建议行，点了没反应
        所以这里优先取 SearchContent 类型，其次取更高的那一行。
        """
        table = wnd.ListControl(ClassName=self.SESSION_TABLE, searchDepth=self.SEARCH_DEPTH)
        if not table.Exists(0.5):
            return None
        t = self._rect_of(table)
        if t is None:
            return None
        found: list[tuple[int, int, int, object]] = []   # (完全匹配, 是聊天条目, 高度, 控件)
        for c in self._session_cells(wnd):
            try:
                name = self._first_line(c.Name)
                if target not in name:
                    continue
                r = c.BoundingRectangle
                cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
                # 同样必须校验点击点落在表格内，否则点击会落到窗口别处
                if not (r.height() > 4 and t.left <= cx <= t.right
                        and t.top <= cy <= t.bottom):
                    continue
                found.append((1 if name == target else 0,
                              1 if "SearchContent" in (c.ClassName or "") else 0,
                              r.height(), c))
            except Exception:
                continue
        if not found:
            return None
        found.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        return found[0][3]

    def _open_chat_by_search(self, wnd, target: str) -> bool:
        """会话不在渲染范围内时，退回「搜索框搜索」路径。

        两条实测出来的铁律（微信 4.1.15.13），改这里之前务必先读一遍：

        1. **绝不能按 Esc**。搜索面板关掉之后再发 Esc（此时焦点在主窗口上），
           微信用会把主窗口**整个收进托盘** —— 窗口对象从 UIA 树里消失，
           之后每次投递都会失败，日志表现是「标题读成空串 + 投递失败」，
           用户那边看到的是「微信自己最小化了」。这正是 10-16 那次事故的成因。
        2. **切换成功后不要再去点搜索框**。点一下就会重开搜索面板，会话列表
           会被搜索结果顶掉（只剩一项「搜索网络结果」），此后列表查找必然失败。
           点中搜索结果后微信自己会清空搜索框并收起面板，不用我们收拾。
        """
        uia = self._load_uia()
        box = wnd.EditControl(ClassName=self.SEARCH_BOX, searchDepth=self.SEARCH_DEPTH)
        if not box.Exists(1):
            log.warning("会话列表未找到「%s」，且搜索框不可用", target)
            return False

        log.info("会话列表中未渲染「%s」，改用搜索框", target)
        if not self._click_safely(box, "搜索框"):
            return False
        try:
            time.sleep(0.25)
            uia.SendKeys("{Ctrl}a{Delete}")
            time.sleep(0.15)
            uia.SetClipboardText(target)
            uia.SendKeys("{Ctrl}v")
        except Exception as exc:
            log.warning("搜索「%s」输入失败：%s", target, exc)
            return False
        time.sleep(1.6)

        # 搜索结果复用了会话表格结构，但分组里有多种行类型，优先取「聊天」条目
        cell = self._search_results_cell(wnd, target) \
            or self._find_session(wnd, target, scroll_tries=0)
        if cell is None:
            log.warning("搜索「%s」也没有结果（微信里没有这个名字的会话？）", target)
            return False

        if not self._click_safely(cell, f"搜索结果「{target}」"):
            return False

        if self._wait_title(wnd, target, timeout=3.0):
            self._current = target
            return True
        # 同 open_chat：后台状态下第一次点击可能被当成激活点击吞掉，补一次
        cell = self._search_results_cell(wnd, target) \
            or self._find_session(wnd, target, scroll_tries=0)
        if cell is not None and self._click_safely(cell, f"搜索结果「{target}」"):
            if self._wait_title(wnd, target):
                self._current = target
                return True
        log.warning("搜索后标题仍未匹配（当前：%r）", self._chat_title(wnd))
        return False

    # ---------- 读取 ----------

    def read_recent(self, target: str = "", limit: int = 20) -> list[tuple[str, str]]:
        """读取当前（或指定）会话可见区域的消息，返回 [(控件类名, 文本)]。

        只读操作，用于校验投递结果，也可作为「微信 → 其他端」的数据源。
        """
        wnd = self._window()
        if target:
            self.open_chat(target)
        lst = wnd.ListControl(ClassName=self.MESSAGE_LIST, searchDepth=self.SEARCH_DEPTH)
        if not lst.Exists(1):
            return []
        out: list[tuple[str, str]] = []
        try:
            items = [c for c in lst.GetChildren()
                     if c.ControlTypeName == "ListItemControl"]
        except Exception:
            return []
        for it in items[-limit:]:
            try:
                out.append((it.ClassName or "", (it.Name or "").replace("\n", " ").strip()))
            except Exception:
                continue
        return out

    def _last_text_message(self, wnd) -> str:
        for cls, text in reversed(self.read_recent(limit=15)):
            if cls == self.TEXT_ITEM:
                return text
        return ""

    # ---------- 发送 ----------

    def send_text(self, target: str, text: str) -> SendResult:
        if not target:
            return SendResult(False, target, "目标为空")
        if not text:
            return SendResult(False, target, "内容为空")

        if self.dry_run:
            print(f"[dry-run] 发送到「{target}」:\n{text}\n{'-' * 40}")
            return SendResult(True, target, "dry-run 未真实发送")

        try:
            return self._send_text_live(target, text)
        except Exception as exc:
            return SendResult(False, target, f"{type(exc).__name__}: {exc}")

    def _send_text_live(self, target: str, text: str) -> SendResult:
        uia = self._load_uia()

        ok, why = self.ensure_visible()
        if not ok:
            return SendResult(False, target, f"微信窗口不可用：{why}")

        wnd = self._window()
        self._activate(wnd)

        if not self.open_chat(target):
            return SendResult(False, target,
                              f"未能切到目标会话「{target}」（当前标题："
                              f"{self._chat_title(wnd)!r}）")

        before = self._last_text_message(wnd)

        box = wnd.EditControl(ClassName=self.INPUT_FIELD, searchDepth=self.SEARCH_DEPTH)
        if not box.Exists(2):
            return SendResult(False, target, f"未找到输入框（{self.INPUT_FIELD}）")
        if not self._click_safely(box, "消息输入框"):
            return SendResult(False, target, "消息输入框没有有效位置，本次不发送")
        time.sleep(0.15)

        # 剪贴板粘贴：绕开中文输入法，且比逐字输入快得多
        uia.SetClipboardText(text)
        uia.SendKeys("{Ctrl}v")
        time.sleep(0.5)

        # 优先点真实「发送」按钮：避免受「按 Enter 还是 Ctrl+Enter 发送」设置影响
        btn = wnd.ButtonControl(
            ClassName=self.SEND_BUTTON, Name="发送", searchDepth=self.SEARCH_DEPTH
        )
        if not (btn.Exists(0.6) and self._click_safely(btn, "发送按钮")):
            uia.SendKeys("{Enter}")
        time.sleep(0.8)

        if not self.verify:
            return SendResult(True, target, "已发送（未校验）")

        # 回读校验：消息列表最后一条应等于刚发的内容
        expect = text.replace("\n", " ").strip()
        deadline = time.time() + 6.0
        while time.time() < deadline:
            last = self._last_text_message(wnd)
            if last and last == expect:
                return SendResult(True, target, "已发送并回读校验通过")
            if last and last != before:
                # 内容有差异（表情/换行被改写等）但确实产生新消息
                return SendResult(True, target, f"已发送，回读为：{last[:40]!r}")
            time.sleep(0.4)

        return SendResult(False, target, "发送后未在消息列表读到新消息，请人工确认")

    # ---------- 探活 ----------

    def health_check(self) -> tuple[bool, str]:
        """探活。注意：这是**只读**检查，即使 dry_run=False 也不会发消息，
        所以始终真实探测，便于在正式运行前确认控件树可用。

        判定逻辑：微信 4.x 只有在**打开了某个会话**之后才会创建输入框；
        未打开任何会话时右侧是占位页，输入框与消息列表都不存在——这是
        正常状态（发送时会先点击会话再输入），不能据此判失败。
        """
        # 先确认 UI 自动化库本身能用。这一条必须在「窗口在不在」之前判断：
        # 环境不对时窗口明明好好的，报「窗口被收进托盘」会把排障方向带偏。
        try:
            self._load_uia()
        except Exception as exc:
            return False, (
                f"UI 自动化库加载失败：{type(exc).__name__}: {exc} —— 这跟微信窗口"
                f"无关，是**运行环境**问题：多半是当前 Python 解释器缺 comtypes，"
                f"或 wxauto 路径配错了。请用 app/_env.json 里记录的 worker 解释器"
                f"启动（安装版会自动选对）")

        # 再区分「窗口根本不在/被最小化」——这两种状态下坐标和标题都是陈旧的，
        # 报一句「控件结构可能已变更」会把用户带偏。
        if not self._wnd_present():
            if self._qt_shell_present():
                return False, ("微信窗口在屏幕上，但**自动化读不到里面的控件** —— "
                               "这次的主窗口是个自绘空壳（已知故障态）。请完全退出"
                               "微信（托盘图标右键 → 退出）再重新打开，程序会自动"
                               "恢复，不用改任何配置")
            return False, ("读不到微信主窗口 —— 多半是被收进了托盘（按 Esc 或点了"
                           "最小化都会这样）。请点开微信主窗口，或用「显示/隐藏"
                           "主窗口」快捷键唤回后重试")

        wnd = self._window()
        if self._is_minimized(wnd):
            return False, ("微信主窗口处于最小化状态 —— 此时点击与按键都不会送到"
                           "微信，必须先把窗口还原（运行时会自动还原，自检只做提示）")

        cells = len(self._session_cells(wnd))
        if cells == 0:
            return False, "读不到会话列表，控件结构可能已变更"

        box = wnd.EditControl(ClassName=self.INPUT_FIELD, searchDepth=self.SEARCH_DEPTH)
        if box.Exists(1):
            state = "输入框已就绪"
        else:
            lst = wnd.ListControl(ClassName=self.MESSAGE_LIST,
                                  searchDepth=self.SEARCH_DEPTH)
            if lst.Exists(1):
                return False, (
                    f"有打开的会话但读不到输入框（{self.INPUT_FIELD}）—— "
                    "当前打开的多半是公众号/服务号会话（这类会话本来就没有输入框，"
                    "2026-10-17 实测「海康威视客户服务」即如此），切到要转发的群即可；"
                    "若切换后仍读不到，再运行 tools/probe_wechat_ui.py 复查"
                    "（微信也可能关闭了 UIA 暴露）"
                )
            state = "当前未打开会话（发送时自动切换，正常）"
        suffix = "（dry-run，不会真发）" if self.dry_run else ""
        return True, f"微信 4.x 控件树可用（会话 {cells} 个，{state}）{suffix}"

    def close(self) -> None:
        self._wnd = None
        self._current = ""
