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

时间过滤
    网关**不做**时间过滤，也不建基线 —— 这两件事由手机端完成（它自己知道
    哪些是新消息）。服务端再判一次反而会把「手机离线一夜、恢复后补报」的
    消息当历史丢掉。

两个端点
    ``POST /messages``
        手机端自己解析好、直接报**消息**。走这条路时手机端承担全部业务判断。

    ``POST /screen``  /  ``GET /screen``
        手机端只把当前界面的**无障碍节点树 XML** 原样送上来（格式与
        ``adb shell uiautomator dump`` 一致），解析、判新老、去重的活儿全留在
        电脑侧。这条路的好处是改规则不用重装手机；``GET /screen`` 取最新一份
        （``?raw=1`` 拿正文，不带就用 JSON 摘要），是排障和分析节点结构的入口。

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

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%m-%d %H:%M", "%H:%M")


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
    lock: threading.Lock = field(default_factory=threading.Lock)


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
        self._reply(200, {"ok": True, "source": "android",
                          "chat_name": self.ctx.chat_name,
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
        if path not in ("/", "/messages", "/screen"):
            self._reply(404, {"ok": False, "error": "未知路径"})
            return
        if not self._auth_ok():
            self._reject_auth()
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

        if count == 1:
            log.info("收到第一帧手机画面：%d 字节，%d 条文本（来自 %s）",
                     length, texts, screen["device"] or "未知名")
        elif count % SCREEN_LOG_EVERY == 0:
            log.info("手机画面已累计 %d 帧（最新 %d 字节 / %d 条文本）",
                     count, length, texts)
        else:
            log.debug("画面 #%d：%d 字节，%d 条文本，md5=%s",
                      count, length, texts, screen["md5"][:8])

        self._reply(200, {"ok": True, "bytes": length, "count": count,
                          "md5": screen["md5"]})


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
        self.chat_name = (dt.chat_names or [""])[0] if dt.chat_names else ""
        self.companies = list(dt.companies or [])

        self.count = {"requests": 0, "accepted": 0, "rejected": 0}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()

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
                   companies=self.companies, on_message=on_message)
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
        log.warning("  注意：本模式**只负责收**。手机报上来的是消息还是画面，"
                    "决定了下游有没有东西可转发 —— 只收到画面时程序不会自动解析，"
                    "别把「网关在收」当成「业务在转」。")

        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
            self._server = None
            self.count = dict(ctx.count)

    def stop(self) -> None:
        self._stop.set()
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
