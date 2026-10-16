"""采集端抽象：钉钉的各种取数方式都实现这套接口，服务层不关心底层怎么取。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from ..models import Message

MessageHandler = Callable[[Message], None]


def infer_company(companies: list[str], *candidates: str) -> str:
    """从候选文本（会话名、发送者、组织名）中推断消息归属公司。

    命中配置列表中的公司名则返回该名称，全部不命中返回空串
    （空串会被路由规则的 companies 过滤掉，属于预期行为）。
    """
    for text in candidates:
        if not text:
            continue
        for name in companies:
            if name and name in text:
                return name
    return ""


class Source(ABC):
    name: str = "unknown"

    @abstractmethod
    def run(self, on_message: MessageHandler) -> None:
        """阻塞式运行，收到消息时回调 on_message。"""

    def stop(self) -> None:
        """请求停止采集（由 run 自行判断退出）。"""

    def check(self) -> tuple[bool, str]:
        """启动前自检：凭证是否齐全、依赖是否可用。"""
        return True, "ok"

    def __repr__(self) -> str:
        return f"<Source {self.name}>"
