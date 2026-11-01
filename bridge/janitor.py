"""本地垃圾清理：把 ``data/`` 下「本程序自己产生、且可再生」的文件按年龄删掉。

为什么单独一个模块
    用户 2026-11-01 提的：「这个版本最好加上本地清理的功能，项目产生的垃圾数据」。
    查下来真正占地方的是这些东西（安装目录实测）：

        data/table-png/    60 张 PNG   35 MB   ← 发出去的表格截图
        data/logs/         12 个日志   1.1 MB
        data/            调试转储若干   ~0.3 MB（screen-*.xml / _live*.xml / _gui_ver*.png）
        data/backup/      配置备份若干

    它们都不是业务数据，删了不影响任何一条消息的去向。但**不能交给用户自己
    按文件夹删** —— attachments/（收下来的附件）和 outbox/（还没发出去的）
    就在隔壁，手一抖就把积压的消息删了。所以做成一键、有白名单、有预览。

三条铁律
    1. **只删白名单**：只有 {@link RULES} 里写明的目录 + 文件名模式才会被选中；
    2. **只删够老的**：默认 7 天（配置备份 30 天），正在用的日志/当天的表格图
       一律不动 —— 出问题回查最近几天是刚需；
    3. **绝不碰业务数据**：``bridge.db``（含 -wal/-shm）、``config.yaml``、
       ``attachments/``、``outbox/`` 写死在 {@link PROTECTED_NAMES} /
       {@link PROTECTED_DIRS} 里，任何规则都绕不过去，测试逐条钉住。

为什么用 mtime 而不是文件名里的日期
    日志名里有日期，但表格图是 ``table-<hash>.png``、调试转储是
    ``screen-20261017_121909.xml`` 这种混合命名。用 mtime 一套逻辑通吃，
    也不会因为改名而误判。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# 默认保留期：正常情况下用户只需要回查最近几天的东西。
DEFAULT_KEEP_DAYS = 7
# 配置备份动得少、体积小，但恢复时很有用，留久一点。
BACKUP_KEEP_DAYS = 30

SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class Rule:
    """一条清理规则：哪个子目录、匹配哪些文件名、留多少天。"""

    key: str
    label: str
    subdir: str          # 相对 data/；空串表示 data/ 根目录（只看这一层，不递归）
    patterns: tuple[str, ...]
    keep_days: int


# 顺序即界面上的展示顺序，按「占地方」从大到小排。
RULES: tuple[Rule, ...] = (
    Rule(
        key="table_png",
        label="表格图片",
        subdir="table-png",
        patterns=("*.png",),
        keep_days=DEFAULT_KEEP_DAYS,
    ),
    Rule(
        key="logs",
        label="运行日志",
        subdir="logs",
        patterns=("*.log", "*.log.*", "*.txt"),
        keep_days=DEFAULT_KEEP_DAYS,
    ),
    Rule(
        key="debug",
        label="调试转储",
        subdir="",
        # 开发期抓屏 / 探针留下的东西，正常运行时不会再有新文件
        patterns=(
            "screen-*.xml",
            "_probe*.xml",
            "_live*.xml",
            "_worker_probe.log",
            "_gui_ver*.png",
            "desktop.png",
        ),
        keep_days=DEFAULT_KEEP_DAYS,
    ),
    Rule(
        key="backup",
        label="配置备份",
        subdir="backup",
        patterns=("*.yaml", "*.yml", "*.bak", "*.bak_*"),
        keep_days=BACKUP_KEEP_DAYS,
    ),
    Rule(
        key="root_bak",
        label="配置备份（根目录）",
        subdir="",
        patterns=("config.yaml.bak*",),
        keep_days=BACKUP_KEEP_DAYS,
    ),
)

# ---------------------------------------------------------------- 保护名单
# 任何规则命中这些名字都要放行（宁可少删，不可错删）。
PROTECTED_NAMES: frozenset[str] = frozenset({
    "bridge.db",
    "bridge.db-wal",
    "bridge.db-shm",
    "config.yaml",
    "config.example.yaml",
})

# 这些子目录整体不参与清理：一个是收下来的附件，一个是还没发出去的。
PROTECTED_DIRS: frozenset[str] = frozenset({"attachments", "outbox"})


@dataclass(frozen=True)
class Item:
    """一个待删文件。"""

    group: str
    label: str
    path: Path
    size: int
    mtime: float


@dataclass
class Plan:
    """扫描结果。``items`` 为空就表示没什么可清的。"""

    root: Path
    items: list[Item] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(it.size for it in self.items)

    @property
    def total_count(self) -> int:
        return len(self.items)

    def groups(self) -> list[tuple[str, str, int, int]]:
        """按规则顺序汇总：(key, label, 文件数, 字节数)，只含非空项。"""
        order = {r.key: i for i, r in enumerate(RULES)}
        label_of = {r.key: r.label for r in RULES}
        acc: dict[str, tuple[int, int]] = {}
        for it in self.items:
            n, sz = acc.get(it.group, (0, 0))
            acc[it.group] = (n + 1, sz + it.size)
        out = [(k, label_of.get(k, k), n, sz) for k, (n, sz) in acc.items()]
        out.sort(key=lambda x: order.get(x[0], 99))
        return out


@dataclass
class Result:
    """执行结果。"""

    deleted: int = 0
    freed: int = 0
    failed: list[tuple[Path, str]] = field(default_factory=list)


def human_size(n: int) -> str:
    """1234567 → '1.2 MB'。界面与日志共用一种说法，免得两处数字对不上。"""
    step = 1024.0
    v = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB"):
        if v < step or unit == "GB":
            if unit == "B":
                return f"{int(v)} B"
            return f"{v:.1f} {unit}"
        v /= step
    return f"{v:.1f} GB"


def _inside(path: Path, root: Path) -> bool:
    """path 解析（含符号链接）之后仍必须落在 root 里面。

    多一道保险：Windows 上 junctions / 符号链接能让一个看着像 ``data\\logs\\x.log``
    的路径实际指向别处，删之前先确认它真的在数据目录里。
    """
    try:
        p = path.resolve(strict=False)
        r = root.resolve(strict=False)
    except OSError:
        return False
    try:
        p.relative_to(r)
    except ValueError:
        return False
    return True


def scan(
    root: str | os.PathLike,
    *,
    keep_days: int | None = None,
    now: float | None = None,
) -> Plan:
    """扫出可清理的文件，**不删任何东西**。

    :param root: 数据目录（一般是 ``paths.data_dir()``）
    :param keep_days: 统一覆盖保留天数。不传就各规则用自己的（表格图 7 天、
        配置备份 30 天）。一般不用传。
    :param now: 当前时间戳，测试用。
    """
    root_p = Path(root)
    plan = Plan(root=root_p)
    if not root_p.is_dir():
        return plan

    now_ts = time.time() if now is None else float(now)
    seen: set[Path] = set()

    for rule in RULES:
        base = root_p / rule.subdir if rule.subdir else root_p
        if not base.is_dir():
            continue
        # 调用方给了统一期限就听调用方的；没给就按规则自己的。
        keep = rule.keep_days if keep_days is None else int(keep_days)
        cutoff = now_ts - max(0, keep) * SECONDS_PER_DAY
        for pattern in rule.patterns:
            for p in sorted(base.glob(pattern)):
                if p in seen or not p.is_file():
                    continue
                if p.name in PROTECTED_NAMES:
                    continue
                # 根目录规则只看这一层，避免 glob 意外吃到子目录
                if not rule.subdir and p.parent != root_p:
                    continue
                if p.parent.name in PROTECTED_DIRS:
                    continue
                if not _inside(p, root_p):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime > cutoff:      # 新的（含未来时间）一律不动
                    continue
                seen.add(p)
                plan.items.append(
                    Item(
                        group=rule.key,
                        label=rule.label,
                        path=p,
                        size=int(st.st_size),
                        mtime=float(st.st_mtime),
                    )
                )
    return plan


def execute(plan: Plan) -> Result:
    """按计划删除。单个文件删不掉（被占用 / 没权限）只记下来，不中断。"""
    res = Result()
    for it in plan.items:
        try:
            size = it.path.stat().st_size
            it.path.unlink()
        except FileNotFoundError:
            continue                      # 已经被别人删了，不算失败
        except OSError as exc:
            # 最常见：worker 正在写这个日志文件。跳过即可，下次清理会再来一遍。
            res.failed.append((it.path, str(exc)))
            continue
        res.deleted += 1
        res.freed += int(size)
    return res


def summary_lines(plan: Plan) -> list[str]:
    """给界面用的多行摘要（一行一个分组 + 一行合计）。"""
    if plan.total_count == 0:
        return ["没有需要清理的文件。"]
    lines = [
        f"· {label}：{n} 个 · {human_size(sz)}"
        for _key, label, n, sz in plan.groups()
    ]
    lines.append(f"合计 {plan.total_count} 个文件，{human_size(plan.total_size)}")
    return lines
