"""配置加载：YAML -> dataclass。

敏感字段（AppKey / AppSecret）优先从环境变量读取，避免写进配置文件。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class StorageConfig:
    db_path: str = "data/bridge.db"
    # 消息数据库保留天数：设置中心「其他 → 消息数据库」的默认值，也是清理
    # 功能的建议上限。**不会自动删**（删数据必须在用户点确认时发生），
    # 只是把用户上次选的值记住。0 = 永久保留（只清孤儿投递记录）。
    keep_days: int = 90


@dataclass
class DingTalkConfig:
    mode: str = "stream"                    # stream | ui | ocr | android
    client_id: str = ""
    client_secret: str = ""
    robot_code: str = ""
    companies: list[str] = field(default_factory=list)
    chat_names: list[str] = field(default_factory=list)
    gateway_host: str = "0.0.0.0"           # android 模式：监听地址
    gateway_port: int = 8765                # android 模式：监听端口
    gateway_token: str = ""                 # android 模式：必填（见 android_gateway 模块说明）
    # android 模式：是否把手机报来的节点树解析成消息并转发。
    # 默认 False —— 此时网关只负责「收」，程序不转发任何东西。开启前务必先用
    # tools/android_capture.py 看过一帧真实画面的解析结果（见 android/README.md）。
    android_parse: bool = False
    # android 模式：手机多久没上报就判为「失联」并告警（秒，0 = 不检测）。
    # 判据是**心跳**（手机端每 60 秒 POST /ping），不是「多久没收到画面」——
    # 群里半天没人说话时画面本来就不变、手机也不上报，按画面判会天天误报。
    android_offline_after: float = 300.0
    # android 模式：手机自动巡检间隔（分钟，0 = 关闭）。手机端会把钉钉拉回
    # 目标群（dingtalk.chat_names 的第一个），好让采集链路自己恢复。
    # 用户 2026-10-20 定为「定时巡检，可选 5 / 10 / 15 分钟」，默认关；
    # 2026-10-27 用户定 15 分钟（「改成15分钟巡检」）。
    android_watch_min: int = 0
    # android 模式：附件转发触发字眼（行首前缀，纯文本不是正则）。消息里
    # 出现以这些字眼开头的行（允许带「明细」前缀，如「明细附件：xxx」），
    # 就让手机点开对应卡片、下载文件并转发。2026-10-27 用户要求可配置 ——
    # 默认「附件：/附件:」（半角全角冒号各一份）。
    attach_keys: list[str] = field(default_factory=lambda: ["附件：", "附件:"])
    # 附件转发的总开关（设置中心 → 采集与投递 → 附件转发）。关掉就不下发
    # 抓取指令，手机端自然什么都不做 —— 手机端不需要知道这个键。
    attach_enabled: bool = True
    # 万能字眼（2026-11-01 用户：「有附件、excel、明细等相关字眼都下载」）。
    # 短行（≤16 字）里同时出现「附件/明细/表格/Excel…」和「下载/查看/获取…」
    # 就当作附件按钮去抓。默认开，漏抓的代价比偶尔白跑一次大得多。
    attach_auto: bool = True
    ui_poll_interval: float = 5.0           # ui / ocr 模式的轮询间隔（秒）
    title_check: bool = True                # OCR 模式：每帧校验窗口标题，防转发错群
    title_ratio: float = 0.55               # 标题模糊匹配阈值（OCR 对标题有误识，需容错）
    time_filter: str = "today"              # today = 丢弃跨天旧消息；any = 不限；window = 指定时段
    time_window: str = "08:00-18:00"        # time_filter=window 时的时段，支持跨零点（22:00-06:00）
    anchor_guard: bool = True               # OCR 模式：用锚点防止窗口滚动导致历史消息被误转发


@dataclass
class WeChatConfig:
    dry_run: bool = True
    # 相邻两条消息的间隔（秒），防风控。
    # interval 是**下限**；另外在 [interval*0.8, interval*1.6] 之间随机抖动，
    # 避免出现「固定 3 秒一条」这种一眼是机器人的节奏，见 service._throttle()。
    interval: float = 5.0
    # 随机长停顿：每隔若干条消息插入一次更长的等待，模拟真人离开/思考。
    interval_jitter_long: bool = True       # 是否启用长停顿
    interval_long_every: int = 3            # 每发几条插入一次长停顿
    interval_long_range: list = field(default_factory=lambda: [8.0, 20.0])
    targets: list[str] = field(default_factory=list)
    wxauto_path: str = "../wxauto-main"
    # auto | wxauto(微信3.9) | ui4(微信4.x UIA) | ocr(微信4.x 截图+OCR)
    # ⚠ 2026-10-18 起微信 4.x 不再暴露 mmui:: 控件树，auto 探测到 4.x 会自动走 ocr。
    driver: str = "auto"
    # 微信「显示/隐藏主窗口」全局热键。微信用 Esc 或点最小化会把主窗口收进
    # 托盘（UIA 里整个消失），此时桥用这个热键把窗口唤回来。
    # 若你在微信「设置 → 快捷键」里改过，这里要跟着改。
    show_hotkey: str = "{Ctrl}{Alt}w"
    # ---- OCR 投递端（driver: ocr）专属 ----
    ocr_verify: bool = True                 # 发送后回读截图校验（检测发出气泡）
    ocr_keep_clipboard: bool = True         # 用完恢复用户原有剪贴板内容
    ocr_match_threshold: float = 0.62       # 会话名匹配阈值，低于此值拒发
    # ---- 表格转图片 ----
    # 钉钉卡片里的表格在微信没有原生类型；开着时把「列名: 值 ｜ 列名: 值」
    # 形态的表格行画成 PNG、在文字消息之后补发一张图。文字照常发，图片
    # 失败不影响文字（见 service.handle）。关掉就完全回到纯文字。
    table_image: bool = True
    # 「积压超龄不转发」的阈值（分钟）。源消息时间距现在超过它 → 只入库留档、
    # 不投微信（详见 service.MAX_BACKLOG_MIN 与 BridgeService._backlog_minutes）。
    # 0 = 关闭这条闸门（不建议：手机断线恢复时会把一屏历史一次性刷进业务群）。
    max_backlog_min: float = 30.0
    # 附件（原文件）专用的微信目标 + 它的**有效截止时间**。平时两个都留空 ——
    # 留空就是「附件跟正文一样发业务群」，这是日常行为，别动。
    #
    # 存在的唯一理由：用户 2026-11-01 20:26「我是说今天要补发的发到文件助手，
    # 不是以后的」—— 补回来的历史附件不想再刷业务群，但以后日常要照旧。
    # 所以配「目标 + 截止时间」：截止前改道，过了时间**自动失效**。
    # 不设成永久开关，是因为忘记关的后果很难查：第二天附件全进文件传输助手，
    # 业务群静默收不到东西，谁也不会立刻发现。
    attach_target: str = ""
    attach_target_until: str = ""


@dataclass
class NotifyConfig:
    """钉钉机器人通知（告警 / 运行报告）。

    ⚠ 这里的 webhook 是**自定义机器人**，只能往群里发消息、收不到群消息，
    不参与「钉钉 → 微信」的取数，详见 bridge/notify.py 顶部说明。
    """
    enabled: bool = False
    webhook: str = ""                       # 老格式：单个地址（也认分号分隔的多个）
    # 新格式：一个机器人一项。每项是 "地址 | 加签密钥 | 关键词"（后两段可省），
    # 不方便写一行时也可以写成 {url:…, secret:…, keyword:…} 映射。
    # 这一项非空时以它为准，webhook 只作为向后兼容的回落。
    webhooks: list[str] = field(default_factory=list)
    secret: str = ""                        # 机器人开了「加签」才需要（多机器人时作默认值）
    keyword: str = ""                       # 机器人开了「自定义关键词」才需要（同上作默认值）
    at_mobiles: list[str] = field(default_factory=list)
    at_all: bool = False
    cooldown: float = 300.0                 # 同一类告警的最小间隔（秒），防刷屏
    log_alerts: bool = True                 # 采集端异常日志 → 告警
    notify_start: bool = True               # 开始 / 停止运行时各发一条
    notify_fail: bool = True                # 投递失败告警
    fail_threshold: int = 3                 # 连续失败达到几次才告警
    notify_idle: bool = True                # 采集停滞告警
    idle_minutes: int = 120                 # 多久没有新消息算停滞
    idle_window: str = "08:00-21:00"        # 只在此时段内判定停滞（避免夜间误报）
    report_hourly: bool = False             # 每小时运行报告
    report_daily_at: str = ""               # 每日报告时间，空 = 不发，例如 "18:00"


@dataclass
class ReconcileConfig:
    """漏转对账：拿钉钉 PC 本地库当基准，查桥有没有漏消息。

    只有在**本机装了钉钉 PC 客户端、且登录的是采集用的那个账号**时才有意义
    —— 手机端采集的账号和 PC 登录账号不是同一个时，库里一条都查不到，
    对账器会自己报「找不到这个群」并停用，不会假装「没有漏转」。

    依赖 pycryptodome。没装时整块功能自动失效，主流程完全不受影响。
    """
    enabled: bool = True
    interval_min: float = 10.0        # 每几轮核对一次
    lookback_min: float = 40.0        # 单轮常规回看窗口
    tolerance_min: float = 30.0       # 时间兜底容差（OCR 时间条只到分钟）
    # 停机/挂起之后一次补齐的最大回看深度；超过这个时长的历史不再追。
    # 太大会让重启后一次解密几万条卡片、告警刷屏。
    max_gap_hours: float = 12.0
    alert: bool = True                # 除日志外还发钉钉通知
    dingtalk_path: str = ""           # 留空自动找最近活跃的 *_v3 目录
    dingtalk_chat: str = ""           # 钉钉侧群名关键词，留空用 dingtalk.chat_names[0]
    bridge_chat: str = ""             # 桥侧群名关键词，留空同上


@dataclass
class RouteConfig:
    name: str = "default"
    companies: list[str] = field(default_factory=list)
    chat_names: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    msg_types: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    template: str = "{content}"


@dataclass
class Config:
    storage: StorageConfig = field(default_factory=StorageConfig)
    dingtalk: DingTalkConfig = field(default_factory=DingTalkConfig)
    wechat: WeChatConfig = field(default_factory=WeChatConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    reconcile: ReconcileConfig = field(default_factory=ReconcileConfig)
    routes: list[RouteConfig] = field(default_factory=list)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def resolve(self, path: str) -> Path:
        """把相对路径解析成相对配置文件所在目录的绝对路径。"""
        p = Path(path)
        return p if p.is_absolute() else (self._base_dir / p).resolve()


def _section(cls, raw: Any) -> Any:
    if not isinstance(raw, dict):
        return cls()
    known = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{k: v for k, v in raw.items() if k in known})


def load_config(path: str | Path = "config.yaml") -> Config:
    cfg_path = Path(path).resolve()
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件根节点必须是映射（key: value）")

    cfg = Config(
        storage=_section(StorageConfig, raw.get("storage")),
        dingtalk=_section(DingTalkConfig, raw.get("dingtalk")),
        wechat=_section(WeChatConfig, raw.get("wechat")),
        notify=_section(NotifyConfig, raw.get("notify")),
        reconcile=_section(ReconcileConfig, raw.get("reconcile")),
        routes=[_section(RouteConfig, r) for r in (raw.get("routes") or [])],
    )

    # 环境变量覆盖敏感字段，优先级高于配置文件
    cfg.dingtalk.client_id = os.getenv("DINGTALK_CLIENT_ID") or cfg.dingtalk.client_id
    cfg.dingtalk.client_secret = os.getenv("DINGTALK_CLIENT_SECRET") or cfg.dingtalk.client_secret
    cfg.dingtalk.robot_code = os.getenv("DINGTALK_ROBOT_CODE") or cfg.dingtalk.robot_code
    # webhook 里带 access_token，等同于凭证，同样支持用环境变量下发。
    # 多个机器人用 DINGTALK_ROBOT_WEBHOOKS（分号隔开，每项仍可跟 |密钥|关键词）。
    env_many = os.getenv("DINGTALK_ROBOT_WEBHOOKS")
    if env_many:
        cfg.notify.webhooks = [x for x in env_many.split(";") if x.strip()]
        cfg.notify.webhook = ""
    else:
        cfg.notify.webhook = os.getenv("DINGTALK_ROBOT_WEBHOOK") or cfg.notify.webhook
    cfg.notify.secret = os.getenv("DINGTALK_ROBOT_SECRET") or cfg.notify.secret

    if not cfg.routes:
        raise ValueError("config.yaml 中 routes 至少需要配置一条路由规则")

    cfg._base_dir = cfg_path.parent
    return cfg
