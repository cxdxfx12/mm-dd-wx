"""采集端工厂：按配置选择实现，延迟导入以免缺失依赖时启动即失败。"""
from __future__ import annotations

from ..config import Config
from .base import Source


def build_source(cfg: Config) -> Source:
    mode = (cfg.dingtalk.mode or "stream").lower()

    if mode == "stream":
        from .dingtalk_stream import DingTalkStreamSource
        return DingTalkStreamSource(cfg)

    if mode == "ui":
        from .dingtalk_ui import DingTalkUISource
        return DingTalkUISource(cfg)

    if mode == "ocr":
        from .dingtalk_ocr import DingTalkOCRSource
        return DingTalkOCRSource(cfg)

    if mode == "android":
        from .android_gateway import AndroidGatewaySource
        return AndroidGatewaySource(cfg)

    raise ValueError(f"不支持的采集模式：{mode}（可选 stream / ui / ocr / android）")
