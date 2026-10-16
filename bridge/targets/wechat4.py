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
    - 微信若在后续版本关闭 UIA 暴露，本模块立即失效 ——
      用 `health_check()` 与 `tools/probe_wechat_ui.py` 可提前发现。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from .base import SendResult, Target

log = logging.getLogger("bridge.target.wechat4")


class WeChat4Target(Target):
    name = "wechat4"

    WINDOW_CLASS = "mmui::MainWindow"
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

    def __init__(
        self,
        wxauto_path: str = "../wxauto-main",
        dry_run: bool = True,
        open_timeout: float = 6.0,
        verify: bool = True,
    ):
        self.wxauto_path = Path(wxauto_path).resolve()
        self.dry_run = dry_run
        self.open_timeout = open_timeout
        self.verify = verify
        self._uia = None
        self._wnd = None
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

    def _window(self):
        """定位微信 4.x 主窗口，带缓存与重连。"""
        if self._wnd is not None:
            try:
                if self._wnd.Exists(0.2):
                    return self._wnd
            except Exception:
                pass
            self._wnd = None

        uia = self._load_uia()
        wnd = uia.WindowControl(ClassName=self.WINDOW_CLASS, searchDepth=1)
        if not wnd.Exists(1):
            raise RuntimeError(
                f"未找到微信 4.x 主窗口（{self.WINDOW_CLASS}）。"
                "请确认微信已启动并登录，且版本为 4.x。"
            )
        self._wnd = wnd
        return wnd

    def _activate(self, wnd) -> None:
        """把微信切到前台 —— SendKeys / Click 都依赖前台焦点。"""
        uia = self._load_uia()
        try:
            uia.SetForegroundWindow(wnd.NativeWindowHandle)
        except Exception:
            try:
                wnd.SetActive()
            except Exception as exc:
                log.debug("激活窗口失败：%s", exc)
        time.sleep(0.25)

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
        """
        table = wnd.ListControl(ClassName=self.SESSION_TABLE, searchDepth=self.SEARCH_DEPTH)
        if not table.Exists(1):
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

    def open_chat(self, target: str) -> bool:
        """把目标会话切到前台。成功返回 True。"""
        wnd = self._window()
        self._activate(wnd)

        if self._current == target:
            title = self._chat_title(wnd)
            if target in title or title.startswith(target):
                return True

        cell = self._find_session(wnd, target)
        if cell is None:
            return self._open_chat_by_search(wnd, target)

        try:
            cell.Click(simulateMove=False)
        except Exception as exc:
            log.warning("点击会话「%s」失败：%s", target, exc)
            return False

        deadline = time.time() + self.open_timeout
        while time.time() < deadline:
            title = self._chat_title(wnd)
            if title and (target in title or title.startswith(target)):
                self._current = target
                return True
            time.sleep(0.25)
        log.warning("已点击「%s」但标题未匹配（当前标题：%r）", target, self._chat_title(wnd))
        return False

    def _open_chat_by_search(self, wnd, target: str) -> bool:
        """会话不在渲染范围内时，退回「搜索框搜索」路径。"""
        uia = self._load_uia()
        box = wnd.EditControl(ClassName=self.SEARCH_BOX, searchDepth=self.SEARCH_DEPTH)
        if not box.Exists(1):
            log.warning("会话列表未找到「%s」，且搜索框不可用", target)
            return False

        log.info("会话列表中未渲染「%s」，改用搜索框", target)
        box.Click(simulateMove=False)
        uia.SendKeys("{Ctrl}a{Delete}")
        time.sleep(0.2)
        uia.SetClipboardText(target)
        uia.SendKeys("{Ctrl}v")
        time.sleep(1.6)

        # 搜索结果通常复用了会话单元格结构
        cell = self._find_session(wnd, target)
        if cell is None:
            table = wnd.ListControl(ClassName=self.SESSION_TABLE,
                                    searchDepth=self.SEARCH_DEPTH)
            if table.Exists(0.5):
                t = table.BoundingRectangle
                for c in self._session_cells(wnd):
                    try:
                        if target not in self._first_line(c.Name):
                            continue
                        r = c.BoundingRectangle
                        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
                        # 同样必须校验点击点落在表格内
                        if (r.height() > 4 and t.left <= cx <= t.right
                                and t.top <= cy <= t.bottom):
                            cell = c
                            break
                    except Exception:
                        continue
        if cell is None:
            uia.SendKeys("{Esc}")
            log.warning("搜索「%s」也没有结果", target)
            return False

        try:
            cell.Click(simulateMove=False)
        except Exception:
            uia.SendKeys("{Esc}")
            return False
        uia.SendKeys("{Esc}")
        time.sleep(0.8)

        title = self._chat_title(wnd)
        if title and (target in title or title.startswith(target)):
            self._current = target
            return True
        log.warning("搜索后标题仍未匹配（当前：%r）", title)
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
        wnd = self._window()
        self._activate(wnd)

        if not self.open_chat(target):
            return SendResult(False, target, "未能切到目标会话（标题未匹配）")

        before = self._last_text_message(wnd)

        box = wnd.EditControl(ClassName=self.INPUT_FIELD, searchDepth=self.SEARCH_DEPTH)
        if not box.Exists(2):
            return SendResult(False, target, f"未找到输入框（{self.INPUT_FIELD}）")
        box.Click(simulateMove=False)
        time.sleep(0.15)

        # 剪贴板粘贴：绕开中文输入法，且比逐字输入快得多
        uia.SetClipboardText(text)
        uia.SendKeys("{Ctrl}v")
        time.sleep(0.5)

        # 优先点真实「发送」按钮：避免受「按 Enter 还是 Ctrl+Enter 发送」设置影响
        btn = wnd.ButtonControl(
            ClassName=self.SEND_BUTTON, Name="发送", searchDepth=self.SEARCH_DEPTH
        )
        if btn.Exists(0.6):
            try:
                btn.Click(simulateMove=False)
            except Exception:
                uia.SendKeys("{Enter}")
        else:
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
        try:
            wnd = self._window()
        except Exception as exc:
            return False, str(exc)

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
                    "微信可能已关闭 UIA 暴露，请运行 tools/probe_wechat_ui.py 复查"
                )
            state = "当前未打开会话（发送时自动切换，正常）"
        suffix = "（dry-run，不会真发）" if self.dry_run else ""
        return True, f"微信 4.x 控件树可用（会话 {cells} 个，{state}）{suffix}"

    def close(self) -> None:
        self._wnd = None
        self._current = ""
