"""把钉钉卡片里的表格画成一张图片，好让微信群里能直接看。

## 为什么需要这个

微信聊天**没有表格消息类型**，只有文本 / 图片 / 文件。钉钉机器人卡片里的
表格本来是一张二维表，采集端把它压成了这样一行文字：

    小件员名称: 周吉祥 ｜ 票数: 1票 ｜ 单号: 777454064365026

信息没丢，但手机上看起来是长长一串，列对不上、数字读不出来。

## 怎么还原成表

上游已经做完了最难的一步：``android_parse._inline_table`` 会按 x 坐标把
H5 卡片的单元格还原成二维网格，再按 ``列名: 值 ｜ 列名: 值`` 渲染。所以
这里**只做解析 + 绘制**：

    "小件员名称: 周吉祥 ｜ 票数: 1票 ｜ 单号: 777454064365026"
    → [["周吉祥", "1票", "777454064365026"]]
    → 配上一行表头，画成 PNG

## 刻意不做的事

  · **不重新解析 XML**。表格结构在 ``android_parse`` 里已经还原过一次，
    在这里再写一份必然漂移（同 ``fingerprint.py`` 的教训）。
  · **不追求好看**。图片的用途是「让人一眼看清哪一列是什么」，不是宣传
    物料。所以只用系统字体、单线框、斑马纹，不引第三方绘图库之外的依赖。
  · **文字仍照常发**。图片是补充，不是替代 —— 用户要能复制文字、也要能
    一眼看懂表，两者不冲突（``table_image.mode`` 可选）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# 单元格分隔符，与 ``android_parse._render_table`` 用的是同一个字符串。
# 两处必须一致 —— 改了那边不改这边，表格就一张都识别不出来。
CELL_SEP = " ｜ "

# 一行正文里至少要这么多格才算「表格行」。两格（``键: 值``）太常见了，
# 普通消息里到处都是（「时间: 08:13 ｜ 网点: 大江东」），不该被当成表格。
MIN_CELLS = 3

# 画出来的图最宽多少像素。微信群里的图会被压到屏宽显示，太宽了字反而看不清，
# 所以到上限就折行（而不是整图缩小）：宁可表变高，也别让数字糊成一片。
MAX_W = 1200
MIN_W = 480

# 配色（浅色，微信里不论深浅主题都看得清 —— 图片本身自带白底）
FG = (32, 32, 32)
FG_DIM = (96, 96, 96)
BG = (255, 255, 255)
HEAD_BG = (240, 243, 248)
ZEBRA = (250, 251, 253)
LINE = (216, 220, 226)

# 字号与内边距（像素）
FONT_SIZE = 26
HEAD_FONT_SIZE = 27
PAD_X, PAD_Y = 18, 14
MARGIN = 24
TITLE_SIZE = 30

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",        # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",      # 黑体
    r"C:\Windows\Fonts\simsun.ttc",      # 宋体
)


def _load_font(size: int):
    """找一个能显示中文的字体。找不到就用 PIL 默认字体（中文会变方块）。"""
    from PIL import ImageFont

    for p in _FONT_CANDIDATES:
        try:
            if Path(p).is_file():
                return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def parse_rows(text: str) -> list[list[str]]:
    """从正文里挑出所有「表格行」，返回二维单元格列表。

    判据只看 ``CELL_SEP`` 分隔出的格数（``>= MIN_CELLS``）。列名本身不要
    ——同一张表的每一行都带着列名，画图时表头只画一次。

    ⚠ 同一张卡片里可能有多段表格行，列数还不一样（真机上周报有 10 列、
    拦截单是 3 列）。这里**按列数分组**，列数不同的行不会被拼进同一张表。
    """
    groups: dict[int, list[list[str]]] = {}
    for line in (text or "").splitlines():
        cells = [c.strip() for c in line.split(CELL_SEP)]
        if len(cells) < MIN_CELLS:
            continue
        if not any(cells):
            continue
        groups.setdefault(len(cells), []).append(cells)
    if not groups:
        return []
    # 只保留格子最多的那组：一张卡片里通常是「一张主表 + 若干零散键值」，
    # 全画出来反而看不清主次。
    return groups[max(groups)]


def has_table(text: str) -> bool:
    """这条消息里有没有值得画成图的表格。"""
    return bool(parse_rows(text))


# 列名是 ``中文/字母 名称`` 形态 → ``名称``；``票数: 1票`` → ``票数``。
_LABEL_RE = re.compile(r"^(.{1,12}?)\s*[:：]\s*(.*)$", re.S)


def split_labelled(text: str) -> tuple[list[str], list[list[str]]]:
    """把满屏 ``列名: 值 ｜ 列名: 值`` 的行拆成 (表头, 数据行)。

    与 :func:`parse_rows` 的区别：这个函数**假定每一格都带列名**，返回的
    数据行只有值。``parse_rows`` 是给画图用的通用入口（格数分组），这里是
    给「这张卡到底是不是表格」判定用的。
    """
    head: list[str] = []
    rows: list[list[str]] = []
    for line in (text or "").splitlines():
        cells = [c.strip() for c in line.split(CELL_SEP)]
        if len(cells) < MIN_CELLS:
            continue
        labels, values = [], []
        for c in cells:
            m = _LABEL_RE.match(c)
            if not m:
                labels, values = [], []
                break
            labels.append(m.group(1).strip())
            values.append(m.group(2).strip())
        if not labels or not any(values):
            continue
        if not head:
            head = labels
        elif labels != head:
            # 列名换了 = 另一张表，不混在一起
            continue
        rows.append(values)
    return head, rows


def table_of(text: str) -> tuple[list[str], list[list[str]]]:
    """从消息正文里取出 (表头, 数据行)。没有表格返回 ``([], [])``。"""
    head, rows = split_labelled(text)
    if head and rows:
        return head, rows
    # 兜底：正文里是「裸表格」（没有列名前缀，直接一格一行拼成的行）。
    # 真机上周报的退化形态就是这一种。
    raw = parse_rows(text)
    if len(raw) >= 2:
        return raw[0], raw[1:]
    if raw:
        return [""] * len(raw[0]), raw
    return [], []


def _wrap(draw, text: str, font, limit: int) -> list[str]:
    """按像素宽度折行；中文按字切，英文尽量在空格处切。"""
    text = text or ""
    if not text:
        return [""]
    out, cur = [], ""
    for ch in text:
        probe = cur + ch
        if draw.textlength(probe, font=font) <= limit or not cur:
            cur = probe
        else:
            out.append(cur)
            cur = ch
    if cur:
        out.append(cur)
    return out


def render_table(head: list[str], rows: list[list[str]],
                 *, title: str = "", max_w: int = MAX_W) -> "object | None":
    """画成 PIL Image。没有数据返回 ``None``。

    布局：``title``（可选）→ 表头 → 数据行（斑马纹），每格按需折行。
    列宽按「这一列最长内容 / 表头」的实测像素分配，总宽不够就整体收窄到
    ``MAX_W`` 并把内容折行 —— 不缩放整图，保证数字清晰。
    """
    from PIL import Image, ImageDraw

    ncol = max([len(head)] + [len(r) for r in rows]) if (head or rows) else 0
    if not ncol or not rows:
        return None

    def cell(row, i):
        return row[i] if i < len(row) else ""

    head = [cell(head, i) for i in range(ncol)]
    rows = [[cell(r, i) for i in range(ncol)] for r in rows]

    font = _load_font(FONT_SIZE)
    head_font = _load_font(HEAD_FONT_SIZE)
    title_font = _load_font(TITLE_SIZE)

    # 先按「不折行」量一遍自然宽度，再按比例分配可用宽度。
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    natural = []
    for i in range(ncol):
        w = probe.textlength(head[i], font=head_font)
        for r in rows:
            w = max(w, probe.textlength(r[i], font=font))
        natural.append(max(48, min(int(w) + 2 * PAD_X, 520)))

    avail = max_w - 2 * MARGIN
    total = sum(natural)
    if total > avail:
        scale = avail / total
        widths = [max(60, int(w * scale)) for w in natural]
    else:
        widths = natural
    width = sum(widths) + 2 * MARGIN

    # 折行后算行高
    head_lines = [_wrap(probe, head[i], head_font, widths[i] - 2 * PAD_X)
                  for i in range(ncol)]
    head_h = max(len(x) for x in head_lines) * (HEAD_FONT_SIZE + 8) + 2 * PAD_Y

    body = []
    for r in rows:
        lines = [_wrap(probe, r[i], font, widths[i] - 2 * PAD_X)
                 for i in range(ncol)]
        h = max(len(x) for x in lines) * (FONT_SIZE + 8) + 2 * PAD_Y
        body.append((lines, h))

    title_h = 0
    title_lines = []
    if title:
        title_lines = _wrap(probe, title, title_font, width - 2 * MARGIN)
        title_h = len(title_lines) * (TITLE_SIZE + 10) + 16

    height = MARGIN + title_h + head_h + sum(h for _, h in body) + MARGIN
    img = Image.new("RGB", (width, max(height, 120)), BG)
    draw = ImageDraw.Draw(img)

    y = MARGIN
    for line in title_lines:
        draw.text((MARGIN, y), line, font=title_font, fill=FG)
        y += TITLE_SIZE + 10
    if title_lines:
        y += 16

    # 表头
    draw.rectangle([MARGIN, y, width - MARGIN, y + head_h], fill=HEAD_BG)
    x = MARGIN
    for i in range(ncol):
        for k, line in enumerate(head_lines[i]):
            draw.text((x + PAD_X, y + PAD_Y + k * (HEAD_FONT_SIZE + 8)),
                      line, font=head_font, fill=FG)
        x += widths[i]
    y += head_h

    # 数据行
    for idx, (lines, h) in enumerate(body):
        if idx % 2 == 1:
            draw.rectangle([MARGIN, y, width - MARGIN, y + h], fill=ZEBRA)
        x = MARGIN
        for i in range(ncol):
            for k, line in enumerate(lines[i]):
                draw.text((x + PAD_X, y + PAD_Y + k * (FONT_SIZE + 8)),
                          line, font=font, fill=FG)
            x += widths[i]
        y += h

    # 线框：横线 + 竖线，细，别抢内容
    right, bottom = width - MARGIN, y
    draw.rectangle([MARGIN, bottom - sum(h for _, h in body) - head_h,
                    right, bottom], outline=LINE)
    x = MARGIN
    for w in widths[:-1]:
        x += w
        draw.line([x, bottom - sum(h for _, h in body) - head_h, x, bottom],
                  fill=LINE)
    head_bottom = bottom - sum(h for _, h in body)
    draw.line([MARGIN, head_bottom, right, head_bottom], fill=LINE)
    for idx in range(1, len(body)):
        yy = head_bottom + sum(h for _, h in body[:idx])
        draw.line([MARGIN, yy, right, yy], fill=LINE)
    return img


def save_png(img, out_dir: Path | str, *, key: str = "") -> Path:
    """把图写到 ``out_dir`` 并返回路径。文件名用内容哈希，重发幂等。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5((key or str(id(img))).encode("utf-8")).hexdigest()[:16]
    path = out_dir / f"table-{h}.png"
    img.save(path, "PNG")
    return path


def render_message(text: str, *, out_dir: Path | str, key: str = "",
                   title: str = "") -> "Path | None":
    """正文 → 表格 PNG 路径；这条消息没有表格时返回 ``None``。

    这是给 service 层用的单入口：解析 + 绘制 + 落盘一步到位，出任何异常都
    只记日志返回 ``None``（宁可只发文字，不能因为画图失败把整条消息丢掉）。
    """
    try:
        head, rows = table_of(text)
        if not rows:
            return None
        img = render_table(head, rows, title=title)
        if img is None:
            return None
        return save_png(img, out_dir, key=key or text)
    except Exception:           # noqa: BLE001  —— 画图失败不能影响主流程
        import logging

        logging.getLogger("bridge.table_image").exception(
            "表格转图片失败（只发文字）：%s", (text or "")[:40])
        return None
