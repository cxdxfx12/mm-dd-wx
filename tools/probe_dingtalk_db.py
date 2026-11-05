"""钉钉 PC 端本地库离线解密 + WAL 合并 + 查询探针。

用途：验证「钉钉 PC 客户端里到底有没有某个群、某条消息、某个附件」，
不依赖界面，不受 OCR 误读影响。参考 dingwave-V3 / im-mcp 的 V3 解密实现。

算法（V3）
    1. ``user_config`` 是 Base64(JSON)，取出 ``salt``；
    2. 真 UID 从 ``log/gaea.log*`` 里的 ``real_uid=<数字>@dingding`` 取，
       目录名 ``xxxx_v3`` 是哈希，**不能**当 UID 用；
    3. key = MD5(PBKDF2-HMAC-SHA1(uid+salt, b"666DingT", 1000, 32))[:16]；
    4. 主库按 4096 字节整页 AES-128-ECB 解密；
    5. WAL 不能只拷贝：帧内页同样是密文，必须逐帧解密，并用解密后的明文页
       重算 SQLite WAL 校验和（否则 sqlite 判 WAL 损坏，直接忽略 —— 表现
       就是「只能读到几小时前的数据」）；
    6. 解好的 WAL 交给 sqlite 自己 checkpoint(FULL/TRUNCATE) 合进主库；
       原生 checkpoint 不认账时，退化为按 pgno 直接回放页。

用法
    python tools/probe_dingtalk_db.py --status          # 版本/滞后/总量
    python tools/probe_dingtalk_db.py --list 30 --grep 申通
    python tools/probe_dingtalk_db.py --cid 1234 --limit 20
    python tools/probe_dingtalk_db.py --cid 1234 --files

依赖：pycryptodome（``pip install pycryptodome``），仅本工具需要，
不进 requirements.txt，也不随安装包分发。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
from pathlib import Path

PAGE = 4096
PBKDF2_SALT = b"666DingT"
PBKDF2_ITER = 1000
PBKDF2_DKLEN = 32


# ---------------------------------------------------------------- 定位与密钥

def find_data_dir() -> Path | None:
    """找最近活跃的钉钉数据目录（``*_v3`` / ``*_v2``）。"""
    bases = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        v = os.environ.get(env)
        if v:
            bases.append(Path(v) / "DingTalk")
    home = Path.home()
    bases.append(home / "AppData" / "Roaming" / "DingTalk")
    bases.append(home / "AppData" / "Local" / "DingTalk")

    found: list[tuple[float, int, Path]] = []
    for base in dict.fromkeys(bases):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if not entry.is_dir() or not entry.name.endswith(("_v2", "_v3")):
                continue
            db = entry / "DBFiles" / "dingtalk.db"
            if db.is_file():
                ver = 3 if entry.name.endswith("_v3") else 2
                found.append((db.stat().st_mtime, ver, entry))
    if not found:
        return None
    found.sort(reverse=True)
    return found[0][2]


def read_salt(data_dir: Path) -> str | None:
    cfg = data_dir / "user_config"
    if not cfg.is_file():
        return None
    try:
        return json.loads(base64.b64decode(cfg.read_text(encoding="utf-8").strip())).get("salt")
    except Exception:
        return None


def derive_key(uid: str, version: int, salt: str | None) -> bytes:
    if version == 3:
        if not salt:
            raise ValueError("V3 需要 user_config 里的 salt")
        dk = hashlib.pbkdf2_hmac("sha1", (uid + salt).encode(), PBKDF2_SALT, PBKDF2_ITER, PBKDF2_DKLEN)
        return hashlib.md5(dk).hexdigest()[:16].encode("ascii")
    return hashlib.md5(uid.encode()).hexdigest()[:16].encode("ascii")


def _decrypt_page(cipher, raw: bytes) -> bytes:
    out = bytearray()
    for i in range(0, PAGE, 16):
        out.extend(cipher.decrypt(raw[i:i + 16]))
    return bytes(out)


def _uid_candidates(data_dir: Path) -> list[str]:
    """从日志里挖候选 UID。注意日志是 ``gaea.log.2026-11-05`` 这种轮转名，
    只认 ``*.log`` 会一个都扫不到（im-mcp 在 Windows 上就栽在这里）。"""
    cands: list[str] = []
    log_dir = data_dir.parent / "log"
    if log_dir.is_dir():
        for fp in log_dir.rglob("*"):
            if not fp.is_file():
                continue
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for line in text.splitlines():
                if "real_uid" not in line:
                    continue
                seg = line.split("real_uid", 1)[1]
                digits = ""
                for ch in seg:
                    if ch.isdigit():
                        digits += ch
                    elif digits:
                        break
                if 6 <= len(digits) <= 12 and digits not in cands:
                    cands.append(digits)
    return cands


def resolve_uid(data_dir: Path, version: int, salt: str | None) -> str:
    env_uid = (os.environ.get("DINGTALK_UID") or "").strip()
    if env_uid:
        return env_uid
    folder_id = data_dir.name.rsplit("_v", 1)[0]
    if version == 2:
        return folder_id           # V2 的目录名就是 uid

    db = data_dir / "DBFiles" / "dingtalk.db"
    with db.open("rb") as f:
        sample = f.read(PAGE)
    from Crypto.Cipher import AES
    for cand in _uid_candidates(data_dir):
        try:
            cipher = AES.new(derive_key(cand, 3, salt), AES.MODE_ECB)
        except Exception:
            continue
        if _decrypt_page(cipher, sample)[:16] == b"SQLite format 3\x00":
            return cand
    raise SystemExit(
        "找不到能解开该库的 UID。\n"
        f"  目录名 {folder_id} 是哈希，不是 UID。\n"
        "  请手动指定：set DINGTALK_UID=<数字 UID> 后重跑。")


# ---------------------------------------------------------------- 解密与合并

def _wal_cksum(data: bytes, s0: int = 0, s1: int = 0, be: bool = False) -> tuple[int, int]:
    """SQLite WAL 校验和：按 8 字节步进，字节序由 magic 的最低位决定。"""
    fmt = ">II" if be else "<II"
    for i in range(0, len(data) - 7, 8):
        x0, x1 = struct.unpack(fmt, data[i:i + 8])
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def decrypt_main(src: Path, dst: Path, cipher) -> int:
    data = src.read_bytes()
    out = bytearray()
    pages = 0
    for off in range(0, len(data), PAGE):
        pg = data[off:off + PAGE]
        if len(pg) == PAGE:
            out.extend(_decrypt_page(cipher, pg))
            pages += 1
        else:
            out.extend(pg)          # 尾部残页原样保留
    dst.write_bytes(bytes(out))
    return pages


def decrypt_wal(src: Path, dst: Path, cipher) -> int:
    """逐帧解密并重算校验和。返回帧数。"""
    data = src.read_bytes()
    if len(data) < 32:
        return 0
    hdr = bytearray(data[:32])
    be = (struct.unpack(">I", bytes(hdr[:4]))[0] & 1) == 1
    s0, s1 = _wal_cksum(bytes(hdr[:24]), be=be)
    hdr[24:32] = struct.pack(">II" if be else "<II", s0, s1)

    out = bytearray(hdr)
    frames = 0
    stride = 24 + PAGE
    for off in range(32, len(data) - stride + 1, stride):
        frame = data[off:off + stride]
        fhdr = bytearray(frame[:24])
        page = _decrypt_page(cipher, frame[24:])
        a0, a1 = _wal_cksum(bytes(fhdr[:8]), s0, s1, be)
        b0, b1 = _wal_cksum(page, a0, a1, be)
        fhdr[16:24] = struct.pack(">II" if be else "<II", b0, b1)
        s0, s1 = b0, b1
        out.extend(fhdr)
        out.extend(page)
        frames += 1
    dst.write_bytes(bytes(out))
    return frames


def _replay_frames(db_path: Path, wal_path: Path, cipher) -> int:
    """兜底：直接按页号把 WAL 页覆盖回主库（sqlite 拒绝 checkpoint 时用）。"""
    pages: dict[int, bytes] = {}
    raw = db_path.read_bytes()
    for i in range(0, len(raw), PAGE):
        pages[i // PAGE + 1] = raw[i:i + PAGE]

    frames = 0
    data = wal_path.read_bytes()
    stride = 24 + PAGE
    for off in range(32, len(data) - stride + 1, stride):
        pgno = struct.unpack(">I", data[off:off + 4])[0]
        pages[pgno] = _decrypt_page(cipher, data[off + 24:off + stride])
        frames += 1

    if not pages:
        return 0
    tmp = db_path.with_suffix(".replay")
    with tmp.open("wb") as f:
        for p in range(1, max(pages) + 1):
            f.write(pages.get(p, b"\x00" * PAGE))
    tmp.replace(db_path)
    return frames


def sync(data_dir: Path, out_dir: Path) -> dict:
    from Crypto.Cipher import AES

    version = 3 if data_dir.name.endswith("_v3") else 2
    salt = read_salt(data_dir) if version == 3 else None
    uid = resolve_uid(data_dir, version, salt)
    cipher = AES.new(derive_key(uid, version, salt), AES.MODE_ECB)

    src_db = data_dir / "DBFiles" / "dingtalk.db"
    src_wal = data_dir / "DBFiles" / "dingtalk.db-wal"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_db = out_dir / "dingtalk.db"
    out_wal = out_dir / "dingtalk.db-wal"

    # 上一轮残留的 WAL/SHM 会被 sqlite 叠到新库上，读成 malformed
    for stale in (out_wal, out_dir / "dingtalk.db-shm"):
        stale.unlink(missing_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="dtprobe_"))
    try:
        enc_db = tmp / "enc.db"
        shutil.copy2(src_db, enc_db)          # 先拷贝，避免锁原库
        decrypt_main(enc_db, out_db, cipher)
        if out_db.read_bytes()[:16] != b"SQLite format 3\x00":
            raise SystemExit(f"解密失败：页头不对，UID={uid} 可能不匹配")

        has_wal = src_wal.is_file() and src_wal.stat().st_size > 0
        frames = 0
        method = "none"
        if has_wal:
            enc_wal = tmp / "enc.db-wal"
            shutil.copy2(src_wal, enc_wal)
            frames = decrypt_wal(enc_wal, out_wal, cipher)
            # enc_wal 是密文副本，兜底回放要用它重新解（out_wal 已是明文，
            # 再解一次就成乱码了）。sqlite 认为 WAL 无效时会在 close() 时
            # 直接删掉 -wal，所以回放必须回到 enc_wal。
            try:
                conn = sqlite3.connect(str(out_db))
                busy, total, ckpt = (conn.execute("PRAGMA wal_checkpoint(FULL);").fetchone() + (0, 0, 0))[:3]
                # 钉钉的 WAL 头 salt/序列与主库对不上时，sqlite 会当成空 WAL
                # （log=0），帧数一个都不认——这种情况必须自己回放。
                if frames and busy == 0 and total > 0 and ckpt == total:
                    method = "native"
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.close()
            except sqlite3.DatabaseError:
                pass
            (out_dir / "dingtalk.db-shm").unlink(missing_ok=True)
            if method != "native":
                applied = _replay_frames(out_db, enc_wal, cipher)
                method = f"replay({applied})"
            out_wal.unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    lag = 0.0
    if src_wal.is_file() and src_wal.stat().st_size:
        lag = (src_wal.stat().st_mtime - src_db.stat().st_mtime) / 60
    return {
        "db": out_db, "uid": uid, "version": version,
        "frames": frames, "method": method, "wal_lag_min": lag,
        "wal_bytes": src_wal.stat().st_size if src_wal.is_file() else 0,
    }


# ---------------------------------------------------------------- 查询

def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "select name from sqlite_master where type='table' and name like 'tbmsg_%'")]


def _fmt(ms) -> str:
    if not ms:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000))


def collect_files(content: str | None) -> list[str]:
    """把消息 JSON 里的附件名挖出来（contentType 501 的 f_name 在 extension 里）。"""
    if not content:
        return []
    try:
        data = json.loads(content)
    except Exception:
        return []
    names: list[str] = []
    exts = [data.get("extension")] + [a.get("extension") for a in data.get("attachments") or []]
    for ext in exts:
        if isinstance(ext, str):
            try:
                ext = json.loads(ext)
            except Exception:
                continue
        if isinstance(ext, dict) and ext.get("f_name"):
            names.append(str(ext["f_name"]))
    return names


def show_status(conn, info: dict) -> None:
    total = 0
    latest = 0
    for t in _tables(conn):
        row = conn.execute(f'select count(*), max(createdAt) from "{t}"').fetchone()
        total += row[0] or 0
        latest = max(latest, row[1] or 0)
    conv = conn.execute("select count(*) from tbconversation").fetchone()[0]
    print(f"  UID          : {info['uid']} (V{info['version']})")
    print(f"  WAL          : {info['wal_bytes'] / 1e6:.1f} MB，帧 {info['frames']}，合并方式 {info['method']}")
    print(f"  主库滞后     : {info['wal_lag_min']:.0f} 分钟（WAL 比主库新的时长）")
    print(f"  会话 / 消息  : {conv} / {total}，最新 {_fmt(latest)}")
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
    for t in _tables(conn):
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
        body = ""
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
    args = ap.parse_args()

    data_dir = Path(args.dir) if args.dir else find_data_dir()
    if not data_dir or not (data_dir / "DBFiles" / "dingtalk.db").is_file():
        print("没找到钉钉数据目录，请用 --dir 指定")
        return 1

    print(f"数据目录: {data_dir}")
    info = sync(data_dir, Path(args.out))
    conn = sqlite3.connect(str(info["db"]))
    try:
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
