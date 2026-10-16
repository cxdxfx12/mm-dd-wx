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
