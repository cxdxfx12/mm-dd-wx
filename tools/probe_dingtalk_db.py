"""钉钉 PC 端本地库离线解密 + WAL 合并 + 查询探针。

用途：验证「钉钉 PC 客户端里到底有没有某个群、某条消息、某个附件」，
不依赖界面，不受 OCR 误读影响。

解密与 WAL 合并的实现在 ``bridge/dingtalk_db.py``（正式运行时的漏转对账
也用同一套代码，避免两份实现漂移）；本文件只负责「把结果打给人看」。

用法
    python tools/probe_dingtalk_db.py --status          # 版本/滞后/总量
    python tools/probe_dingtalk_db.py --list 30 --grep 申通
    python tools/probe_dingtalk_db.py --cid 1234 --limit 20
    python tools/probe_dingtalk_db.py --cid 1234 --files
    python tools/probe_dingtalk_db.py --cid 79617007357 --reconcile 数字质控

依赖：pycryptodome（``pip install pycryptodome``）。安装包会带上它，
源码环境下需要自己装。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge import dingtalk_db as ddb                    # noqa: E402
from bridge.dingtalk_db import (DingTalkDbError, collect_files,  # noqa: E402
                                find_data_dir, load_cards, sync)


def _fmt(ms) -> str:
    if not ms:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000))


# ---------------------------------------------------------------- 展示

def show_status(conn, info: dict) -> None:
    total = 0
    for t in ddb.msg_tables(conn):
        row = conn.execute(f'select count(*), max(createdAt) from "{t}"').fetchone()
        total += row[0] or 0
    conv = conn.execute("select count(*) from tbconversation").fetchone()[0]
    print(f"  UID          : {info['uid']} (V{info['version']})")
    print(f"  WAL          : {info['wal_bytes'] / 1e6:.1f} MB，帧 {info['frames']}，"
          f"合并方式 {info['method']}")
    print(f"  主库滞后     : {info['wal_lag_min']:.0f} 分钟（WAL 比主库新的时长）")
    print(f"  会话 / 消息  : {conv} / {total}，最新 {_fmt(ddb.latest_ts(conn))}")
    print(f"  解密库       : {info['db']}")


def show_list(conn, limit: int, grep: str | None) -> None:
    sql = "select cid,title,memberCount,lastModify from tbconversation"
    params: list = []
    if grep:
        sql += " where title like ?"
        params.append(f"%{grep}%")
    sql += " order by lastModify desc limit ?"
    params.append(limit)
    for cid, title, members, last in conn.execute(sql, params):
        print(f"  {_fmt(last)}  {str(cid):>18}  {title or '(无标题)'}  [{members}人]")


def show_messages(conn, cid: str, limit: int, only_files: bool) -> None:
    rows = []
    # 只看附件时先多捞一些再筛，否则最近 20 条里没文件就什么都看不到
    fetch = limit * 10 if only_files else limit
    for t in ddb.msg_tables(conn):
        try:
            rows += conn.execute(
                f'select createdAt, senderId, contentType, content from "{t}" '
                f'where cid=? order by createdAt desc limit ?', (cid, fetch)).fetchall()
        except sqlite3.OperationalError:
            continue
    rows.sort(key=lambda r: r[0] or 0, reverse=True)
    if not rows:
        print(f"  该 cid 在库里没有消息：{cid}")
        return
    shown = 0
    for ts, sid, ctype, content in rows:
        files = collect_files(content)
        if only_files and not files:
            continue
        if files:
            body = "[文件] " + ", ".join(files)
        else:
            try:
                data = json.loads(content or "{}")
            except Exception:
                data = {}
            body = str(data.get("text") or data.get("title") or "")[:120]
        print(f"  {_fmt(ts)}  {str(sid):>12}  type={ctype:<5} {body}")
        shown += 1
        if shown >= limit:
            break


def default_bridge_db() -> Path:
    """桥的 bridge.db：优先安装目录，其次仓库里的 data/。"""
    candidates = [
        Path(r"C:\Program Files\喵喵钉钉-微信消息转发桥\data\bridge.db"),
        ROOT / "data" / "bridge.db",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return candidates[0]


def _load_forwarded(bridge_db: Path, chat_kw: str, since: float) -> list[tuple[float, str, str]]:
    """桥侧已入库的消息：msg_time 字符串是本地时间（可能被取整到分钟）。"""
    conn = sqlite3.connect(f"file:{bridge_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select msg_time, msg_type, content from messages where chat_name like ?",
            (f"%{chat_kw}%",)).fetchall()
    finally:
        conn.close()
    out = []
    for stamp, kind, content in rows:
        try:
            ts = time.mktime(time.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        if ts >= since:
            out.append((ts, kind or "", (content or "").replace("\n", " ")))
    return out


def _norm(text: str) -> str:
    return re.sub(r"[\s@#*【】\[\]（）()，。,.：:！!～~\-]", "", text or "")


def show_reconcile(conn, cid: str, bridge_db: Path, chat_kw: str, hours: float,
                   tolerance: float = 1800.0) -> None:
    """钉钉库为准，检查桥有没有漏转。

    两道判据，都不完美，所以只输出「疑似」供人眼确认：
      1. 标题优先——卡片标题常原样出现在桥的正文里（``拦截任务风险提醒-中心未到件``），
         这条不受时间错位影响；
      2. 时间兜底——OCR 时间条只到分钟、再叠加重试延迟，实测同一张卡片能差
         十几分钟，所以容差给到 tolerance 秒。
    桥把一张卡片拆成正文/表格图/附件多条属正常，反向的「多」只报个数不列明细。
    """
    since = time.time() - hours * 3600
    cards = load_cards(conn, cid, int(since * 1000))
    forwarded = _load_forwarded(bridge_db, chat_kw, since - tolerance)
    print(f"  窗口内钉钉卡片 {len(cards)} 条 / 桥已入库 {len(forwarded)} 条")

    used = [False] * len(forwarded)
    missing = []
    for ts, title in cards:
        sec = ts / 1000
        needle = _norm(title)
        hit = None
        if len(needle) >= 5:
            for i, (_fts, _kind, content) in enumerate(forwarded):
                if not used[i] and needle in _norm(content):
                    hit = i
                    break
        if hit is None:
            near = [(abs(f[0] - sec), i) for i, f in enumerate(forwarded)
                    if not used[i] and abs(f[0] - sec) <= tolerance]
            if near:
                hit = min(near)[1]
        if hit is None:
            missing.append((ts, title))
        else:
            used[hit] = True

    print(f"  桥侧未被认领 {sum(1 for u in used if not u)} 条"
          "（多为同一条消息的正文/表格图/附件多段）")
    if not missing:
        print("  ✅ 没有疑似漏转")
        return
    print(f"  ⚠ 疑似漏转 {len(missing)} 条"
          f"（标题对不上、且 {tolerance/60:.0f} 分钟内没有可认领记录）：")
    for ts, title in missing:
        print(f"    {time.strftime('%m-%d %H:%M:%S', time.localtime(ts / 1000))}"
              f"  {title or '(无标题)'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="钉钉 PC 库解密 + WAL 合并探针")
    ap.add_argument("--dir", help="钉钉数据目录（默认自动查找最近活跃的）")
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "dtprobe_out"),
                    help="解密结果目录")
    ap.add_argument("--status", action="store_true", help="只看版本/滞后/总量")
    ap.add_argument("--list", type=int, metavar="N", help="列出最近 N 个会话")
    ap.add_argument("--grep", metavar="KW", help="会话名关键词")
    ap.add_argument("--cid", help="按会话 cid 看消息")
    ap.add_argument("--limit", type=int, default=20, help="消息条数，默认 20")
    ap.add_argument("--files", action="store_true", help="只看带附件的消息")
    ap.add_argument("--reconcile", metavar="CHAT", help="漏转对账：桥里的群名关键词（配合 --cid）")
    ap.add_argument("--hours", type=float, default=12, help="对账回看小时数，默认 12")
    ap.add_argument("--tolerance", type=float, default=30, help="时间兜底容差（分钟），默认 30")
    ap.add_argument("--bridge-db", help="桥的 bridge.db 路径（默认找安装目录）")
    args = ap.parse_args()

    data_dir = Path(args.dir) if args.dir else find_data_dir()
    if not data_dir or not (data_dir / "DBFiles" / "dingtalk.db").is_file():
        print("没找到钉钉数据目录，请用 --dir 指定")
        return 1

    print(f"数据目录: {data_dir}")
    try:
        info = sync(data_dir, Path(args.out))
    except DingTalkDbError as exc:
        print(f"解密失败：{exc}")
        return 1
    conn = sqlite3.connect(str(info["db"]))
    try:
        if args.reconcile:
            if not args.cid:
                print("--reconcile 需要配合 --cid 指定钉钉侧会话")
                return 1
            bridge_db = Path(args.bridge_db) if args.bridge_db else default_bridge_db()
            if not bridge_db.is_file():
                print(f"找不到 bridge.db：{bridge_db}")
                return 1
            print(f"对账: 钉钉 cid={args.cid} ↔ 桥「{args.reconcile}」"
                  f"（近 {args.hours:g} 小时）")
            show_reconcile(conn, args.cid, bridge_db, args.reconcile, args.hours,
                           tolerance=args.tolerance * 60)
            return 0
        if args.list or args.grep:
            show_list(conn, args.list or 30, args.grep)
        if args.cid:
            show_messages(conn, args.cid, args.limit, args.files)
        if args.status or not (args.list or args.grep or args.cid):
            show_status(conn, info)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
