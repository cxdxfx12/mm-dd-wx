"""安卓采集端网关（mode: android）。

手机上的小 App 用无障碍服务读钉钉群消息，把结果 POST 到这里；本模块只做
「收 → 转成 Message → 交给下游」，不掺任何业务判断（去重、路由、格式化、
限流、投递、告警全在下游，一行都不用改）。

为什么这么切
    `bridge/sources/base.py` 的 Source 抽象本来就为「换一种取数方式」而设。
    网关接进来以后，`BridgeService.handle()` 之后的一切照旧复用 —— 这也是
    这套代码唯一需要改动的采集侧接入点。

安全（必读）
    网关要监听局域网（手机得连得上），所以 **token 是强制的**：token 为空时
    `check()` 直接不通过、`run()` 拒绝启动，不做「没配就放行」。原因是能往
    这里 POST 就等于能往你的微信群发消息 —— 裸奔等于把转发权限开放给整个
    网段。token 比较用 `hmac.compare_digest`，避免计时侧信道。

幂等
    手机端算好指纹放进 `fp` 字段，`raw_id = fp:seq`，uid 稳定可复现；重传
    （网络抖动、手机没收到响应）会得到同一个 uid，被下游 `is_duplicate` 挡掉。
    手机端没带 `fp` 时本模块用 `bridge.fingerprint` 补算 —— 与 OCR 路径
    **同一份实现**，避免两边算法漂移导致同一条消息被采两次。

时间过滤与基线
    走 ``/messages`` 时这两个判断由手机端完成（它自己知道哪些是新消息），本模块
    不再判 —— 再判一次反而会把「手机离线一夜、恢复后补报」的消息当历史丢掉。

    走 ``/screen`` 时情况相反：手机端只送「这一屏长什么样」，判新老只能在电脑侧做。
    这时由 ``dingtalk.android_parse`` 开关决定是否启用
    :class:`bridge.sources.android_parse.FrameDiffer` —— 它第一帧只建基线（一条
    不发），之后只转发基线（锚点）之后的部分。默认关闭：没看过真机画面的解析
    结果之前不要开，否则可能把认错的文本当真消息发进微信群。

两个端点
    ``POST /messages``
        手机端自己解析好、直接报**消息**。走这条路时手机端承担全部业务判断。

    ``POST /screen``  /  ``GET /screen``
        手机端只把当前界面的**无障碍节点树 XML** 原样送上来（格式与
        ``adb shell uiautomator dump`` 一致）。``dingtalk.android_parse = true``
        时本模块负责解析、判新老，再交给下游；= false 时只存不解析。
        ``GET /screen`` 取最新一份（``?raw=1`` 拿正文，不带就用 JSON 摘要），
        是排障和分析节点结构的入口。

    两条路可以同时存在，互不影响。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import Config
from ..fingerprint import fp_of
from ..models import MSG_TYPES, Message
from .android_parse import (FrameDiffer, ParseError, parse_frame,
                            units_to_messages)
from .base import MessageHandler, Source, infer_company

log = logging.getLogger("bridge.source.android")

MAX_BODY = 2 * 1024 * 1024          # 单次请求体上限
# 节点树的体积比消息大得多（一屏几百个节点，中文正文，实测 30~300 KB），
# 所以给 /screen 单独放宽到 4 MB。上限仍然要有：监听局域网意味着任何人都能来灌。
MAX_SCREEN_BODY = 4 * 1024 * 1024
MAX_BATCH = 200                     # 单次请求最多几条消息
DEFAULT_PORT = 8765
# 每收到这么多帧画面打一条 INFO。帧率可能到 0.5/s，全打会把日志淹掉；
# 全不打又会让「手机到底还在不在上报」变成一个查不出来的问题。
SCREEN_LOG_EVERY = 30
# 连续这么多帧都认不出「聊天页」→ 升到 WARNING（会触发钉钉告警）。
# 认不出意味着一条都不转发，属于「程序在跑但静默失效」，必须让人知道。
UNKNOWN_PAGE_ALERT_AFTER = 3
# 看门狗多久检查一次「手机还在不在」（秒）。比心跳间隔（手机端 60s）小得多，
# 所以失联的发现延迟最多是「心跳间隔 + 这个 tick」。
OFFLINE_TICK = 10.0
# 失联期间重复提醒的间隔（秒）。告警本身有冷却，但日志也需要一个节奏 ——
# 每 10 秒刷一条 WARNING 会把日志文件灌满，真问题反而被盖掉。
OFFLINE_REMIND_EVERY = 1800.0

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%m-%d %H:%M", "%H:%M")


def _online_now(ctx: "_Ctx") -> bool:
    """此刻手机算不算在线 —— 按「最后一次心跳距今多久」算，不是「曾经连上过」。

    这里踩过一次：早先写成 ``bool(ctx.last_seen)``，于是手机锁屏八分钟、一次
    心跳都没有的时候，``/ping`` 还在回 ``online: true``，把排查的人（连我自己）
    引到「网络没问题，是不是电脑侧卡了」的错误方向上。字段名既然叫 online，
    就必须回答「现在」，而不是「历史上」。
    """
    last = ctx.last_seen
    if last <= 0.0:
        return False
    span = ctx.offline_after
    if span <= 0.0:          # 关掉失联检测时，只能退回「收到过就算在线」
        return True
    return (time.monotonic() - last) <= span


# --------------------------------------------------------------- 失联看门狗

def _fmt_span(seconds: float) -> str:
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分钟"
    return f"{s // 3600} 小时 {s % 3600 // 60} 分钟"


def _watchdog_step(ctx: "_Ctx", started: float, now: float,
                   offline_after: float) -> tuple[int, str, tuple] | None:
    """判一次「手机还在不在」，顺带把状态迁移写回 ``ctx``。

    返回 ``(日志级别, 模板, 参数)``，不该说话时返回 ``None``。

    抽成纯函数（只依赖传入的 ``now``、不自己取时钟）是为了**可测**：
    走线程 + sleep 的测试既慢又会在负载高时随机红，而这里的每一条分支
    （从没收到过 / 掉线 / 恢复 / 旧版没有心跳）都是必须逐条锁住的现场结论。
    """
    msg: tuple[int, str, tuple] | None = None
    with ctx.seen_lock:
            last, heard = ctx.last_seen, ctx.heard_heartbeat
            state, device = ctx.state, ctx.last_device
            wall, last_alert = ctx.last_seen_wall, ctx.last_alert_at
            gap = (now - last) if last > 0.0 else (now - started)
            new_state = state

            # ---- 情况一：启动之后一次都没收到过 ----
            if last <= 0.0:
                if state != "missing" and gap >= offline_after:
                    new_state = "missing"
                    ctx.last_alert_at = now
                    msg = (logging.WARNING,
                           "安卓网关已就绪 %s，但一次都没有收到手机的上报。"
                           "手机端请自查：App 在运行吗？无障碍服务还开着吗？"
                           "填的 IP/端口/token 与电脑端一致吗（token 不对会一直是"
                           " 401）？手机和电脑在同一 Wi-Fi 吗？",
                           (_fmt_span(gap),))
                elif state == "missing" and now - last_alert >= OFFLINE_REMIND_EVERY:
                    ctx.last_alert_at = now
                    msg = (logging.WARNING,
                           "手机端仍然没有任何上报（已经 %s）。",
                           (_fmt_span(gap),))

            # ---- 情况二：有上报但从没心跳（旧版 APK，没有心跳就没法判失联）----
            elif not heard:
                if state != "online":
                    new_state = "online"
                    msg = (logging.INFO,
                           "已收到手机（%s）的上报，但它没有发心跳 —— 多半是旧版 APK。"
                           "旧版只在画面变化时上报，无法区分「App 被系统杀了」和"
                           "「群里没人说话」，所以失联检测对旧版不启用（否则必然"
                           "误报）。要准确检测请把手机端升级到 0.2.0。",
                           (device or "未知机型",))

            # ---- 情况三：正常在线 ----
            elif gap <= offline_after:
                if state == "offline":
                    away = now - (ctx.offline_last_seen or last)
                    ctx.offline_last_seen = 0.0
                    msg = (logging.WARNING,
                           "手机端恢复上报（断开了约 %s）。", (_fmt_span(away),))
                elif state == "missing":
                    msg = (logging.WARNING,
                           "手机端终于连上了（网关启动后空等了 %s）。",
                           (_fmt_span(now - started),))
                new_state = "online"

            # ---- 情况四：失联 ----
            elif state != "offline" or now - last_alert >= OFFLINE_REMIND_EVERY:
                new_state = "offline"
                ctx.last_alert_at = now
                ctx.offline_last_seen = last
                msg = (logging.WARNING,
                       "手机端失联：已经 %s 没有心跳（最后一次 %s，机型 %s）。"
                       "常见原因：系统把 App 清理了 / 锁屏断网 / 手机换了 Wi-Fi。"
                       "处理：打开手机上的采集端 App，看「最后动作」那一行是不是"
                       "「上报失败」；若 App 已被关掉，重新打开并确认无障碍服务"
                       "仍然开启。",
                       (_fmt_span(gap), wall or "未知", device or "未知机型"))

            ctx.state = new_state
    return msg


def _watchdog(ctx: "_Ctx", offline_after: float,
              stop: threading.Event) -> None:
    """盯着「手机还在不在」，状态**只在变化时**说话。

    为什么必须有这东西：手机端 App 被系统清掉、锁屏断网之后，电脑侧什么异常
    都收不到 —— 没有连接、没有请求、没有报错。表现出来的样子是「程序安安静静
    在跑」，而实际上一条消息都不会再转发。这种静默失效只能靠主动探测发现。

    判据用**心跳**（手机端每 60 秒 POST /ping），不用「多久没收到画面」：
    群里半天没人说话时画面本来就不变、手机也不上报，按画面判会天天误报，
    而误报几次之后人就再也不看告警了 —— 那比不告警更糟。

    决策在锁内、日志在锁外。看着绕，但必须这样：告警处理器是**同步发 HTTP**
    到钉钉的（要几秒），持锁写日志会把手机下一次上报一起卡住，
    现场表现成「一告警就断线」，反而更像手机出问题。
    """
    started = time.monotonic()
    while not stop.wait(OFFLINE_TICK):
        msg = _watchdog_step(ctx, started, time.monotonic(), offline_after)
        if msg is not None:
            log.log(msg[0], msg[1], *msg[2])


# --------------------------------------------------------------- 报文解析

def _parse_time(value: Any, now: datetime | None = None) -> datetime:
    """宽容解析手机端给的时间；认不出来就退回本机时间。

    时间只用于展示与下游格式，不参与去重（去重靠指纹），所以这里刻意取
    「宁可用本机时间，也不拒收」：手机端时区、格式的差异不值得把消息丢掉。
    支持 ``"2026-10-16 20:31:00"``、``"20:31"``、秒级/毫秒级时间戳。
    """
    now = now or datetime.now()
    if value is None or value == "":
        return now
    if isinstance(value, bool):
        return now
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:                       # 毫秒时间戳
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            return now

    text = str(value).strip().replace("T", " ")
    if text.isdigit():
        return _parse_time(int(text), now)
    text = text.split(".")[0].strip()
    for fmt in TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%m-%d %H:%M":
            return dt.replace(year=now.year)
        if fmt == "%H:%M":
            return now.replace(hour=dt.hour, minute=dt.minute,
                               second=0, microsecond=0)
        return dt
    return now


def build_messages(payload: dict, *, chat_name: str, companies: list[str],
                   now: datetime | None = None) -> tuple[list[Message], list[str]]:
    """把一次上报的报文变成 Message 列表，返回 ``(消息, 跳过原因)``。

    跳过不算错误：手机端可能读到纯图片/纯文件消息（没有文本），那是正常的，
    不该让它一直重传，所以也回 200。

    ``payload`` 既可以是 ``{"messages": [...]}``，也可以是单条消息对象本身
    （便于用 curl 手工验证）。
    """
    raw = payload.get("messages")
    if raw is None and ("content" in payload or "sender" in payload):
        raw = [payload]                     # 单条简写
    if not isinstance(raw, list):
        raise ValueError("messages 必须是数组")

    # 会话名优先级：报文里带的最准（手机知道自己在哪个群）→ 配置里第一个当兜底。
    # 配置里的常是片段（如「数字质控」），用来做路由的子串匹配，不该盖掉真名。
    default_chat = str(payload.get("chat_name") or "").strip() or chat_name
    out: list[Message] = []
    skipped: list[str] = []

    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            skipped.append(f"第 {idx + 1} 条不是对象")
            continue

        sender = str(item.get("sender") or "").strip()
        content = str(item.get("content") or "").strip()
        if not content:
            skipped.append(f"第 {idx + 1} 条没有文本内容")
            continue

        msg_chat = str(item.get("chat_name") or "").strip() or default_chat

        msg_type = str(item.get("msg_type") or "text").strip().lower()
        if msg_type not in MSG_TYPES:
            msg_type = "unknown"

        try:
            seq = max(1, int(item.get("seq") or 1))
        except (TypeError, ValueError):
            seq = 1

        fp = str(item.get("fp") or "").strip() or fp_of(sender, content)
        # 展示用昵称：自己发的统一成「(我)」，与 OCR 路径口径一致
        sender_name = "(我)" if item.get("is_self") else sender

        out.append(Message(
            source="android",
            chat_id=str(item.get("chat_id") or "").strip(),
            chat_name=msg_chat,
            sender_id="",
            sender_name=sender_name,
            company=infer_company(companies, msg_chat, sender_name, content),
            content=content,
            msg_type=msg_type,
            msg_time=_parse_time(item.get("time"), now),
            raw_id=f"{fp}:{seq}",
        ))
    return out, skipped


# --------------------------------------------------------------- 网络工具

def lan_ips() -> list[str]:
    """本机在局域网里的 IPv4 地址，用于提示手机该连哪个地址。"""
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))      # 不发包，只让系统选出出口网卡
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


# --------------------------------------------------------------- HTTP 服务

def _screen_meta(screen: dict | None) -> dict:
    """画面缓冲的对外摘要。

    **刻意不含 XML 正文**：正文可能几百 KB，塞进 JSON 首页或日志里只会让
    「看一眼状态」这件事变成「拉一份大文件」。要正文走 ``GET /screen?raw=1``。
    """
    if not screen:
        return {"received": False, "hint": "还没收到任何画面"}
    ts = screen.get("ts")
    age = None if not ts else max(0.0, round(time.time() - float(ts), 1))
    return {
        "received": True,
        "at": screen.get("at", ""),
        "age_sec": age,
        "bytes": screen.get("bytes", 0),
        "md5": screen.get("md5", ""),
        "texts": screen.get("texts", 0),
        "device": screen.get("device", ""),
        "reason": screen.get("reason", ""),
        "count": screen.get("count", 0),
    }


@dataclass
class _Ctx:
    token: str
    chat_name: str
    companies: list[str] = field(default_factory=list)
    on_message: MessageHandler | None = None
    count: dict = field(default_factory=lambda: {"requests": 0, "accepted": 0,
                                                 "rejected": 0})
    # 最新一帧节点树。只留最新一份：这一层的语义就是「手机现在长什么样」，
    # 历史帧由解析器自己按需比对，网关不做序列缓存。
    screen: dict | None = None
    # 帧解析（dingtalk.android_parse 才开启）。differ 有状态 —— 它记着上一帧的
    # 锚点，用来判「哪几条是新的」，所以必须跟 ctx 一起走、由同一把锁保护。
    parse: bool = False
    differ: FrameDiffer | None = None
    # 失联判定阈值（秒），0 = 不检测。只用于 /ping 的回包与看门狗。
    offline_after: float = 300.0
    # 上一次「不是聊天页，本帧跳过」的说明。用户点开 @ 面板会连着报好几帧，
    # 内容一样就没必要重复写日志（日志是滚动文件，刷满了会盖掉真问题）。
    last_skip_note: str = ""
    # 连续多少帧认不出聊天页。只数「认不出来」，明确的 @ 面板/群信息不计。
    skip_streak: int = 0
    # ---- 在线状态（失联告警用）----
    # 最后一次收到手机上报的时刻（time.monotonic）。0 = 至今一次都没收到。
    last_seen: float = 0.0
    last_seen_wall: str = ""              # 同一时刻的人话版本，写进告警文案
    last_device: str = ""                 # 手机型号，告警时好认出是哪台
    # 是否收到过**心跳**。判定失联必须有它：手机端只在画面变化时上报，
    # 群里半天没人说话时它本来就不发东西 —— 按「多久没收到上报」判会天天误报。
    heard_heartbeat: bool = False
    # waiting（启动后还没收到）/ online / offline / missing（一直没收到）
    state: str = "waiting"
    last_alert_at: float = 0.0            # 上次告警时刻，用来控制重复提醒的节奏
    # 掉线那一刻「最后一次心跳」的时间。恢复时要用它算「断了多久」——
    # 不能用恢复后的 gap，那时 last_seen 刚被心跳刷新，算出来永远是 0 秒，
    # 日志会变成「断开了约 0 秒」，正好把最想看的信息抹掉。
    offline_last_seen: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    # 在线状态用**单独的锁**。绝不能复用上面那把：`_post_screen` 是拿着 lock
    # 再去调 touch 的，同一把不可重入的 Lock 会当场死锁 —— 表现是「收到画面后
    # 网关整个卡住，手机上报超时」，而日志里什么都没有。
    seen_lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self, device: str = "", heartbeat: bool = False) -> None:
        """记一次「手机还活着」。心跳与业务上报都算。

        这里**只更新时间戳、不改状态**：状态机和它的日志全交给看门狗一处做，
        否则「恢复」的日志会出现在请求线程里、与看门狗的判定互相打架，
        现场就成了「一会儿说恢复、一会儿说失联」。
        """
        with self.seen_lock:
            self.last_seen = time.monotonic()
            self.last_seen_wall = time.strftime("%H:%M:%S")
            if device:
                self.last_device = device
            if heartbeat:
                self.heard_heartbeat = True


class _Handler(BaseHTTPRequestHandler):
    """只实现两个端点。

    刻意用 HTTP/1.0（不开 keep-alive）：消息频率很低，每请求一个连接的开销
    可以忽略，换来的是「不会因为漏写 Content-Length 而挂住」。
    """

    server_version = "ddwx-android-gateway/1.0"
    ctx: _Ctx = None                     # 由 _make_handler 注入

    # --- 基础设施 ---

    def log_message(self, fmt: str, *args) -> None:
        log.debug("网关 %s %s", self.address_string(), fmt % args)

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass                          # 手机提前断开，不值得记一条错误

    def _drain(self, length: int, cap: int = 8 * 1024 * 1024) -> None:
        """拒绝之前先把请求体读掉（最多读完 ``cap`` 字节）。

        不读完就回 413，客户端还在往里写，socket 会被 RST —— 客户端看到的不是
        「请求体过大」而是一句「连接被重置」，人就不知道到底哪儿错了。
        """
        remaining = min(max(0, length), cap)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

    def _path(self) -> str:
        p = urlparse(self.path).path
        return p.rstrip("/") or "/"

    def _auth_ok(self) -> bool:
        # 两个头名都认：X-Bridge-Token 是本文档规定的口径，X-Auth-Token 是手机端
        # 那个自建 App 用的。它们指同一个 token，不构成额外的攻击面，
        # 但少了这个别名就会出现「App 里 token 填得一字不差却一直 401」这种
        # 极难自查的问题。
        got = (self.headers.get("X-Bridge-Token")
               or self.headers.get("X-Auth-Token") or "")
        if not got:
            # 也认 ?token=xxx —— 方便用浏览器或一行 curl 做连通性自测
            got = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        if not got:
            return False
        # 编成 bytes 再比：hmac.compare_digest 对 str 要求纯 ASCII，
        # 用户把 token 填成中文时会直接抛 TypeError（变成 500），
        # 而这里应该是一个干净的 401。
        return hmac.compare_digest(got.encode("utf-8"),
                                   (self.ctx.token or "").encode("utf-8"))

    def _reject_auth(self) -> None:
        self._reply(401, {"ok": False, "error": "token 不对",
                          "hint": "请求头带上 X-Bridge-Token，值与本机 "
                                  "config.yaml 的 dingtalk.gateway_token 一致"})

    # --- 端点 ---

    def do_GET(self) -> None:                                 # noqa: N802
        path = self._path()
        if path not in ("/", "/ping", "/screen"):
            self._reply(404, {"ok": False, "error": "未知路径"})
            return
        if not self._auth_ok():
            self._reject_auth()
            return
        if path == "/screen":
            self._get_screen()
            return
        if path == "/ping":
            # 只有带 hb=1 才算「手机还在」：浏览器/curl 里随手打开一次只是自测，
            # 若也刷新在线状态，人工排查反而会把真正的失联掩盖掉。
            q = parse_qs(urlparse(self.path).query)
            if (q.get("hb") or [""])[0] in ("1", "true", "yes"):
                self.ctx.touch(device=(q.get("device") or [""])[0],
                               heartbeat=True)
        self._reply(200, {"ok": True, "source": "android",
                          "chat_name": self.ctx.chat_name,
                          "online": _online_now(self.ctx),
                          "state": self.ctx.state,
                          "last_seen": self.ctx.last_seen_wall,
                          "device": self.ctx.last_device,
                          "heartbeat": self.ctx.heard_heartbeat,
                          "offline_after": self.ctx.offline_after,
                          "screen": _screen_meta(self.ctx.screen),
                          "stats": dict(self.ctx.count)})

    def _get_screen(self) -> None:
        """取最新一帧节点树。默认给 JSON 摘要，``?raw=1`` 给 XML 正文。"""
        screen = self.ctx.screen
        if not screen:
            self._reply(404, {
                "ok": False, "error": "还没有收到任何画面",
                "hint": "手机端网络通了、无障碍服务开了、钉钉停在群聊页之后，"
                        "这里才会有内容"})
            return
        raw = (parse_qs(urlparse(self.path).query).get("raw") or [""])[0]
        if raw in ("1", "true", "yes"):
            data = (screen.get("xml") or "").encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        meta = _screen_meta(screen)
        meta["ok"] = True
        self._reply(200, meta)

    def do_POST(self) -> None:                                # noqa: N802
        path = self._path()
        if path not in ("/", "/messages", "/screen", "/ping"):
            self._reply(404, {"ok": False, "error": "未知路径"})
            return
        if not self._auth_ok():
            self._reject_auth()
            return

        if path == "/ping":
            # 手机端的**心跳**。它存在的唯一理由，是让电脑侧能区分
            # 「手机好好的，只是群里没人说话」和「手机端已经死了」——
            # 前者的表现是「没有新消息」，后者也是，光看消息永远分不出来。
            # 手机端会带一个 {} 作请求体；curl 手测可能什么都不带。
            # 不读掉又直接回包的话，客户端还在写 → RST → 它只看到「连接被重置」。
            try:
                hb_len = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                hb_len = 0
            self._drain(hb_len)
            device = (self.headers.get("X-Ddwx-Device") or "").strip()
            self.ctx.touch(device=device, heartbeat=True)
            self._reply(200, {"ok": True, "source": "android",
                              "chat_name": self.ctx.chat_name,
                              "last_seen": self.ctx.last_seen_wall,
                              "offline_after": self.ctx.offline_after})
            return

        limit = MAX_SCREEN_BODY if path == "/screen" else MAX_BODY
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._reply(400, {"ok": False, "error": "请求体为空"})
            return
        if length > limit:
            self._drain(length)
            self._reply(413, {"ok": False,
                              "error": f"请求体过大（{length} > {limit} 字节）"})
            return

        raw = self.rfile.read(length)

        if path == "/screen":
            self._post_screen(raw, length)
            return

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._reply(400, {"ok": False, "error": f"报文不是合法 JSON：{exc}"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"ok": False, "error": "报文顶层必须是对象"})
            return

        items = payload.get("messages")
        if isinstance(items, list) and len(items) > MAX_BATCH:
            self._reply(413, {"ok": False,
                              "error": f"单次最多 {MAX_BATCH} 条，收到 {len(items)} 条"})
            return

        try:
            msgs, skipped = build_messages(
                payload, chat_name=self.ctx.chat_name,
                companies=self.ctx.companies)
        except (ValueError, TypeError) as exc:
            self._reply(400, {"ok": False, "error": str(exc)})
            return

        # 能在 /messages 里报消息，本身就说明手机还活着（老版 APK 没有心跳，
        # 靠这一条也能让「启动后一次都没收到」的告警不再误报）。
        self.ctx.touch(device=(self.headers.get("X-Ddwx-Device") or "").strip())

        for msg in msgs:
            try:
                self.ctx.on_message(msg)
            except Exception:                                  # 单条出错不拖垮整批
                log.exception("下游处理消息失败：%s", msg.uid)

        with self.ctx.lock:
            self.ctx.count["requests"] += 1
            self.ctx.count["accepted"] += len(msgs)
            self.ctx.count["rejected"] += len(skipped)

        if msgs:
            head = msgs[0].content[:40].replace("\n", " ")
            log.info("安卓端上报 %d 条（跳过 %d 条）：%s", len(msgs), len(skipped), head)

        self._reply(200, {"ok": True, "accepted": len(msgs),
                          "skipped": skipped, "chat_name": self.ctx.chat_name})

    def _post_screen(self, raw: bytes, length: int) -> None:
        """收一帧节点树。只做「存下来 + 记元信息」，不解析 —— 解析是采集端的事。"""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self._reply(400, {"ok": False, "error": f"画面不是 UTF-8：{exc}"})
            return
        # 入口形状检查。这一条的价值在于把「IP 填成了别的服务」「手机端版本不对」
        # 这类问题在门口就挡下来 —— 否则后面解析器要拿一堆垃圾去猜结构，
        # 报出来的错会离真正的原因非常远。
        head = text[:600]
        if "<hierarchy" not in head:
            self._reply(400, {
                "ok": False, "error": "请求体不像 uiautomator 节点树 XML",
                "hint": "应当以 <?xml 开头且含 <hierarchy>；"
                        "确认手机端 App 是配套版本"})
            return

        try:
            texts = int(self.headers.get("X-Ddwx-Texts") or 0)
        except ValueError:
            texts = 0

        with self.ctx.lock:
            prev = self.ctx.screen or {}
            count = int(prev.get("count", 0)) + 1
            screen = {
                "xml": text,
                "bytes": length,
                # 手机端给了摘要就用它（省一次全量哈希），没给就自己算
                "md5": (self.headers.get("X-Ddwx-Md5") or "").strip()
                       or hashlib.md5(raw).hexdigest(),
                "texts": texts,
                "device": (self.headers.get("X-Ddwx-Device") or "").strip(),
                "reason": (self.headers.get("X-Ddwx-Reason") or "").strip(),
                "ts": time.time(),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "count": count,
            }
            self.ctx.screen = screen
            # 解析 + 判新老都在这把锁里：帧必须**按到达顺序**喂给 FrameDiffer，
            # 否则两帧并发时锚点会错位，表现为偶尔漏发或重复发。这两步是纯
            # 内存计算（几毫秒），占锁时间可控。
            pending: list = []
            note = ""
            if self.ctx.parse and self.ctx.on_message is not None:
                pending, note = self._ingest(text)

        # 记「手机还活着」。必须在锁外调：touch 走的是另一把锁，
        # 但把它放在这里能让 hold 锁的时间只包含解析。
        self.ctx.touch(device=screen["device"])

        if count == 1:
            log.info("收到第一帧手机画面：%d 字节，%d 条文本（来自 %s）",
                     length, texts, screen["device"] or "未知名")
        elif count % SCREEN_LOG_EVERY == 0:
            log.info("手机画面已累计 %d 帧（最新 %d 字节 / %d 条文本）",
                     count, length, texts)
        else:
            log.debug("画面 #%d：%d 字节，%d 条文本，md5=%s",
                      count, length, texts, screen["md5"][:8])

        # 投递在锁外：下游 on_message 会同步等微信把消息发出去（可能几秒），
        # 占着锁会把 GET /screen 和手机的下一次上报一起卡住。
        emitted = self._emit(pending)

        body = {"ok": True, "bytes": length, "count": count,
                "md5": screen["md5"]}
        if self.ctx.parse:
            # 手机端日志里直接能看到「这一帧有没有产生转发」，省得两头对时间
            body["messages"] = emitted
            if note:
                body["note"] = note
        self._reply(200, body)

    # --- 画面解析（仅 dingtalk.android_parse = true）---

    def _ingest(self, text: str) -> tuple[list, str]:
        """一帧节点树 → 本帧**新增**的消息单元。返回 ``(单元列表, 说明)``。

        解析失败**不能**回 4xx/5xx：手机端收到错误会重传同一帧，而这一帧的
        形状问题是重传解决不了的，只会把日志刷满。所以记一条 WARNING、
        照常回 200，让采集继续。
        """
        try:
            frame = parse_frame(text)
        except ParseError as exc:
            log.warning("手机画面解析失败（这一帧跳过）：%s", exc)
            return [], "解析失败"

        if not frame.is_chat:
            # 用户点开了 @ 面板、群信息、搜索页 —— 屏上是一排排人名和身份标签，
            # 结构和消息列表几乎一样。必须整帧丢掉，**连基线都不能动**：
            # 拿这些人名当基线，回到聊天页时会把整屏旧消息判成新消息。
            note = f"不是聊天页（{frame.page_reason}），本帧跳过"
            if frame.page == "other":
                # 明确的非聊天页（认出了「选择要@的人」这类文案）：这是用户的
                # 正常操作，日志只能到 INFO —— WARNING 会被钉钉告警处理器抓走，
                # 用户每点一次 @ 就收到一条手机告警。
                self.ctx.skip_streak = 0
                if note != self.ctx.last_skip_note:
                    log.info("安卓画面解析：%s", note)
                    self.ctx.last_skip_note = note
            else:
                # 认不出来的页面才是真问题：多半是钉钉改版把输入区文案改了，
                # 那样会「程序在跑、一条不转」。连续几帧都认不出就升级成
                # WARNING —— 让它进钉钉告警，别静默失效。
                self.ctx.skip_streak += 1
                if self.ctx.skip_streak == UNKNOWN_PAGE_ALERT_AFTER:
                    log.warning(
                        "连续 %d 帧认不出聊天页（%s）—— 钉钉可能改版了，"
                        "现在一条消息都不会转发。用 tools/android_capture.py "
                        "抓一帧看 page_reason", self.ctx.skip_streak, frame.page_reason)
                    self.ctx.last_skip_note = note
            return [], note

        self.ctx.skip_streak = 0
        self.ctx.last_skip_note = ""
        differ = self.ctx.differ
        if differ is None:
            differ = self.ctx.differ = FrameDiffer()
        diff = differ.feed(frame.units)

        if diff.baseline:
            log.info("安卓画面解析：已建立基线（当前屏 %d 条），"
                     "此后只转发基线之后的新消息", len(frame.units))
            return [], "已建立基线"
        if diff.anchor_lost:
            log.warning("安卓画面解析：上一帧的锚点在本帧里找不到（翻历史／换群／"
                        "一屏全换）—— 本帧不转发，基线已重建到当前画面")
            return [], "锚点丢失"
        return list(diff.units), ""

    def _emit(self, units: list) -> int:
        """把消息单元交给下游，返回成功条数。"""
        if not units or self.ctx.on_message is None:
            return 0
        msgs = units_to_messages(units, chat_name=self.ctx.chat_name or "",
                                 companies=self.ctx.companies)
        emitted = 0
        for msg in msgs:
            try:
                self.ctx.on_message(msg)
                emitted += 1
            except Exception:                                  # 单条出错不拖垮整帧
                log.exception("下游处理消息失败：%s", msg.uid)
        with self.ctx.lock:
            self.ctx.count["accepted"] += emitted
        if emitted:
            head = msgs[0].content[:40].replace("\n", " ")
            log.info("安卓画面新增 %d 条消息：%s", emitted, head)
        return emitted


def _make_handler(ctx: _Ctx):
    return type("_BoundHandler", (_Handler,), {"ctx": ctx})


class _GatewayServer(ThreadingHTTPServer):
    """HTTP 服务本体。

    刻意覆写 ``allow_reuse_address``：Python 自带的 HTTPServer 把它设成 True，
    而 Windows 上 SO_REUSEADDR 的语义是「允许另一个进程抢同一个端口」。
    那会让「端口已被占用」退化成一个静默的双绑定 —— 两个进程各接一半连接，
    表现为「手机有时连得上有时连不上」，极难排查。所以 Windows 上关掉；
    POSIX 上保留，避免重启时被 TIME_WAIT 卡住。
    """

    allow_reuse_address = (os.name != "nt")
    daemon_threads = True


# --------------------------------------------------------------- Source

class AndroidGatewaySource(Source):
    """等手机把消息 POST 进来的采集端。"""

    name = "android"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        dt = cfg.dingtalk
        self.host = str(getattr(dt, "gateway_host", "") or "").strip() or "0.0.0.0"
        try:
            self.port = int(getattr(dt, "gateway_port", DEFAULT_PORT)
                            or DEFAULT_PORT)
        except (TypeError, ValueError):
            self.port = DEFAULT_PORT
        self.token = str(getattr(dt, "gateway_token", "") or "").strip()
        self.parse = bool(getattr(dt, "android_parse", False))
        try:
            self.offline_after = float(getattr(dt, "android_offline_after", 300.0))
        except (TypeError, ValueError):
            self.offline_after = 300.0
        if self.offline_after < 0:
            self.offline_after = 0.0          # 负数按「关闭」处理，别让它变成「秒级误报」
        self.chat_name = (dt.chat_names or [""])[0] if dt.chat_names else ""
        self.companies = list(dt.companies or [])

        self.count = {"requests": 0, "accepted": 0, "rejected": 0}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._watchdog_stop = threading.Event()
        self.online = False                    # 只用于退出时留个快照

    # --- 生命周期 ---

    def check(self) -> tuple[bool, str]:
        if not self.token:
            return False, (
                "安卓网关没配 token（dingtalk.gateway_token）—— 网关要监听局域网，"
                "手机才连得上；没有 token 等于任何人都能往你的微信群发消息。"
                "请在 config.yaml 的 dingtalk 段填一个随机字符串，"
                "并让手机端填同一个值")
        if not (0 < self.port < 65536):
            return False, f"网关端口不合法：{self.port}"
        if not self.chat_name and not self.companies:
            # 不是硬错误：路由里若用 「*」 或按公司匹配仍可用，只提示一下
            log.warning("未配置 dingtalk.chat_names，安卓端必须在报文里带 chat_name，"
                        "否则路由规则里的群名匹配会落空")
        ips = lan_ips()
        where = "、".join(f"http://{ip}:{self.port}" for ip in ips) or \
            f"http://<本机IP>:{self.port}"
        return True, f"监听 {self.host}:{self.port}｜手机端填：{where}"

    def run(self, on_message: MessageHandler) -> None:
        ok, why = self.check()
        if not ok:
            raise RuntimeError(why)

        ctx = _Ctx(token=self.token, chat_name=self.chat_name,
                   companies=self.companies, on_message=on_message,
                   parse=self.parse, offline_after=self.offline_after,
                   differ=FrameDiffer() if self.parse else None)
        self._stop.clear()
        try:
            server = _GatewayServer((self.host, self.port), _make_handler(ctx))
        except OSError as exc:
            raise RuntimeError(
                f"网关端口 {self.host}:{self.port} 起不来：{exc}"
                f"（是不是已被别的程序占用？）") from exc

        self._server = server
        self._ready.set()

        ips = lan_ips()
        log.info("安卓网关已就绪：监听 %s:%d", self.host, self.port)
        for ip in ips:
            log.info("  手机端请填：http://%s:%d   （token 见 config.yaml）",
                     ip, self.port)
        if not ips:
            log.warning("  没探测到局域网 IP，请用 ipconfig 查本机地址")
        log.info("  连通性自测：手机浏览器打开 http://<本机IP>:%d/ping?token=<token>",
                 self.port)
        log.info("  收消息 POST /messages ｜ 收画面 POST /screen ｜ "
                 "本机查最新画面 GET /screen?token=<token>&raw=1")
        if self.parse:
            log.info("  画面解析已开启（dingtalk.android_parse = true）："
                     "第一帧只建基线不转发，之后只转发基线之后的新消息")
        else:
            log.warning("  注意：本模式**只负责收**。手机报上来的是消息还是画面，"
                        "决定了下游有没有东西可转发 —— 只收到画面时程序不会自动解析，"
                        "别把「网关在收」当成「业务在转」。"
                        "要开启解析：config.yaml 的 dingtalk.android_parse 设为 true"
                        "（先用 tools/android_capture.py 看过一帧真实画面的解析结果）")

        # ---- 失联看门狗 ----
        self._watchdog_stop = threading.Event()
        if self.offline_after > 0:
            self._watchdog_stop.clear()
            threading.Thread(target=_watchdog,
                             args=(ctx, self.offline_after, self._watchdog_stop),
                             name="android-watchdog", daemon=True).start()
            log.info("  失联检测已开启：手机超过 %s 没有心跳就告警"
                     "（要准确检测，手机端需 0.2.0 及以上；旧版只会报「一次都没收到」）",
                     _fmt_span(self.offline_after))
        else:
            log.info("  失联检测已关闭（dingtalk.android_offline_after = 0）："
                     "手机端挂了不会有任何提示，只能靠群里没消息自己察觉")

        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            self._watchdog_stop.set()
            server.server_close()
            self._server = None
            self.count = dict(ctx.count)
            self.online = _online_now(ctx)   # 语义同 /ping：退出那一刻是否还在线

    def stop(self) -> None:
        self._stop.set()
        wd = getattr(self, "_watchdog_stop", None)
        if wd is not None:
            wd.set()
        srv = self._server
        if srv is not None:
            # shutdown() 必须与 serve_forever 不在同一个线程里调用，
            # 这里统一丢给临时线程，避免 stop() 被谁调用都安全。
            threading.Thread(target=srv.shutdown, daemon=True).start()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """等监听套接字真正就绪（自检工具与测试用）。"""
        return self._ready.wait(timeout)

    def describe(self) -> str:
        return (f"安卓网关 | 监听 {self.host}:{self.port} | "
                f"会话 {self.chat_name or '(报文自带)'} | "
                f"已收 {self.count['accepted']} 条")
