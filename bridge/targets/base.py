"""投递端抽象：任何目标（微信/企业微信/钉钉回推）实现同一套接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SendResult:
    ok: bool
    target: str
    detail: str = ""


class Target(ABC):
    """消息投递目标。"""

    name: str = "unknown"

    @abstractmethod
    def send_text(self, target: str, text: str) -> SendResult:
        ...

    def send_file(self, target: str, path: str) -> SendResult:
        return SendResult(ok=False, target=target, detail="该目标不支持发送文件")

    def close(self) -> None:
        pass

    def __repr__(self) -> str:
        return f"<Target {self.name}>"
