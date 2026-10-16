"""规则路由：决定一条消息该转发到哪些微信目标、用哪个模板。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import RouteConfig
from .models import Message

WILDCARD = "*"


def _match_list(patterns: list[str], value: str) -> bool:
    """空列表表示不限；含 '*' 表示不限；否则做不区分大小写的包含匹配。"""
    if not patterns or WILDCARD in patterns:
        return True
    v = (value or "").lower()
    return any(p.lower() in v for p in patterns)


@dataclass
class MatchResult:
    targets: list[str] = field(default_factory=list)
    template: str = ""


class Router:
    def __init__(self, routes: list[RouteConfig]):
        if not routes:
            raise ValueError("路由规则不能为空")
        self.routes = routes

    def match(self, msg: Message) -> MatchResult:
        """返回命中的所有路由合并结果：目标去重、模板取第一条命中的。"""
        targets: list[str] = []
        template = ""
        for r in self.routes:
            if r.companies and not _match_list(r.companies, msg.company):
                continue
            if r.chat_names and not _match_list(r.chat_names, msg.chat_name):
                continue
            if r.msg_types and not _match_list(r.msg_types, msg.msg_type):
                continue
            if r.keywords and not _match_list(r.keywords, msg.content):
                continue
            for t in (r.targets or []):
                if t not in targets:
                    targets.append(t)
            if not template and r.template:
                template = r.template
        return MatchResult(targets=targets, template=template)
