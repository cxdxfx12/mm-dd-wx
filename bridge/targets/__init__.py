"""投递端集合与工厂。

微信有两代客户端，控件结构完全不同，需要对应两个实现：

    WeChatTarget   —— 微信 3.9.x（`WeChatMainWndForPC`），走 wxauto 原版逻辑
    WeChat4Target  —— 微信 4.x  （`mmui::MainWindow`），走自研 mmui:: 控件实现

另有第三代 **OCR 投递端**（`wechat_ocr.WeChatOcrTarget`）：完全不依赖控件树，
抓窗口截图 + OCR 找会话 + 模拟点击。2026-10-18 起微信 4.x 不再暴露
`mmui::*` 控件树（主窗口退化为 `Qt51514QWindowIcon` 空壳，换版本/重装/
重启/切输入法全部无效），UIA 方案彻底失效，OCR 成为唯一可用通路。

配置 `wechat.driver` 可选 auto / wxauto / ui4 / ocr。
auto 会探测本机实际运行的窗口类名：3.9 → wxauto，4.x → **ocr**
（4.x 的 UIA 树已不可靠，auto 直接走 OCR，免得每次启动都报自检失败）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from .base import SendResult, Target
from .wechat import WeChatTarget
from .wechat4 import WeChat4Target
from .wechat_ocr import WeChatOcrTarget

log = logging.getLogger("bridge.targets")

__all__ = [
    "SendResult",
    "Target",
    "WeChatTarget",
    "WeChat4Target",
    "WeChatOcrTarget",
    "build_target",
    "detect_driver",
]


def detect_driver(wxauto_path: str) -> str:
    """探测本机微信属于哪一代，返回 'wxauto'(3.9) 或 'ui4'(4.x)。

    注意：**这里返回 ui4 时，`build_target` 会把它换成 ocr**。
    原因见模块文档 —— 微信 4.x 的 UIA 控件树从 2026-10-18 起不可用，
    再按 ui4 走只会每次自检失败。保留 ui4 返回值是为了让日志能显示
    「探测到 4.x」，而不是假装探测不出。
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
        # 4.x 的 UIA 树已不可用（见模块文档），auto 直接落到 OCR 实现
        if driver == "ui4":
            log.info("微信投递端自动探测：driver=ui4（4.x）→ 改用 OCR 投递端")
            driver = "ocr"
        else:
            log.info("微信投递端自动探测：driver=%s", driver)

    if driver == "wxauto":
        return WeChatTarget(wxauto_path=wxauto_path, dry_run=cfg.wechat.dry_run)
    if driver == "ui4":
        return WeChat4Target(
            wxauto_path=wxauto_path,
            dry_run=cfg.wechat.dry_run,
            show_hotkey=str(getattr(cfg.wechat, "show_hotkey", "") or ""),
        )
    if driver == "ocr":
        return WeChatOcrTarget(
            dry_run=cfg.wechat.dry_run,
            verify=bool(getattr(cfg.wechat, "ocr_verify", True)),
            keep_clipboard=bool(getattr(cfg.wechat, "ocr_keep_clipboard", True)),
            match_threshold=float(getattr(cfg.wechat, "ocr_match_threshold", 0.62)),
        )
    raise ValueError(
        f"未知的 wechat.driver：{driver!r}（可选 auto | wxauto | ui4 | ocr）")
