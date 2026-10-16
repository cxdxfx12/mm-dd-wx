"""存储层：SQLite 落库 + 幂等去重 + 投递日志 + 导出。

所有 SQL 均使用参数绑定；去重判断下推为 INSERT OR IGNORE，
不做「先全表查再在应用层过滤」。
"""
from __future__ import annotations

import csv
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .models import Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    uid         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    chat_id     TEXT,
    chat_name   TEXT,
    sender_id   TEXT,
    sender_name TEXT,
    company     TEXT,
    content     TEXT,
    msg_type    TEXT,
    media_path  TEXT,
    msg_time    TEXT,
    raw_id      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_time    ON messages(msg_time);
CREATE INDEX IF NOT EXISTS idx_msg_chat    ON messages(chat_name);
CREATE INDEX IF NOT EXISTS idx_msg_company ON messages(company);

CREATE TABLE IF NOT EXISTS deliveries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        TEXT NOT NULL,
    target     TEXT NOT NULL,
    status     TEXT NOT NULL,
    error      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delivery_uid ON deliveries(uid);
"""


def _parse_dt(text: str) -> datetime | None:
    """宽容解析库里的时间字符串 —— 解不出来就返回 None，不抛异常。

    库里存的是 ``%Y-%m-%d %H:%M:%S``，但手工改过库、或早期版本留下的
    短格式不该让整条补发流程崩掉：时间只影响渲染出来的时间占位，
    内容本身照发。
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(text), fmt)
        except (TypeError, ValueError):
            continue
    return None


def row_to_message(row) -> Message:
    """把库里的一行还原成 Message —— 「补发最近 N 条」需要完整的消息对象。

    刻意保留 `raw_id`：还原出来的 `uid` 与当初入库时一模一样，
    补发的投递记录才能正确挂回同一条消息上。
    """
    return Message(
        source=row["source"] or "dingtalk",
        chat_id=row["chat_id"] or "",
        chat_name=row["chat_name"] or "",
        sender_id=row["sender_id"] or "",
        sender_name=row["sender_name"] or "",
        company=row["company"] or "",
        content=row["content"] or "",
        msg_type=row["msg_type"] or "text",
        media_path=row["media_path"] or "",
        msg_time=_parse_dt(row["msg_time"]),
        raw_id=row["raw_id"] or "",
    )


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 写入 ----------

    def save_message(self, msg: Message) -> bool:
        """写入消息。返回 True 表示新消息，False 表示已存在（重复）。"""
        row = msg.as_dict()
        row["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = """INSERT OR IGNORE INTO messages
                 (uid, source, chat_id, chat_name, sender_id, sender_name,
                  company, content, msg_type, media_path, msg_time, raw_id, created_at)
                 VALUES (:uid, :source, :chat_id, :chat_name, :sender_id, :sender_name,
                         :company, :content, :msg_type, :media_path, :msg_time, :raw_id, :created_at)"""
        with self._lock:
            cur = self._conn.execute(sql, row)
            self._conn.commit()
            return cur.rowcount > 0

    def is_duplicate(self, uid: str) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM messages WHERE uid = ? LIMIT 1", (uid,))
            return cur.fetchone() is not None

    def log_delivery(self, uid: str, target: str, status: str, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO deliveries (uid, target, status, error, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, target, status, error, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            self._conn.commit()

    # ---------- 查询 ----------

    def query(
        self,
        limit: int = 50,
        chat_name: str | None = None,
        company: str | None = None,
        keyword: str | None = None,
    ) -> list[sqlite3.Row]:
        """条件全部下推到 SQL，不在应用层做全表过滤。"""
        where, params = [], []
        if chat_name:
            where.append("chat_name = ?")
            params.append(chat_name)
        if company:
            where.append("company = ?")
            params.append(company)
        if keyword:
            where.append("content LIKE ?")
            params.append(f"%{keyword}%")
        sql = "SELECT * FROM messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY msg_time DESC, created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def recent(
        self,
        limit: int = 3,
        chat_name: str | None = None,
        only_failed: bool = False,
    ) -> list[sqlite3.Row]:
        """取最近入库的 N 条消息，**按发生顺序（旧 → 新）返回**。

        与 `query()` 的两点差别，都是「补发」场景需要的：

        1. 排序用 ``msg_time`` + ``rowid``，取完再翻正 —— 补发必须按当初
           发生的顺序发，倒着发会让群里的人读反；
        2. ``only_failed=True`` 只挑「最近一次投递状态是 fail」的消息。

        这里用子查询按 id 倒序取最后一条投递记录，而不是 ``EXISTS(... 'fail')``：
        失败后已经重发成功的消息不该再算成待补发，否则每点一次都会重发一遍。
        """
        where, params = [], []
        if chat_name:
            where.append("m.chat_name = ?")
            params.append(chat_name)
        if only_failed:
            where.append("(SELECT d.status FROM deliveries d WHERE d.uid = m.uid"
                         " ORDER BY d.id DESC LIMIT 1) = 'fail'")
        sql = "SELECT m.* FROM messages m"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.msg_time DESC, m.rowid DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return list(reversed(rows))

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            by_company = self._conn.execute(
                "SELECT company, COUNT(*) AS n FROM messages GROUP BY company ORDER BY n DESC"
            ).fetchall()
            by_status = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"
            ).fetchall()
        return {
            "total_messages": total,
            "by_company": {r["company"]: r["n"] for r in by_company},
            "deliveries": {r["status"]: r["n"] for r in by_status},
        }

    def export_csv(self, path: str | Path, rows: Iterable[sqlite3.Row] | None = None) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = list(rows) if rows is not None else self.query(limit=10 ** 9)
        headers = ["msg_time", "company", "chat_name", "sender_name", "msg_type", "content", "uid"]
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in data:
                writer.writerow([r[h] for h in headers])
        return out
