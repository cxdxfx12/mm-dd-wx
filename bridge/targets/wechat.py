"""微信投递端：复用 wxauto 向指定好友/群发送消息。

dry_run=True 时只打印不真发，便于在没有登录微信的环境下验证整条链路。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

from .base import SendResult, Target


class WeChatTarget(Target):
    name = "wechat"

    def __init__(self, wxauto_path: str = "../wxauto-main", dry_run: bool = True):
        self.wxauto_path = Path(wxauto_path).resolve()
        self.dry_run = dry_run
        self._wx = None

    # ---------- 内部 ----------

    def _ensure_client(self):
        """延迟初始化微信实例：只在真的要发送时才连接客户端。"""
        if self._wx is not None:
            return self._wx
        if not self.wxauto_path.exists():
            raise FileNotFoundError(f"wxauto 库路径不存在：{self.wxauto_path}")
        if str(self.wxauto_path) not in sys.path:
            sys.path.insert(0, str(self.wxauto_path))
        from wxauto import WeChat  # noqa: WPS433  延迟导入，避免无微信环境时报错
        self._wx = WeChat()
        return self._wx

    # ---------- 接口实现 ----------

    def send_text(self, target: str, text: str) -> SendResult:
        if not target:
            return SendResult(False, target, "目标为空")
        if not text:
            return SendResult(False, target, "内容为空")
        if self.dry_run:
            print(f"[dry-run] 发送到「{target}」:\n{text}\n{'-' * 40}")
            return SendResult(True, target, "dry-run 未真实发送")
        try:
            wx = self._ensure_client()
            wx.SendMsg(text, who=target)
            return SendResult(True, target)
        except Exception as exc:  # 客户端异常不应中断整个服务
            return SendResult(False, target, f"{type(exc).__name__}: {exc}")

    def send_file(self, target: str, path: str) -> SendResult:
        if self.dry_run:
            print(f"[dry-run] 发送文件到「{target}」: {path}")
            return SendResult(True, target, "dry-run 未真实发送")
        if not Path(path).exists():
            return SendResult(False, target, f"文件不存在：{path}")
        try:
            wx = self._ensure_client()
            ok = wx.SendFiles(path, who=target)
            return SendResult(bool(ok), target)
        except Exception as exc:
            return SendResult(False, target, f"{type(exc).__name__}: {exc}")

    def health_check(self) -> tuple[bool, str]:
        """探活：确认微信客户端是否可连接，供启动自检使用。"""
        if self.dry_run:
            return True, "dry-run 模式，未连接客户端"
        try:
            wx = self._ensure_client()
            return True, f"已连接微信，当前账号：{getattr(wx, 'nickname', '未知')}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=2)}"
