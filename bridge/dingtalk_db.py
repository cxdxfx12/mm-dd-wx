"""钉钉 PC 端本地库：离线解密 + WAL 合并 + 读卡片。

为什么需要它
    手机端采集（无障碍 + OCR）天生会漏：卡片被重绘、滑走、附件抓取超时，
    屏上就什么都没有了，而「到底该有几条」只有钉钉自己的库知道。这个模块
    把本机 PC 钉钉的库解出来，给 `bridge/reconcile.py` 当**对账基准**：
    库里有的卡片，桥有没有入库。

    它**不能**当采集源 —— 2950 互动卡片的正文、表格、附件都在服务端，
    本地只落一行标题（实测 214 张表全文本列检索，正文短语 0 命中）。

算法（V3，参考 dingwave-V3 / im-mcp 的实现）
    1. ``user_config`` 是 Base64(JSON)，取出 ``salt``；
    2. 真 UID 从 ``log/gaea.log*`` 里的 ``real_uid=<数字>@dingding`` 取，
       目录名 ``xxxx_v3`` 是哈希，**不能**当 UID 用；
    3. key = MD5(PBKDF2-HMAC-SHA1(uid+salt, b"666DingT", 1000, 32))[:16]；
    4. 主库按 4096 字节整页 AES-128-ECB 解密；
    5. WAL 不能只拷贝：帧内页同样是密文，必须逐帧解密，并用解密后的明文页
       重算 SQLite WAL 校验和（否则 sqlite 判 WAL 损坏直接忽略 —— 表现就是
       「只能读到几小时前的数据」）；
    6. 解好的 WAL 交给 sqlite 自己 checkpoint；原生 checkpoint 不认账时
       （钉钉 WAL 头的 salt/序列和主库对不上，sqlite 会当空 WAL），退化为
       按 pgno 直接回放页。

依赖：pycryptodome（可选功能，没装的时候 :func:`available` 返回 False，
对账器会自禁用，主流程完全不受影响）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import tempfile
from pathlib import Path

PAGE = 4096
PBKDF2_SALT = b"666DingT"
PBKDF2_ITER = 1000
PBKDF2_DKLEN = 32

SQLITE_MAGIC = b"SQLite format 3\x00"


class DingTalkDbError(RuntimeError):
    """解密/读取失败（给调用方一个能分辨的异常类型）。"""


def available() -> tuple[bool, str]:
    """对账功能能不能用：``(可用, 原因)``。

    不能用的原因要能直接展示给用户 —— 最常见的两种是「没装 pycryptodome」
    和「本机没装/没登录钉钉 PC 客户端」。
    """
    try:
        import Crypto.Cipher.AES  # noqa: F401
    except Exception:
        return False, "缺少 pycryptodome（离线包未附带），钉钉库对账不可用"
    if find_data_dir() is None:
        return False, "本机没找到钉钉 PC 客户端的数据目录（未安装或未登录）"
    return True, ""


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
        try:
            if not base.is_dir():
                continue
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or not entry.name.endswith(("_v2", "_v3")):
                continue
            db = entry / "DBFiles" / "dingtalk.db"
            try:
                if db.is_file():
                    ver = 3 if entry.name.endswith("_v3") else 2
                    found.append((db.stat().st_mtime, ver, entry))
            except OSError:
                continue
    if not found:
        return None
    found.sort(reverse=True)
    return found[0][2]


def read_salt(data_dir: Path) -> str | None:
    cfg = data_dir / "user_config"
    if not cfg.is_file():
        return None
    try:
        return json.loads(
            base64.b64decode(cfg.read_text(encoding="utf-8").strip())).get("salt")
    except Exception:
        return None


def derive_key(uid: str, version: int, salt: str | None) -> bytes:
    if version == 3:
        if not salt:
            raise DingTalkDbError("V3 库需要 user_config 里的 salt，读取失败")
        dk = hashlib.pbkdf2_hmac("sha1", (uid + salt).encode(), PBKDF2_SALT,
                                 PBKDF2_ITER, PBKDF2_DKLEN)
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
    if not log_dir.is_dir():
        return cands
    for fp in log_dir.rglob("*"):
        try:
            if not fp.is_file():
                continue
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
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

    from Crypto.Cipher import AES

    db = data_dir / "DBFiles" / "dingtalk.db"
    with db.open("rb") as f:
        sample = f.read(PAGE)
    for cand in _uid_candidates(data_dir):
        try:
            cipher = AES.new(derive_key(cand, 3, salt), AES.MODE_ECB)
        except Exception:
            continue
        if _decrypt_page(cipher, sample)[:16] == SQLITE_MAGIC:
            return cand
    raise DingTalkDbError(
        f"找不到能解开该库的 UID（目录名 {folder_id} 是哈希，不是 UID）。"
        "可设环境变量 DINGTALK_UID=<数字 UID> 后重试")


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


def _replay_frames(db_path: Path, enc_wal: Path, cipher) -> int:
    """兜底：直接按页号把 WAL 页覆盖回主库（sqlite 拒绝 checkpoint 时用）。

    ``enc_wal`` 必须是**密文**副本 —— 明文 WAL 再解一次就是乱码。
    """
    pages: dict[int, bytes] = {}
    raw = db_path.read_bytes()
    for i in range(0, len(raw), PAGE):
        pages[i // PAGE + 1] = raw[i:i + PAGE]

    frames = 0
    data = enc_wal.read_bytes()
    stride = 24 + PAGE
    for off in range(32, len(data) - stride + 1, stride):
        pgno = struct.unpack(">I", data[off:off + 4])[0]
        pages[pgno] = _decrypt_page(cipher, data[off + 24:off + stride])
        frames += 1

    if not frames:
        return 0
    tmp = db_path.with_suffix(".replay")
    with tmp.open("wb") as f:
        for p in range(1, max(pages) + 1):
            f.write(pages.get(p, b"\x00" * PAGE))
    tmp.replace(db_path)
    return frames


def sync(data_dir: Path, out_dir: Path) -> dict:
    """解密 + 合并 WAL，结果写到 ``out_dir/dingtalk.db``。

    返回 ``{"db", "uid", "version", "frames", "method", "wal_lag_min",
    "wal_bytes", "msg_latest"}``。实测全量约 7 秒（14 MB 主库 + 6.9 MB WAL）。
    """
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
    frames = 0
    method = "none"
    try:
        enc_db = tmp / "enc.db"
        shutil.copy2(src_db, enc_db)          # 先拷贝，避免锁原库
        decrypt_main(enc_db, out_db, cipher)
        if out_db.read_bytes()[:16] != SQLITE_MAGIC:
            raise DingTalkDbError(f"解密失败：页头不对，UID={uid} 可能不匹配")

        has_wal = src_wal.is_file() and src_wal.stat().st_size > 0
        if has_wal:
            enc_wal = tmp / "enc.db-wal"
            shutil.copy2(src_wal, enc_wal)
            frames = decrypt_wal(enc_wal, out_wal, cipher)
            try:
                conn = sqlite3.connect(str(out_db))
                try:
                    row = conn.execute("PRAGMA wal_checkpoint(FULL);").fetchone()
                    busy, total, ckpt = (row + (0, 0, 0))[:3]
                    # 钉钉的 WAL 头 salt/序列与主库对不上时 sqlite 当空 WAL
                    # （log=0），帧一个都不认 —— 这种情况必须自己回放。
                    if frames and busy == 0 and total > 0 and ckpt == total:
                        method = "native"
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                finally:
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
    try:
        if src_wal.is_file() and src_wal.stat().st_size:
            lag = (src_wal.stat().st_mtime - src_db.stat().st_mtime) / 60
    except OSError:
        pass
    return {
        "db": out_db, "uid": uid, "version": version,
        "frames": frames, "method": method, "wal_lag_min": lag,
        "wal_bytes": src_wal.stat().st_size if src_wal.is_file() else 0,
    }


# ---------------------------------------------------------------- 查询

def msg_tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "select name from sqlite_master where type='table' and name like 'tbmsg_%'")]


def card_title(content: str | None) -> str:
    """2950 互动卡片只在 extension 里留标题，正文/表格由客户端渲染，不落库。"""
    try:
        data = json.loads(content or "{}")
        ext = json.loads(data["attachments"][0]["extension"])
        return (ext.get("interactiveCardLastMessage")
                or ext.get("LastMessageI18n") or "")
    except Exception:
        return ""


def collect_files(content: str | None) -> list[str]:
    """把消息 JSON 里的附件名挖出来（contentType 501 的 f_name 在 extension 里）。"""
    if not content:
        return []
    try:
        data = json.loads(content)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    names: list[str] = []
    exts = [data.get("extension")] + [a.get("extension")
                                      for a in data.get("attachments") or []]
    for ext in exts:
        if isinstance(ext, str):
            try:
                ext = json.loads(ext)
            except Exception:
                continue
        if isinstance(ext, dict) and ext.get("f_name"):
            names.append(str(ext["f_name"]))
    return names


def find_cid(conn, keyword: str) -> tuple[str, str] | None:
    """按群名关键词找会话，返回 ``(cid, title)``；找不到返回 None。

    优先**完全等于**关键词的，再退到包含匹配 —— 否则「数字质控」会命中一堆
    同前缀的兄弟公司群，对账就看错群了。多个包含匹配时按最近活跃取。
    """
    kw = (keyword or "").strip()
    if not kw:
        return None
    try:
        exact = conn.execute(
            "select cid, title from tbconversation where title = ? "
            "order by lastModify desc limit 1", (kw,)).fetchone()
        if exact:
            return str(exact[0]), exact[1] or ""
        rows = conn.execute(
            "select cid, title from tbconversation where title like ? "
            "order by lastModify desc limit 5", (f"%{kw}%",)).fetchall()
    except sqlite3.DatabaseError:
        return None
    if not rows:
        return None
    return str(rows[0][0]), rows[0][1] or ""


def load_cards(conn, cid: str, since_ms: int) -> list[tuple[int, str]]:
    """某个会话在 ``since_ms`` 之后的卡片 ``[(createdAt_ms, 标题)]``（升序）。"""
    rows: list[tuple[int, str]] = []
    for t in msg_tables(conn):
        try:
            rows += conn.execute(
                f'select createdAt, content from "{t}" where cid=? and createdAt>=?',
                (cid, since_ms)).fetchall()
        except sqlite3.OperationalError:
            continue
    rows.sort(key=lambda r: r[0] or 0)
    return [(r[0], card_title(r[1])) for r in rows]


def latest_ts(conn, cid: str | None = None) -> int:
    """全库（或某会话）最新消息时间（毫秒），用来判断库是不是还在更新。"""
    best = 0
    for t in msg_tables(conn):
        try:
            sql = f'select max(createdAt) from "{t}"'
            if cid:
                row = conn.execute(sql + " where cid=?", (cid,)).fetchone()
            else:
                row = conn.execute(sql).fetchone()
        except sqlite3.OperationalError:
            continue
        best = max(best, (row[0] if row and row[0] else 0))
    return best
