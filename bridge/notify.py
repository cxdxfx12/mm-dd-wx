"""钉钉机器人：把本程序的运行状况回传给人（告警 + 报告）。

════════════════════════════════════════════════════════════════════════
 能力边界 —— 这是本模块最重要的一段说明
════════════════════════════════════════════════════════════════════════
钉钉「自定义机器人」webhook 是**单向**的：
    只能 POST 消息**进群**，拿不到群里其他人发的消息。
所以它**不能**替代 OCR 采集。取数依旧走
`bridge/sources/dingtalk_ocr.py`（截屏 + 离线 OCR）。

那它解决什么问题？
    本程序最怕的是「静默失效」——钉钉窗口被切走、被最小化，
    或微信端掉线之后，转发会**停下来但界面毫无提示**。
    没人盯着屏幕就永远发现不了。webhook 正好用来填这个坑：
    出问题立刻往钉钉群发一条提醒，顺带按需发运行报告。

三种安全设置全部支持
    自定义关键词  keyword   内容里必须出现该词（缺失时自动补齐，见 _finalize）
    加签          secret    timestamp + HMAC-SHA256，见 _signed_url
    IP 白名单     ——        钉钉侧配置即可，无需代码支持

════════════════════════════════════════════════════════════════════════
 为什么用 urllib 而不是 requests
════════════════════════════════════════════════════════════════════════
requirements.txt 里没有 requests。用一个标准库就够的 HTTP 调用去引入一个
新依赖（还要打进 exe）不划算，所以这里直接用 urllib.request。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("bridge.notify")

WEBHOOK_PREFIX = "https://oapi.dingtalk.com/robot/send?access_token="

# 一个机器人 = 一个 webhook 地址。群里/系统里有多个机器人时，全部填上即可，
# 通知会往每一个都发一条（谁挂了也不影响其它的）。
# 书写格式（配置里一行一个）：
#     https://oapi.dingtalk.com/robot/send?access_token=AAA | SECxxxx | 关键词
# 后面的「加签密钥」「自定义关键词」可以省略 —— 省略时回落到 notify.secret /
# notify.keyword。也认分号分隔，方便在老的 notify.webhook 单值里塞多个。
ROBOT_SEP = "|"
_ROBOT_SPLIT_RE = re.compile(r"[\r\n;]+")

# 默认测试文案：设置中心的「填入测试文案」与命令行 --notify-test 共用，
# 只此一份，免得两边各写一段、改了一处忘了另一处。
DEFAULT_TEST_TEXT = (
    "✅ 喵喵钉钉-微信消息转发桥\n"
    "通知通道测试成功，之后的故障告警会发到这里。\n"
    "如果你在群里看到这条消息，说明配置没问题。"
)

# 钉钉接口返回的错误码，挑几个常见且值得给出人话解释的
ERR_HINTS = {
    300001: "消息发送太快，被钉钉限流了",
    310000: "安全设置不匹配：机器人配了「自定义关键词」但消息里没有这个词，"
            "或加签密钥（secret）填错了",
    130101: "发送频率超限（每个机器人每分钟最多 20 条）",
}


# ══════════════════════════════════════════════════════════════════════
#  告警文案映射（纯函数，便于单测）
# ══════════════════════════════════════════════════════════════════════

# (正则, 去重键, 人类读得懂的标题)
# 这些字符串来自 bridge/sources/dingtalk_ocr.py 里已有的日志 ——
# 刻意不去改采集代码，只在日志上挂一个 Handler，零侵入。
ALERT_RULES: list[tuple[str, str, str]] = [
    (r"未找到钉钉主窗口",            "ocr-no-window",   "采集中断：找不到钉钉窗口"),
    (r"钉钉窗口处于最小化",          "ocr-minimized",   "采集中断：钉钉窗口被最小化"),
    (r"定位不到钉钉消息区",          "ocr-no-area",     "采集异常：找不到钉钉消息区"),
    (r"PrintWindow 失败",            "ocr-print-fail",  "采集异常：钉钉窗口截图失败"),
    (r"会话标题校验：不通过",        "ocr-title-mismatch", "转发已暂停：钉钉当前不是目标群"),
    (r"锚点丢失",                    "ocr-anchor-lost", "采集波动：无法确认新旧消息，本帧已跳过"),
    (r"OCR 轮询异常",                "ocr-exception",   "采集端异常"),
    (r"钉钉凭证|AppKey|client_id",   "stream-cred",     "钉钉凭证相关异常"),
]

# 交给 service 的「连续失败阈值」逻辑处理，日志层不再重复告警
ALERT_EXCLUDE = (r"^投递失败",)


def match_alert(levelno: int, text: str) -> tuple[str, str] | None:
    """把一条日志映射成 ``(去重键, 标题)``；不值得告警则返回 None。

    判定顺序：先排除 → 再精确规则 → 最后兜底（ERROR 级别一律告警）。
    """
    for pat in ALERT_EXCLUDE:
        if re.search(pat, text):
            return None
    for pat, key, title in ALERT_RULES:
        if re.search(pat, text):
            return key, title
    if levelno >= logging.ERROR:
        return "generic-error", "程序异常"
    return None


def in_hhmm_window(now, spec: str) -> bool:
    """判断 ``now``（datetime）是否落在 ``"HH:MM-HH:MM"`` 里，支持跨零点。

    spec 为空或格式不对时返回 True —— 宁可多告警，也不要因为配置写错而
    把告警整个吞掉。
    """
    spec = (spec or "").strip()
    if not spec or "-" not in spec:
        return True
    lo_s, _, hi_s = spec.partition("-")
    try:
        lo_h, lo_m = (int(x) for x in lo_s.strip().split(":"))
        hi_h, hi_m = (int(x) for x in hi_s.strip().split(":"))
        lo = lo_h * 60 + lo_m
        hi = hi_h * 60 + hi_m
    except Exception:
        return True
    cur = now.hour * 60 + now.minute
    if lo == hi:
        return True
    if lo < hi:
        return lo <= cur < hi
    return cur >= lo or cur < hi          # 跨零点，例如 22:00-06:00


# ══════════════════════════════════════════════════════════════════════
#  发送端
# ══════════════════════════════════════════════════════════════════════

def mask_webhook(url: str) -> str:
    """脱敏，用于日志与界面展示（access_token 是凭证，不能明文外露）。"""
    url = (url or "").strip()
    if not url:
        return ""
    m = re.search(r"access_token=([^&\s]+)", url)
    if not m:
        return url if len(url) <= 32 else url[:32] + "…"
    tok = m.group(1)
    shown = tok[:6] + "…" + tok[-4:] if len(tok) > 14 else "…"
    return url.replace(tok, shown)


# ══════════════════════════════════════════════════════════════════════
#  机器人清单（一个地址一个机器人，各自带自己的加签密钥 / 关键词）
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RobotSpec:
    """一个钉钉自定义机器人。

    ``secret`` / ``keyword`` 是**每个机器人自己**的安全设置 —— 多个机器人
    未必用同一套，所以不能只留一个全局值。
    """

    url: str
    secret: str = ""
    keyword: str = ""

    def as_line(self) -> str:
        """还原成配置里的单行写法，用于写回 config.yaml。"""
        return ROBOT_SEP.join((self.url, self.secret, self.keyword)).rstrip(
            ROBOT_SEP + " ")

    def label(self) -> str:
        """界面 / 日志里用的短标签（地址已脱敏）。"""
        tail = []
        if self.secret:
            tail.append("已加签")
        if self.keyword:
            tail.append(f"关键词「{self.keyword}」")
        return mask_webhook(self.url) + (f"（{'，'.join(tail)}）" if tail else "")


def parse_robot_line(line: str, default_secret: str = "",
                     default_keyword: str = "") -> RobotSpec | None:
    """一行 -> 一个 :class:`RobotSpec`；空行 / 注释 / 没有地址时返回 None。

    格式：``地址`` / ``地址 | 加签密钥`` / ``地址 | 加签密钥 | 关键词``。
    自己那段留空时回落到 ``default_secret`` / ``default_keyword``
    （即配置里的全局 ``notify.secret`` / ``notify.keyword``），
    这样「一个 webhook + 一个 secret」的老配置不改也能继续用。
    """
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split(ROBOT_SEP)]
    url = parts[0]
    if not url:
        return None
    secret = parts[1] if len(parts) > 1 and parts[1] else default_secret
    keyword = parts[2] if len(parts) > 2 and parts[2] else default_keyword
    return RobotSpec(url=url, secret=(secret or "").strip(),
                     keyword=(keyword or "").strip())


def _as_line(item) -> str:
    """``webhooks`` 列表项可以是字符串，也可以是 ``{url, secret, keyword}`` 映射。"""
    if isinstance(item, dict):
        url = str(item.get("url") or item.get("webhook") or "").strip()
        if not url:
            return ""
        return ROBOT_SEP.join((url, str(item.get("secret") or ""),
                               str(item.get("keyword") or "")))
    return str(item or "")


def robots_from_lines(lines, default_secret: str = "",
                      default_keyword: str = "") -> list[RobotSpec]:
    """把若干行摊平成机器人清单：按地址去重、保持书写顺序。

    每个列表项自身还可以再用换行 / 分号装多个地址 —— 老配置里
    ``notify.webhook`` 是一个字符串，用分号塞多个也能被正确拆开。
    """
    out: list[RobotSpec] = []
    seen: set[str] = set()
    for raw in lines or []:
        for piece in _ROBOT_SPLIT_RE.split(_as_line(raw)):
            spec = parse_robot_line(piece, default_secret, default_keyword)
            if spec is None or spec.url in seen:
                continue
            seen.add(spec.url)
            out.append(spec)
    return out


def collect_robots(ncfg) -> list[RobotSpec]:
    """从 :class:`NotifyConfig` 取出全部机器人。

    优先用新的 ``webhooks`` 列表；它为空时回落到老的单个 ``webhook``。
    两条路都空 -> 返回空列表（调用方据此判定「没配」）。
    """
    if ncfg is None:
        return []
    d_secret = str(getattr(ncfg, "secret", "") or "").strip()
    d_keyword = str(getattr(ncfg, "keyword", "") or "").strip()
    items = list(getattr(ncfg, "webhooks", None) or [])
    if items:
        return robots_from_lines(items, d_secret, d_keyword)
    return robots_from_lines([getattr(ncfg, "webhook", "") or ""],
                             d_secret, d_keyword)


class DingTalkNotifier:
    """钉钉机器人发送端。

    支持**同时配置多个机器人**：一条通知会往每一个都发一份。
    线程安全，失败不抛异常（只返回 False）。
    """

    def __init__(self, webhook: str = "", secret: str = "", keyword: str = "",
                 at_mobiles: list[str] | None = None, at_all: bool = False,
                 timeout: float = 6.0, cooldown: float = 300.0,
                 retries: int = 2, opener=None,
                 robots: list[RobotSpec] | None = None):
        # robots 是「已解析好」的清单（from_config 用）；
        # 否则按老的单个 webhook 参数解析 —— 单地址调用方无需改动。
        if robots is not None:
            self.robots = list(robots)
        else:
            src = [webhook] if (webhook or "").strip() else []
            self.robots = robots_from_lines(src, secret, keyword)
        # 兼容旧属性：老代码 / 老测试还在读 notifier.webhook / .secret / .keyword，
        # 语义是「第一个机器人」。
        first = self.robots[0] if self.robots else None
        self.webhook = first.url if first else ""
        self.secret = first.secret if first else (secret or "").strip()
        self.keyword = first.keyword if first else (keyword or "").strip()
        self.at_mobiles = [str(m).strip() for m in (at_mobiles or []) if str(m).strip()]
        self.at_all = bool(at_all)
        self.timeout = float(timeout)
        self.cooldown = float(cooldown)
        self.retries = max(0, int(retries))
        # 便于单测注入假 opener
        self._opener = opener or urllib.request.urlopen
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}
        self.last_error = ""

    # -------------------------------------------------- 构造

    @classmethod
    def from_config(cls, ncfg) -> "DingTalkNotifier | None":
        """按配置构造；未启用或一个地址都没填时返回 None（调用方据此跳过）。"""
        if ncfg is None or not getattr(ncfg, "enabled", False):
            return None
        robots = collect_robots(ncfg)
        if not robots:
            log.warning("已开启钉钉通知，但没有填 webhook，通知功能将不生效")
            return None
        return cls(
            robots=robots,
            at_mobiles=list(getattr(ncfg, "at_mobiles", []) or []),
            at_all=bool(getattr(ncfg, "at_all", False)),
            cooldown=float(getattr(ncfg, "cooldown", 300.0) or 300.0),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.robots)

    def describe(self) -> str:
        bits = ["钉钉通知已启用"]
        if len(self.robots) > 1:
            bits.append(f"{len(self.robots)} 个机器人")
        if self.robots and all(r.secret for r in self.robots):
            bits.append("已加签")
        kws = {r.keyword for r in self.robots if r.keyword}
        if len(kws) == 1:
            bits.append(f"关键词「{kws.pop()}」")
        if self.at_all:
            bits.append("@所有人")
        elif self.at_mobiles:
            bits.append(f"@{'、'.join(self.at_mobiles)}")
        return "，".join(bits)

    # -------------------------------------------------- 请求构造

    def _signed_url(self, now_ms: int | None = None,
                    robot: RobotSpec | None = None) -> str:
        """加签：timestamp + "\\n" + secret 做 HMAC-SHA256，再 urlencode。

        ``robot`` 省略时用第一个机器人（保持老调用方与单测的用法）。
        """
        rb = robot or (self.robots[0] if self.robots else None)
        url = rb.url if rb else self.webhook
        secret = rb.secret if rb else self.secret
        if not secret:
            return url
        ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}"
        digest = hmac.new(secret.encode("utf-8"),
                          string_to_sign.encode("utf-8"),
                          hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}timestamp={ts}&sign={sign}"

    def _finalize(self, content: str, robot: RobotSpec | None = None) -> str:
        """关键词安全设置下，内容里必须出现该词 —— 缺了就补一行，别让消息白丢。

        关键词是**每个机器人各自**配的，所以要按机器人补齐。
        """
        kw = (robot.keyword if robot else self.keyword)
        if kw and kw not in content:
            return f"{content}\n\n[{kw}]"
        return content

    def _at_payload(self) -> dict:
        return {"atMobiles": list(self.at_mobiles), "isAtAll": bool(self.at_all)}

    def _post_one(self, robot: RobotSpec, factory) -> tuple[bool, str]:
        """发给**一个**机器人。``factory(robot) -> payload`` 因为关键词要逐个补。

        返回 ``(成功, 说明)``，任何异常都吞掉只记日志。
        """
        payload = factory(robot)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = self._signed_url(robot=robot)
        who = mask_webhook(robot.url)
        last = "未知错误"
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST")
            try:
                with self._opener(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                try:
                    data = json.loads(raw)
                except Exception:
                    last = f"返回不是 JSON：{raw[:120]}"
                else:
                    code = int(data.get("errcode", -1))
                    if code == 0:
                        return True, "ok"
                    hint = ERR_HINTS.get(code, "")
                    last = f"errcode={code} {data.get('errmsg', '')}" + (
                        f"（{hint}）" if hint else "")
                    # 业务错误重试没用，直接返回
                    log.warning("钉钉通知发送失败（%s）：%s", who, last)
                    return False, last
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
        log.warning("钉钉通知发送失败（%s，已重试 %d 次）：%s", who, self.retries, last)
        return False, last

    def _broadcast(self, factory) -> tuple[bool, str]:
        """往**每个**机器人都发一份。

        判定口径：只要有任意一个发成功就算成功（返回 True）——
        否则某个机器人配错/被移出群，会让 `/钉钉通知/` 的冷却机制一直不生效，
        健康的那几个会被同一类告警反复刷。失败的那些逐条记 WARNING。
        """
        if not self.robots:
            return False, "没有配置任何机器人 webhook 地址"
        oks, fails = 0, []
        for rb in self.robots:
            ok, why = self._post_one(rb, factory)
            if ok:
                oks += 1
            else:
                fails.append(f"{mask_webhook(rb.url)} → {why}")
        if not fails:
            self.last_error = ""
            return True, "ok" if oks == 1 else f"{oks} 个机器人全部发送成功"
        self.last_error = "；".join(fails)
        if oks:
            log.warning("钉钉通知部分失败：%s", self.last_error)
            return True, f"{oks}/{len(self.robots)} 个机器人成功（{self.last_error}）"
        log.warning("钉钉通知全部失败：%s", self.last_error)
        return False, self.last_error

    # -------------------------------------------------- 对外接口

    def send_text(self, content: str, at: bool = False) -> tuple[bool, str]:
        def build(rb: RobotSpec) -> dict:
            payload = {"msgtype": "text",
                       "text": {"content": self._finalize(content, rb)}}
            if at:
                payload["at"] = self._at_payload()
            return payload

        return self._broadcast(build)

    def send_markdown(self, title: str, text: str, at: bool = False) -> tuple[bool, str]:
        # markdown 的标题也在「关键词」的检查范围内，所以两边都要过一遍
        def build(rb: RobotSpec) -> dict:
            payload = {"msgtype": "markdown",
                       "markdown": {"title": self._finalize(title, rb),
                                    "text": self._finalize(text, rb)}}
            if at:
                payload["at"] = self._at_payload()
            return payload

        return self._broadcast(build)

    def alert(self, key: str, content: str, at: bool = True) -> bool:
        """带冷却去重的告警。

        同一个 key 在 cooldown 秒内只发一次 —— 采集窗口被切走时，
        OCR 每个轮询周期都会打一条同样的 WARNING，不去重会瞬间刷屏。
        """
        now = time.time()
        with self._lock:
            last = self._last_sent.get(key, 0.0)
            if now - last < self.cooldown:
                log.debug("告警「%s」处于冷却期（%.0fs），跳过", key, self.cooldown)
                return False
            self._last_sent[key] = now
        ok, detail = self.send_text(content, at=at)
        if not ok:
            with self._lock:              # 发失败就释放冷却，下一轮还能再试
                self._last_sent.pop(key, None)
        return ok

    def reset_cooldown(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._last_sent.clear()
            else:
                self._last_sent.pop(key, None)

    def send_custom(self, content: str, at: bool = False) -> tuple[bool, str]:
        """发送用户自己写的内容。

        与 ``send_text`` 的唯一区别是先做一次人工检查：全空白（比如用户
        只敲了几个空格回车）直接挡下来并给出人话原因 —— 否则钉钉会返回
        一个语焉不详的错误码，让人以为 webhook 配错了。
        """
        content = (content or "").strip()
        if not content:
            return False, "消息内容是空的，请先输入要发送的文字"
        return self.send_text(content, at=at)

    def test(self) -> tuple[bool, str]:
        """发一条默认测试消息，用于确认 webhook / 关键词 / 加签是否配对。"""
        return self.send_custom(DEFAULT_TEST_TEXT, at=False)


# ══════════════════════════════════════════════════════════════════════
#  日志 -> 告警 的桥接
# ══════════════════════════════════════════════════════════════════════

class AlertHandler(logging.Handler):
    """把 bridge.* 的异常日志转成钉钉告警。

    刻意做成 logging.Handler 而不是去改采集端代码：
      · 采集端本来就会在这些场景打 WARNING，无需新增埋点
      · 采集、投递、路由任何一处将来新增的 ERROR 都自动被覆盖
      · 不会因为通知功能出问题而影响主流程
    """

    def __init__(self, notifier: DingTalkNotifier, level: int = logging.INFO):
        super().__init__(level=level)
        self.notifier = notifier

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not record.name.startswith("bridge."):
                return
            if record.name.startswith("bridge.notify"):   # 防止自己告警自己，形成回环
                return
            try:
                text = record.getMessage()
            except Exception:
                return
            hit = match_alert(record.levelno, text)
            if not hit:
                return
            key, title = hit
            self.notifier.alert(
                key,
                f"🔔 {title}\n"
                f"{text}\n\n"
                f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"来源：{record.name}",
            )
        except Exception:
            # 日志处理器绝不能自己抛异常，否则会把主流程带崩
            pass


def install_alert_handler(notifier: DingTalkNotifier | None,
                          level: int = logging.INFO) -> AlertHandler | None:
    """挂上告警 Handler，返回句柄便于卸载。未启用通知时返回 None。"""
    if notifier is None or not notifier.enabled:
        return None
    handler = AlertHandler(notifier, level=level)
    logging.getLogger("bridge").addHandler(handler)
    return handler
