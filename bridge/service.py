"""服务编排：采集 → 去重落库 → 规则路由 → 格式化 → 限流 → 投递 → 记录。"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter

from .config import Config
from .formatter import DEFAULT_TEMPLATE, render
from .models import Message
from .router import Router
from .storage import Storage
from .targets import build_target
from .targets.base import Target

log = logging.getLogger("bridge.service")


class BridgeService:
    def __init__(self, cfg: Config, target: Target | None = None):
        self.cfg = cfg
        self.storage = Storage(cfg.resolve(cfg.storage.db_path))
        self.router = Router(cfg.routes)
        self.target = target or build_target(cfg)
        self.counter: Counter = Counter()
        self.last_target_check: tuple[bool, str] = (True, "尚未检查")
        self._last_send = 0.0
        self._send_lock = threading.Lock()

    # ---------- 核心处理 ----------

    def handle(self, msg: Message) -> bool:
        """处理一条消息。返回 True 表示确认为新消息（无论是否真的转发成功）。"""
        if self.storage.is_duplicate(msg.uid):
            self.counter["duplicate"] += 1
            log.debug("重复消息，跳过：%s", msg.uid)
            return False

        self.storage.save_message(msg)
        self.counter["received"] += 1

        matched = self.router.match(msg)
        if not matched.targets:
            self.counter["filtered"] += 1
            log.info("未命中转发规则，仅入库：%s | %s", msg.company, msg.content[:30])
            return True

        text = render(msg, matched.template or DEFAULT_TEMPLATE)
        for tgt in matched.targets:
            self._throttle()
            result = self.target.send_text(tgt, text)
            self.storage.log_delivery(
                msg.uid, tgt, "ok" if result.ok else "fail", result.detail
            )
            if result.ok:
                self.counter["sent"] += 1
                log.info("已投递 → %s", tgt)
            else:
                self.counter["failed"] += 1
                log.error("投递失败 → %s | %s", tgt, result.detail)
        return True

    def _throttle(self) -> None:
        """相邻两条发送之间保持最小间隔，降低微信风控风险。"""
        interval = max(0.0, float(self.cfg.wechat.interval))
        with self._send_lock:
            delta = time.time() - self._last_send
            if delta < interval:
                time.sleep(interval - delta)
            self._last_send = time.time()

    # ---------- 生命周期 ----------

    def preflight(self) -> list[str]:
        """启动自检，返回问题列表（空列表表示全部通过）。

        副产物：投递端的检查结论存入 `last_target_check`，
        供调用方打印（自检通过时也需要让用户看到投递端状态）。
        """
        problems: list[str] = []
        if not self.cfg.wechat.targets and not any(r.targets for r in self.cfg.routes):
            problems.append("没有任何转发目标：请在 routes[].targets 中配置微信好友或群名")
        ok, detail = self.target.health_check()
        self.last_target_check = (ok, detail)
        if not ok:
            problems.append(f"微信投递端不可用：{detail}")
        return problems

    def close(self) -> None:
        self.target.close()
        self.storage.close()

    def summary(self) -> str:
        s = self.storage.stats()
        return (
            f"运行统计：收到 {self.counter['received']} | 重复 {self.counter['duplicate']} | "
            f"未命中规则 {self.counter['filtered']} | 已发送 {self.counter['sent']} | "
            f"失败 {self.counter['failed']} | 累计入库 {s['total_messages']}"
        )
