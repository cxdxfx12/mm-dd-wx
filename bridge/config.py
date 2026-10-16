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
    ui_poll_interval: float = 5.0           # ui / ocr 模式的轮询间隔（秒）
    title_check: bool = True                # OCR 模式：每帧校验窗口标题，防转发错群
    title_ratio: float = 0.55               # 标题模糊匹配阈值（OCR 对标题有误识，需容错）
    time_filter: str = "today"              # today = 丢弃跨天旧消息；any = 不限；window = 指定时段
    time_window: str = "08:00-18:00"        # time_filter=window 时的时段，支持跨零点（22:00-06:00）
    anchor_guard: bool = True               # OCR 模式：用锚点防止窗口滚动导致历史消息被误转发


@dataclass
class WeChatConfig:
    dry_run: bool = True
    interval: float = 3.0                   # 相邻两条消息的最小发送间隔（秒），防风控
    targets: list[str] = field(default_factory=list)
    wxauto_path: str = "../wxauto-main"
    driver: str = "auto"                    # auto | wxauto(微信3.9) | ui4(微信4.x)
    # 微信「显示/隐藏主窗口」全局热键。微信用 Esc 或点最小化会把主窗口收进
    # 托盘（UIA 里整个消失），此时桥用这个热键把窗口唤回来。
    # 若你在微信「设置 → 快捷键」里改过，这里要跟着改。
    show_hotkey: str = "{Ctrl}{Alt}w"


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
