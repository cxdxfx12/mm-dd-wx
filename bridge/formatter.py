"""格式化层：把 Message 渲染成待发送的文本。

模板占位符：{company} {sender} {chat} {time} {content} {type}
"""
from __future__ import annotations

from .models import Message

DEFAULT_TEMPLATE = "【{company}】{sender}：\n{content}"

TYPE_LABEL = {
    "image": "[图片]",
    "file": "[文件]",
    "voice": "[语音]",
    "link": "[链接]",
}


def render(msg: Message, template: str = DEFAULT_TEMPLATE) -> str:
    body = msg.content or TYPE_LABEL.get(msg.msg_type, "")
    values = {
        "company": msg.company or msg.chat_name or "未知来源",
        "sender": msg.sender_name or "未知",
        "chat": msg.chat_name or "",
        "time": msg.short_time,
        "content": body,
        "type": msg.msg_type,
    }
    try:
        text = template.format(**values)
    except KeyError as exc:
        raise ValueError(f"模板占位符不存在：{exc}") from exc
    return text.strip()


def render_digest(messages: list[Message], title: str = "消息汇总") -> str:
    """把多条消息合并成一条摘要，用于「攒批推送」模式。"""
    if not messages:
        return ""
    lines = [f"【{title}】共 {len(messages)} 条", "-" * 18]
    for m in messages:
        prefix = f"{m.short_time} " if m.short_time else ""
        who = m.sender_name or m.company or "未知"
        lines.append(f"{prefix}{who}：{m.content}")
    return "\n".join(lines)
