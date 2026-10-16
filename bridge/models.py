"""消息数据模型：钉钉侧解析后统一成这个结构，微信侧只认这个结构。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

MSG_TYPES = ("text", "image", "file", "voice", "link", "unknown")


@dataclass
class Message:
    source: str = "dingtalk"          # 来源平台标识
    chat_id: str = ""                 # 源会话 ID
    chat_name: str = ""               # 源会话名称
    sender_id: str = ""               # 发送者 ID
    sender_name: str = ""             # 发送者昵称
    company: str = ""                 # 归属公司，用于规则过滤
    content: str = ""                 # 文本内容（图片/文件为占位描述）
    msg_type: str = "text"
    media_path: str = ""              # 图片/文件落地后的本地路径
    msg_time: datetime | None = None  # 源消息时间
    raw_id: str = ""                  # 源平台消息 ID，优先用作去重键

    @property
    def uid(self) -> str:
        """全局唯一 ID，程序重启后仍然稳定，用于幂等去重。"""
        if self.raw_id:
            return f"{self.source}:{self.raw_id}"
        parts = "|".join([
            self.source, self.chat_id, self.sender_id,
            self.content, self.msg_time.isoformat() if self.msg_time else "",
        ])
        return f"{self.source}:{hashlib.sha1(parts.encode('utf-8')).hexdigest()}"

    @property
    def time_text(self) -> str:
        return self.msg_time.strftime("%Y-%m-%d %H:%M:%S") if self.msg_time else ""

    @property
    def short_time(self) -> str:
        return self.msg_time.strftime("%H:%M") if self.msg_time else ""

    def as_dict(self) -> dict:
        return {
            "uid": self.uid,
            "source": self.source,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "company": self.company,
            "content": self.content,
            "msg_type": self.msg_type,
            "media_path": self.media_path,
            "msg_time": self.time_text,
            "raw_id": self.raw_id,
        }
