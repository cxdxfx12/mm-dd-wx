"""消息数据库瘦身：按保留期删旧消息与投递记录，再 VACUUM 回收磁盘。

为什么需要
    用户 2026-11-05 问「以后 sqlite 数据库会不会很臃肿」。会，而且是**只增
    不减**：每条消息进一行（正文 + 表格原文，长卡片两三 KB），每条消息再
    记 1~3 条投递记录（正文 / 附件 / 表格图各一条）。一天几十条、一年一万
    多条，几年后库就上几百兆 —— 而且 SQLite 的 DELETE **不会**把文件缩小。

三个必须做对的地方
    1. **删了不 VACUUM 等于没删**：页会进 freelist，文件大小原地不动。
       所以删完一定要 VACUUM，且 VACUUM 前先 checkpoint（WAL 模式下不
       checkpoint 直接 VACUUM 会报 busy 或留下巨大的 -wal）。
    2. **保留期不能太短**：去重靠 ``messages.uid``，删掉后手机端再补传同一条
       会重新入库。虽然超龄闸门会拦（默认 30 分钟），但补发/对账都还要看
       历史，所以下限写死 7 天、默认 90 天，传 0 直接拒绝。
    3. **服务在跑时可能锁库**：VACUUM 需要排他。抢不到就如实说「没释放」，
       让用户停掉转发再点一次 —— 绝不假装成功。

删哪些
    * ``messages``：入库时间（缺就退回 msg_time）早于保留期的行；
    * ``deliveries``：同样早于保留期，外加**孤儿**（uid 已经不在 messages 里
      的残留记录，消息被删过就永远挂着）；
    * 附件文件**不动**：``data/attachments`` 是业务凭证，删不删由人决定
      （清理界面里会明说）。
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# 保留期下限：低于这个数就是在拿「去重」和「补发」冒险，直接拒绝。
MIN_KEEP_DAYS = 7
DEFAULT_KEEP_DAYS = 90
# 保留期档位（设置中心的下拉框用这套；不在档位里的值原样显示）
KEEP_CHOICES = (30, 90, 180, 365, 0)
KEEP_LABELS = {
    30: "保留 30 天",
    90: "保留 90 天（推荐）",
    180: "保留 180 天",
    365: "保留 1 年",
    0: "永久保留（只清孤儿记录）",
}
# 服务在跑时 VACUUM 会撞锁，最多等这么久就放弃并如实报告。
BUSY_TIMEOUT_MS = 8000


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


@dataclass
class Plan:
    """一次清理的「体检报告」：删什么、能省多少，都算好给用户看。"""

    db_path: Path
    keep_days: int
    file_bytes: int = 0                 # 主库 + -wal + -shm 实际占盘
    db_bytes: int = 0                   # 主库文件本身
    message_rows: int = 0
    message_old: int = 0
    delivery_rows: int = 0
    delivery_old: int = 0
    delivery_orphan: int = 0
    page_size: int = 0
    page_count: int = 0
    freelist_count: int = 0
    cutoff: str = ""
    error: str = ""

    @property
    def delivery_doomed(self) -> int:
        return self.delivery_old + self.delivery_orphan

    @property
    def nothing_to_do(self) -> bool:
        return self.message_old == 0 and self.delivery_doomed == 0

    @property
    def reclaimable(self) -> int:
        """预计能回收的字节（粗估，按删除行数占比算主库大小）。"""
        if self.db_bytes <= 0:
            return 0
        rows = self.message_rows + self.delivery_rows
        doomed = self.message_old + self.delivery_doomed
        if not rows or not doomed:
            return 0
        return int(self.db_bytes * doomed / rows)


@dataclass
class Result:
    deleted_messages: int = 0
    deleted_deliveries: int = 0
    before_bytes: int = 0
    after_bytes: int = 0
    vacuumed: bool = False
    note: str = ""
    error: str = ""

    @property
    def freed(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def _file_bytes(p: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += Path(f"{p}{suffix}").stat().st_size
        except OSError:
            pass
    return total


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def _has_table(conn, name: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone()
    return bool(row)


def scan(db_path: str | Path, keep_days: int = DEFAULT_KEEP_DAYS,
         now: datetime | None = None) -> Plan:
    """只读体检：算清会删多少行、能省多少空间，不动一行数据。

    ``keep_days=0`` 表示「不按时间删」，只清孤儿投递记录（安全选项）。
    """
    db_path = Path(db_path)
    keep = int(keep_days)
    if keep < 0:
        raise ValueError("保留天数不能是负数")
    if 0 < keep < MIN_KEEP_DAYS:
        raise ValueError(f"保留天数不能少于 {MIN_KEEP_DAYS} 天"
                         "（再短会影响去重和补发）")
    now = now or datetime.now()
    cutoff = (now - timedelta(days=keep)).strftime("%Y-%m-%d %H:%M:%S") if keep else ""

    plan = Plan(db_path=db_path, keep_days=keep, cutoff=cutoff)
    if not db_path.is_file():
        plan.error = f"数据库不存在：{db_path}"
        return plan
    plan.file_bytes = _file_bytes(db_path)

    try:
        conn = _connect(db_path)
    except sqlite3.DatabaseError as exc:
        plan.error = f"打不开数据库：{exc}"
        return plan
    try:
        plan.db_bytes = db_path.stat().st_size
        try:
            plan.page_size = conn.execute("PRAGMA page_size").fetchone()[0] or 0
            plan.page_count = conn.execute("PRAGMA page_count").fetchone()[0] or 0
            plan.freelist_count = conn.execute("PRAGMA freelist_count").fetchone()[0] or 0
        except sqlite3.DatabaseError:
            pass

        if _has_table(conn, "messages"):
            plan.message_rows = conn.execute(
                "select count(*) from messages").fetchone()[0] or 0
            if cutoff:
                # msg_time 可能是空串或短格式，缺就退回入库时间
                plan.message_old = conn.execute(
                    "select count(*) from messages where "
                    "coalesce(nullif(trim(msg_time), ''), created_at) < ?",
                    (cutoff,)).fetchone()[0] or 0

        if _has_table(conn, "deliveries"):
            plan.delivery_rows = conn.execute(
                "select count(*) from deliveries").fetchone()[0] or 0
            if cutoff:
                plan.delivery_old = conn.execute(
                    "select count(*) from deliveries where created_at < ?",
                    (cutoff,)).fetchone()[0] or 0
            if _has_table(conn, "messages"):
                plan.delivery_orphan = conn.execute(
                    "select count(*) from deliveries "
                    "where uid not in (select uid from messages)").fetchone()[0] or 0
    except sqlite3.DatabaseError as exc:
        plan.error = f"读数据库失败：{exc}"
    finally:
        conn.close()
    return plan


def summary_lines(plan: Plan, limit: int = 6) -> list[str]:
    """给人看的清单（设置中心对话框直接用）。"""
    out = [
        f"数据库文件：{human_size(plan.file_bytes)}"
        f"（主库 {human_size(plan.db_bytes)}，其余是 WAL 与索引）",
        f"消息：共 {plan.message_rows} 条" + (
            f"，其中 {plan.message_old} 条早于 {plan.cutoff}（将删除）"
            if plan.message_old else "，没有超出保留期的"),
        f"投递记录：共 {plan.delivery_rows} 条" + (
            f"，其中过期的 {plan.delivery_old} 条、孤儿 {plan.delivery_orphan} 条"
            "（将删除）" if plan.delivery_doomed else "，没有需要删的"),
    ]
    if plan.freelist_count:
        hole = plan.freelist_count * plan.page_size
        out.append(f"另有已删未回收的空白页：{human_size(hole)}（VACUUM 会一起收回）")
    if plan.reclaimable:
        out.append(f"预计回收：约 {human_size(plan.reclaimable)}")
    return out


def execute(plan: Plan, vacuum: bool = True) -> Result:
    """按体检结果动手：删行 → checkpoint → VACUUM。

    删与 VACUUM 分成两步：删是必须成功的（否则回滚），VACUUM 抢不到锁只
    降级（记 note），不把已经删掉的事实报成失败。
    """
    res = Result(before_bytes=plan.file_bytes or _file_bytes(plan.db_path))
    if plan.error:
        res.error = plan.error
        return res

    try:
        conn = _connect(plan.db_path)
    except sqlite3.DatabaseError as exc:
        res.error = f"打不开数据库：{exc}"
        return res

    try:
        with conn:                      # 一个事务：要么全删，要么一行不动
            if plan.cutoff and plan.message_old:
                cur = conn.execute(
                    "delete from messages where "
                    "coalesce(nullif(trim(msg_time), ''), created_at) < ?",
                    (plan.cutoff,))
                res.deleted_messages = cur.rowcount or 0
            if plan.delivery_doomed:
                if plan.cutoff:
                    cur = conn.execute(
                        "delete from deliveries where created_at < ?", (plan.cutoff,))
                    res.deleted_deliveries = cur.rowcount or 0
                cur = conn.execute(
                    "delete from deliveries where uid not in "
                    "(select uid from messages)")
                res.deleted_deliveries += cur.rowcount or 0
    except sqlite3.DatabaseError as exc:
        res.error = f"删除失败（已回滚）：{exc}"
        try:
            conn.close()
        except sqlite3.DatabaseError:
            pass
        return res

    if not vacuum:
        try:
            conn.close()
        except sqlite3.DatabaseError:
            pass
        res.after_bytes = _file_bytes(plan.db_path)
        res.note = "未执行 VACUUM：文件大小不会马上变小，下次清理会一起收缩。"
        return res

    # VACUUM 前的 checkpoint 是硬要求：WAL 里还挂着页时 VACUUM 会直接 busy，
    # 或者把数据再搬回 -wal，白忙一场。
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError:
        pass
    try:
        conn.execute("VACUUM")
        res.vacuumed = True
    except sqlite3.DatabaseError as exc:
        res.note = (f"数据已删除，但空间没回收（{exc}）——"
                    "多半是转发程序还在运行、库被占用。停掉转发后再点一次即可。")
        # 让 sqlite 释放文件句柄后再量体积
        try:
            conn.close()
        except sqlite3.DatabaseError:
            pass
        time.sleep(0.3)
        res.after_bytes = _file_bytes(plan.db_path)
        return res
    finally:
        if res.vacuumed:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass

    try:
        conn.close()
    except sqlite3.DatabaseError:
        pass
    time.sleep(0.3)
    res.after_bytes = _file_bytes(plan.db_path)
    return res


def auto_clean_if_due(db_path: str | Path, keep_days: int,
                      min_bytes: int = 80 * 1024 * 1024) -> Result | None:
    """库里超过 ``min_bytes`` 才顺手清一次（给启动时用，可选）。

    默认不接进启动流程 —— 删数据这件事应该在用户知情时发生。留这个函数是
    给「以后想开自动清理」留口子：阈值卡在体积而不是时间，避免小库天天
    VACUUM。
    """
    p = Path(db_path)
    if not p.is_file() or _file_bytes(p) < min_bytes:
        return None
    plan = scan(p, keep_days)
    if plan.error or plan.nothing_to_do:
        return None
    return execute(plan)


def journal_size(db_path: str | Path) -> int:
    """``-wal`` 文件大小（排查「库不大但占盘很大」时用）。"""
    try:
        return os.path.getsize(f"{db_path}-wal")
    except OSError:
        return 0


__all__ = ["Plan", "Result", "scan", "execute", "summary_lines", "human_size",
           "auto_clean_if_due", "journal_size", "DEFAULT_KEEP_DAYS",
           "MIN_KEEP_DAYS", "KEEP_CHOICES", "KEEP_LABELS"]
