"""「先攒后发」的消息缓冲：同一张 H5 卡片分几批长出来，只发最全的那一版。

## 为什么需要这个模块

钉钉机器人消息是 H5 卡片，**内容是分批渲染出来的**：第一帧可能只有标题和
前两段，表格单元格要再过几百毫秒才长全。用「抓到的这一帧」直接发，群里收到
的就是残废消息（2026-10-17 拦截跟单日报真机事故）。

早期的对策是「扣住等渲染稳定」：新内容第一次见先不发，让手机 800ms 后重报，
等到「同样的内容见到第二眼」再发。这套思路在真机上**基本没工作过** ——
2026-10-18 全天日志：扣住第 1 眼 **14 次**，进到第 2 眼 **0 次**，终投递 1 条。

根因是它的前提「同一批内容重报时指纹不变」不成立：H5 卡片每重绘一次，
**整批节点的指纹都会变**（这个结论在 `fingerprint.py` 里早就写过）。于是

    key = "\\n".join(u.fp for u in new)      # 每帧都不同
    seen = hold_seen + 1 if hold_key == key else 1   # 永远等于 1
    if seen < 2 and hold_total < HOLD_MAX:   # 永远扣住，直到 hold_total 到顶

只能靠 `hold_total` 撞到 `HOLD_MAX` 才放行；而这一路上**只要锚点同时丢失
（长卡片把上一帧的锚点整条挤出屏幕，真机上极常见）整帧就被丢掉**，扣住的那批
直接蒸发，日志还只有一行 INFO。

## 这套替代方案的不同之处

不再问「屏上这两帧一样吗」——那个问题在 H5 卡片上无解。改问：

    「同一条消息，我见过的最全的那个版本是什么？」

每来一帧，把解析出的每条消息按 **(群, 发送者, 时间窗)** 归并进缓冲，
**保留更长的那个版本**（用 `_join_fragments` 拼，能处理两截重叠的情况）。
这条消息**静默 `quiet` 秒**不再有更长版本出现，就投递。

于是这些真机故障全部失去影响：
  · 卡片分批渲染 → 每批都是同一 key 的候选，取最长的；
  · 重绘换指纹     → 根本不用指纹做身份，用 (发送者, 时间窗)；
  · 锚点丢失       → 缓冲在内存里，不依赖相邻帧比对；
  · 系统通知插队   → 它不参与归并（早被 `_is_system_note` 剔掉）。

代价是消息晚 `quiet` 秒到群。这是刻意的取舍：**晚几秒 > 丢掉**。

## 归并键怎么选

不能用指纹（会因为重绘而变），不能用 `uid`（同上）。用「发送者 + 时间窗」：

  · 同一个机器人一天会发很多条日报，不能只看发送者 → 必须带时间窗；
  · 时间窗取「最后一次见到它」往前 `span` 秒 —— 卡片持续重绘期间一直往
    后滚，重绘停了窗口就自然关闭。
  · 窗口内同一发送者的**不同消息**（比如连发两条「收到」）会被误并成一条。
    这是本方案唯一的已知风险，用「内容差异太大就不合并」的判据压住：
    `_join_fragments` 拼不上（两段互不包含、也没有重叠）就当成两条。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..fingerprint import norm_sender

# 一条消息静默多久才发。太短等于没攒（卡片长不全），太长群里觉得慢。
# 真机实测 H5 卡片从首帧到长全约 1~2 秒，留一倍余量。
QUIET = 3.0

# 上限：超过这个时间还一直有「更长版本」冒出来（病态卡片），按现状发。
# 防「内容一直变 → 永远不发」。
MAX_WAIT = 20.0

# 归并窗口：同一条消息的后续分片必须在这么近的时间内出现。卡片重绘是
# 连续的（毫秒级间隔），用它把「同一条」和「下一分钟的同类日报」分开。
SPAN = 30.0


@dataclass
class _Entry:
    """缓冲里的一条消息（可能是半成品）。"""
    sender: str                 # 原样保留，投递时要显示
    content: str                # 目前见过的最全版本
    chat_name: str = ""
    first_at: float = 0.0       # 首次见到（monotonic）
    last_at: float = 0.0        # 最近一次见到 / 更新
    extra: dict = field(default_factory=dict)   # 交给 Message 的其它字段

    @property
    def key_sender(self) -> str:
        return norm_sender(self.sender)


class PendingBuffer:
    """按「发送者 + 时间窗」归并同一张卡片的分批内容，静默后取最全版本投递。

    线程安全：网关的 HTTP 线程会反复 `offer()`，另有 flusher 线程定期
    `drain()`。两把锁分开 —— 归并要在锁内（判重、比长度），**投递必须在锁外**
    （下游 `on_message` 会同步等微信把消息发出去，可能几秒；持锁发会把手机上
    的下一次上报全堵住）。
    """

    def __init__(self, *, quiet: float = QUIET, max_wait: float = MAX_WAIT,
                 span: float = SPAN):
        self.quiet = quiet
        self.max_wait = max_wait
        self.span = span
        self._lock = threading.Lock()
        self._items: list[_Entry] = []

    # --- 写入 -----------------------------------------------------

    def offer(self, sender: str, content: str, *, chat_name: str = "",
              join=None, **extra) -> str:
        """收一条候选。返回 ``"new"`` / ``"grow"`` / ``"same"`` / ``"skip"``。

        ``join(old, new) -> str | None`` 是拼接函数（复用网关里 `_join_fragments`
        的实现，别在这里重写一份 —— 两份必然漂移）。返回 ``None`` 表示
        「这两段拼不上，不是同一条」。
        """
        now = time.monotonic()
        ns = norm_sender(sender)
        with self._lock:
            cand = None
            for e in self._items:
                if e.key_sender != ns:
                    continue
                if now - e.last_at > self.span:
                    continue                    # 太久以前的了，不是同一条
                cand = e
                break

            if cand is None:
                self._items.append(_Entry(
                    sender=sender, content=content, chat_name=chat_name,
                    first_at=now, last_at=now, extra=dict(extra)))
                return "new"

            if content == cand.content:
                cand.last_at = now             # 同样的内容又来一次：续命不发
                return "same"

            merged = None
            if join is not None:
                merged = join(cand.content, content)
            else:
                merged = self._default_join(cand.content, content)

            if merged is None:
                # 拼不上：同一发送者在窗口内的**另一条**消息（连发两条「收到」
                # 那种）。当成新的排队，别硬塞进上一条。
                self._items.append(_Entry(
                    sender=sender, content=content, chat_name=chat_name,
                    first_at=now, last_at=now, extra=dict(extra)))
                return "new"

            if len(merged) > len(cand.content):
                cand.content = merged
                cand.sender = sender or cand.sender
                cand.last_at = now
                for k, v in extra.items():
                    cand.extra.setdefault(k, v)
                return "grow"

            # 这一版不比已有的全（少见：卡片重绘后内容变短）。仍要续命，
            # 否则「一直在重绘但长不长」的卡片会每 quiet 秒发一次。
            cand.last_at = now
            return "same"

    @staticmethod
    def _default_join(old: str, new: str) -> str | None:
        """没给拼接函数时的保守兜底：包含关系取长的，否则视为不同消息。"""
        a, b = old or "", new or ""
        if not a or not b:
            return a or b
        if a in b:
            return b
        if b in a:
            return a
        return None

    # --- 取出 -----------------------------------------------------

    def drain(self, now: float | None = None) -> list[_Entry]:
        """取出所有「已经静默够久」的条目，并从缓冲里移除。

        判据：``now - last_at >= quiet``（不再有更新）**或**
        ``now - first_at >= max_wait``（等太久了，按现状发）。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            ready, keep = [], []
            for e in self._items:
                if (now - e.last_at >= self.quiet
                        or now - e.first_at >= self.max_wait):
                    ready.append(e)
                else:
                    keep.append(e)
            self._items = keep
        return ready

    def flush_all(self) -> list[_Entry]:
        """把缓冲里的全部取出来（停止运行时用 —— 别把已收到的丢掉）。"""
        with self._lock:
            ready, self._items = self._items, []
        return ready

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def snapshot(self) -> list[tuple[str, str, float]]:
        """只读快照，给日志/排障用：(发送者, 内容, 已等待秒数)。"""
        now = time.monotonic()
        with self._lock:
            return [(e.sender, e.content, now - e.first_at) for e in self._items]
