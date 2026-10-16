"""程序图标生成（只用标准库，不依赖 pillow）。

设计
    圆角方块 + 从上到下的蓝→绿渐变（左蓝=钉钉，右绿=微信），中间一个白色
    「⇄」双向箭头，寓意消息在这两端来回搬运。

实现
    * 手写 PNG 编码（``zlib`` + ``struct``），Vista 以后的 ICO 允许直接内嵌 PNG；
    * 每个尺寸都用 2 倍超采样再降采样，得到抗锯齿边缘。

用法
    python app/icon.py            # 生成 app/app.ico
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ICO_PATH = APP_DIR / "app.ico"

BLUE = (42, 107, 242)        # 钉钉蓝
GREEN = (7, 193, 96)         # 微信绿
SS = 2                       # 超采样倍数


# ---------------------------------------------------------------- 形状（归一化坐标）

def _in_round_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float,
                   r: float) -> bool:
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    if x0 + r <= x <= x1 - r or y0 + r <= y <= y1 - r:
        return x0 <= x <= x1 and y0 <= y <= y1
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _in_capsule(x: float, y: float, x0: float, x1: float, yc: float, r: float) -> bool:
    return _in_round_rect(x, y, x0, yc - r, x1, yc + r, r)


def _in_tri(x: float, y: float, a, b, c) -> bool:
    def sign(p, q, r):
        return (p[0] - r[0]) * (q[1] - r[1]) - (q[0] - r[0]) * (p[1] - r[1])
    d1, d2, d3 = sign((x, y), a, b), sign((x, y), b, c), sign((x, y), c, a)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def _hit(x: float, y: float) -> bool:
    """⇄ 字形：两根胶囊横条 + 两个三角箭头。"""
    if _in_capsule(x, y, 0.22, 0.63, 0.395, 0.072):
        return True
    if _in_tri(x, y, (0.585, 0.255), (0.585, 0.535), (0.795, 0.395)):
        return True
    if _in_capsule(x, y, 0.37, 0.78, 0.605, 0.072):
        return True
    if _in_tri(x, y, (0.415, 0.465), (0.415, 0.745), (0.205, 0.605)):
        return True
    return False


# ---------------------------------------------------------------- 渲染

def render_rgba(size: int) -> bytearray:
    n = size * SS
    base = bytearray(size * size * 4)
    for py in range(size):
        for px in range(size):
            acc_r = acc_g = acc_b = acc_a = 0
            for sy in range(SS):
                for sx in range(SS):
                    X = (px * SS + sx + 0.5) / n
                    Y = (py * SS + sy + 0.5) / n
                    if not _in_round_rect(X, Y, 0.03, 0.03, 0.97, 0.97, 0.22):
                        continue
                    t = min(1.0, max(0.0, (X * 0.35 + Y * 0.65)))
                    r = BLUE[0] + (GREEN[0] - BLUE[0]) * t
                    g = BLUE[1] + (GREEN[1] - BLUE[1]) * t
                    b = BLUE[2] + (GREEN[2] - BLUE[2]) * t
                    if _hit(X, Y):
                        r = g = b = 255.0
                    acc_r += r
                    acc_g += g
                    acc_b += b
                    acc_a += 255
            total = SS * SS
            i = (py * size + px) * 4
            a = acc_a // total
            if a:
                # 未覆盖率用背景色填充，避免边缘出现深色描边
                base[i] = int(acc_r) // total
                base[i + 1] = int(acc_g) // total
                base[i + 2] = int(acc_b) // total
            base[i + 3] = a
    return base


def _png(rgba: bytes, w: int, h: int) -> bytes:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgba[y * w * 4:(y + 1) * w * 4]

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def build(path: str | Path = ICO_PATH, sizes=(256, 128, 64, 48, 32, 16)) -> Path:
    out = Path(path)
    entries = []
    for s in sizes:
        entries.append((s, _png(bytes(render_rgba(s)), s, s)))

    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    dirs, blobs = b"", b""
    for s, data in entries:
        w = 0 if s >= 256 else s
        dirs += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    out.write_bytes(header + dirs + blobs)
    return out


if __name__ == "__main__":
    p = build()
    print(f"已生成图标：{p}（{p.stat().st_size} 字节）")
    png_path = APP_DIR / "icon_64.png"
    png_path.write_bytes(_png(bytes(render_rgba(64)), 64, 64))
    print(f"已生成界面用 PNG：{png_path}")
