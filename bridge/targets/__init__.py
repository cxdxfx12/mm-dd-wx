"""投递端集合与工厂。

微信有两代客户端，控件结构完全不同，需要对应两个实现：

    WeChatTarget   —— 微信 3.9.x（`WeChatMainWndForPC`），走 wxauto 原版逻辑
    WeChat4Target  —— 微信 4.x  （`mmui::MainWindow`），走自研 mmui:: 控件实现

配置 `wechat.driver` 可选 auto / wxauto / ui4。auto 会探测本机实际运行的窗口类名。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from .base import SendResult, Target
from .wechat import WeChatTarget
from .wechat4 import WeChat4Target

log = logging.getLogger("bridge.targets")

__all__ = [
    "SendResult",
    "Target",
    "WeChatTarget",
    "WeChat4Target",
    "build_target",
    "detect_driver",
]


def detect_driver(wxauto_path: str) -> str:
    """探测本机微信属于哪一代，返回 'wxauto'(3.9) 或 'ui4'(4.x)。

    探测不出来时按 'ui4' 处理 —— 微信 4.x 已是当前主线版本。
    """
    try:
        path = Path(wxauto_path)
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
        from wxauto import uiautomation as uia
    except Exception as exc:
        log.debug("UIAutomation 不可用，无法探测微信版本：%s", exc)
        return "ui4"

    for cls, driver in (("WeChatMainWndForPC", "wxauto"), ("mmui::MainWindow", "ui4")):
        try:
            if uia.WindowControl(ClassName=cls, searchDepth=1).Exists(0.4):
                return driver
        except Exception:
            continue
    return "ui4"


def build_target(cfg) -> Target:
    """按配置构造微信投递端。"""
    wxauto_path = str(cfg.resolve(cfg.wechat.wxauto_path))
    driver = str(getattr(cfg.wechat, "driver", "auto") or "auto").lower()

    if driver == "auto":
        driver = detect_driver(wxauto_path)
        log.info("微信投递端自动探测：driver=%s", driver)

    if driver == "wxauto":
        return WeChatTarget(wxauto_path=wxauto_path, dry_run=cfg.wechat.dry_run)
    if driver == "ui4":
        return WeChat4Target(wxauto_path=wxauto_path, dry_run=cfg.wechat.dry_run)
    raise ValueError(f"未知的 wechat.driver：{driver!r}（可选 auto | wxauto | ui4）")
