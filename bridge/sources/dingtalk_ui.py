"""钉钉 PC 客户端 UI 自动化采集端（备选路线，mode: ui）。

⚠ 重要前提
    钉钉 PC 端为 CEF/Electron 架构，聊天区的内部 Web 内容**未必**暴露给
    UIAutomation。上线前必须先运行 `tools/dump_dingtalk_tree.py` 导出真实控件树，
    再据此调整本文件中的选择器常量。

优点：无需开放平台审批、无需内网穿透。
缺点：稳定性显著低于 Stream 模式，且依赖客户端版本、窗口位置与前台焦点。
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from ..models import Message
from .base import Source, infer_company

log = logging.getLogger("bridge.source.ui")

# 钉钉主窗口类名候选（不同版本差异较大，按顺序探测）
CLIENT_CLASS_CANDIDATES = (
    "StandardFrame_DingTalk",
    "DingTalkPCMainWindow",
    "Chrome_WidgetWin_1",
)


class DingTalkUISource(Source):
    name = "dingtalk-ui"

    def __init__(self, cfg):
        self.cfg = cfg
        self.dt = cfg.dingtalk
        self.interval = max(1.0, float(getattr(self.dt, "ui_poll_interval", 5.0)))
        self._stop = threading.Event()
        self._seen: set[str] = set()
        self._uia = None

    # ---------- 底层 ----------

    def _load_uia(self):
        """复用 wxauto 内嵌的 uiautomation 封装，避免重复造轮子。"""
        if self._uia is not None:
            return self._uia
        wxauto_path = Path(self.cfg.wechat.wxauto_path).resolve()
        if not wxauto_path.exists():
            raise FileNotFoundError(f"wxauto 库路径不存在：{wxauto_path}")
        if str(wxauto_path) not in sys.path:
            sys.path.insert(0, str(wxauto_path))
        from wxauto import uiautomation as uia  # 延迟导入
        self._uia = uia
        return uia

    def _find_client(self):
        uia = self._load_uia()
        for cls in CLIENT_CLASS_CANDIDATES:
            wnd = uia.WindowControl(ClassName=cls, searchDepth=1)
            if wnd.Exists(0.3):
                log.debug("命中钉钉窗口类名：%s", cls)
                return wnd
        wnd = uia.WindowControl(Name="钉钉", searchDepth=1)
        if wnd.Exists(1):
            return wnd
        return None

    def _open_chat(self, wnd, chat_name: str) -> bool:
        """在会话列表里切到目标会话。"""
        item = wnd.ListItemControl(Name=chat_name, searchDepth=12)
        if not item.Exists(1):
            item = wnd.TextControl(Name=chat_name, searchDepth=12)
        if not item.Exists(1):
            log.warning("会话列表未找到：%s", chat_name)
            return False
        item.Click(simulateMove=False)
        time.sleep(0.6)
        return True

    def _read_visible(self, wnd, chat_name: str) -> list[Message]:
        """读取当前会话可见区域的消息。

        控件结构未知，采用「全量文本控件按 Y 坐标排序 + 相邻配对」的启发式：
        每条消息通常由「发送者」与「内容」两个相邻文本控件组成。
        如实际结构不同，请用 dump 工具导出后改写此处。
        """
        uia = self._load_uia()
        texts = []
        for ctrl in wnd.GetChildren():
            self._collect_texts(ctrl, texts, depth=0, max_depth=14)
        texts.sort(key=lambda c: (c.BoundingRectangle.top, c.BoundingRectangle.left))

        msgs: list[Message] = []
        buffer: list = []
        for ctrl in texts:
            buffer.append(ctrl)
            if len(buffer) < 2:
                continue
            sender_ctrl, content_ctrl = buffer[-2], buffer[-1]
            sender = (sender_ctrl.Name or "").strip()
            content = (content_ctrl.Name or "").strip()
            if not content:
                continue
            m = Message(
                source="dingtalk",
                chat_name=chat_name,
                sender_name=sender,
                company=infer_company(self.dt.companies, chat_name, sender),
                content=content,
                msg_type="text",
                msg_time=datetime.now(),
                raw_id=f"ui:{chat_name}:{sender}:{content}",
            )
            msgs.append(m)
        return msgs

    def _collect_texts(self, ctrl, out: list, depth: int, max_depth: int) -> None:
        if depth > max_depth:
            return
        try:
            name = ctrl.ControlTypeName
        except Exception:
            return
        if name == "TextControl" and (ctrl.Name or "").strip():
            out.append(ctrl)
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for child in children:
            self._collect_texts(child, out, depth + 1, max_depth)

    # ---------- Source 接口 ----------

    def check(self) -> tuple[bool, str]:
        try:
            uia = self._load_uia()
        except Exception as exc:
            return False, f"UIAutomation 不可用：{exc}"
        wnd = self._find_client()
        if wnd is None:
            return False, "未找到钉钉主窗口，请确认客户端已启动并登录"
        chats = self.dt.chat_names or []
        if not chats:
            return False, "dingtalk.chat_names 为空，UI 模式必须指定要监听的会话名"
        return True, f"已连接钉钉窗口，待监听 {len(chats)} 个会话"

    def run(self, on_message) -> None:
        wnd = self._find_client()
        if wnd is None:
            raise RuntimeError("未找到钉钉主窗口")
        chats = list(self.dt.chat_names or [])
        log.info("UI 轮询启动，间隔 %.1fs，会话：%s", self.interval, chats)
        while not self._stop.is_set():
            for chat in chats:
                if self._stop.is_set():
                    break
                try:
                    if not self._open_chat(wnd, chat):
                        continue
                    for msg in self._read_visible(wnd, chat):
                        if msg.uid in self._seen:
                            continue
                        self._seen.add(msg.uid)
                        on_message(msg)
                except Exception as exc:
                    log.error("读取会话「%s」失败：%s", chat, exc)
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
