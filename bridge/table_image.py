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

# ── 超宽表格切块（2026-10-18 用户报「横的表格比较宽」）──────────────
# 列数一多，硬塞进一份宽度只能把每列压到几十像素，一行字竖折成两三个字一个
# —— 跟竖排对联一样没法读。切块后每块独占全宽，每格都能横排。
MAX_COL_W = 520         # 单列再宽也不超过这个（超了折行）
MIN_COL_W = 60          # 单列最窄；比这窄就不该出现在图里了
# 切块后**每块重复**的键列数：前 N 列（通常是「网点」「单号」这类标识列）。
# 重复是对的 —— 第 2 块单独截出来也要能看懂是哪条数据。
KEY_COLS = 2

# 配色（浅色，微信里不论深浅主题都看得清 —— 图片本身自带白底）
FG = (32, 32, 32)
FG_DIM = (96, 96, 96)
BG = (255, 255, 255)
HEAD_BG = (240, 243, 248)
ZEBRA = (250, 251, 253)
LINE = (216, 220, 226)
SEP_BG = (238, 240, 244)   # 多块表之间的分隔色带

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

    **对外入口**。列数多到一份宽度装不下时，自动切成几块**纵向摞起来**
    （每块独占全宽、重复键列），见 :func:`render_table_blocks` 的说明。
    """
    if not head and not rows:
        return None
    ncol = max([len(head)] + [len(r) for r in rows]) if (head or rows) else 0
    if not ncol or not rows:
        return None

    groups = _split_columns(head, rows, ncol, max_w)
    if len(groups) <= 1:
        return _render_block(head, rows, all_cols=list(range(ncol)),
                             title=title, max_w=max_w, part=(0, 1))

    # 多块：每块自带表头（+ 重复键列），纵向拼接成一张长图。
    imgs = []
    for idx, cols in enumerate(groups):
        imgs.append(_render_block(head, rows, all_cols=cols, title=title,
                                  max_w=max_w, part=(idx, len(groups))))
    return _stack_vertical(imgs)


def _split_columns(head: list[str], rows: list[list[str]], ncol: int,
                   max_w: int) -> list[list[int]]:
    """把列切成若干组，每组单独画一块就能保证「每格横排不竖折」。

    ── 为什么是纵向摞而不是左右并排 ──────────────────────────────
    2026-10-18 用户选了「横向并排」，但算过账才发现那是反的：12 列切成 3 块
    左右并排，总宽还是 1200px，每块只分到 ~400px 装 5~6 列 = 每列 ~70px，
    **比不切（每列 ~100px）更挤**，竖折反而更严重。左右并排只在「每块列很少」
    时才有意义，而那时根本不需要切。所以这里改成**每块独占全宽、上下摞**：
    3 列能分到 400px/列，文字轻松横排。

    切法：按「自然宽度」累加，超过一份可用宽度就开新块。第一块带上
    :data:`KEY_COLS` 列的键列，后续块重复这些键列（用户选的「重复键列」），
    这样每一块单独截出来也能看懂是哪条数据。

    返回 ``[[列下标...], ...]``；只有一组时分块逻辑会被跳过。
    """
    probe_draw, font, head_font = _probe()
    natural = []
    for i in range(ncol):
        w = probe_draw.textlength(head[i] if i < len(head) else "", font=head_font)
        for r in rows:
            if i < len(r):
                w = max(w, probe_draw.textlength(r[i], font=font))
        natural.append(max(MIN_COL_W, min(int(w) + 2 * PAD_X, MAX_COL_W)))

    keys = list(range(min(KEY_COLS, ncol)))
    key_w = sum(natural[i] for i in keys)

    avail = max_w - 2 * MARGIN
    groups: list[list[int]] = []
    cur: list[int] = list(keys)
    cur_w = key_w
    for i in range(len(keys), ncol):
        w = natural[i]
        # 装满就换块（首块也要留够，否则第一块只有键列，一列数据都没有）
        if cur != keys and cur_w + w > avail:
            groups.append(cur)
            cur = list(keys)
            cur_w = key_w
        cur.append(i)
        cur_w += w
    if cur != keys or not groups:
        groups.append(cur)

    # 只有一列数据 + 键列时不必切（切了反而多一张几乎一样的表）
    if len(groups) <= 1 or ncol <= len(keys) + 1:
        return [list(range(ncol))]
    return groups


def _probe():
    """量文字宽度用的 (ImageDraw, 正文字体, 表头字体)。"""
    from PIL import Image, ImageDraw

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    return probe, _load_font(FONT_SIZE), _load_font(HEAD_FONT_SIZE)


def _stack_vertical(imgs: list) -> "object | None":
    """把几块表上下摞成一张图（间隔一道分隔带，一眼看出是切开的）。"""
    from PIL import Image, ImageDraw

    imgs = [i for i in imgs if i is not None]
    if not imgs:
        return None
    width = max(i.size[0] for i in imgs)
    gap = 18
    height = sum(i.size[1] for i in imgs) + gap * (len(imgs) - 1)
    out = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(out)
    y = 0
    for img in imgs:
        out.paste(img, (0, y))
        y += img.size[1]
        if y < height:
            # 分隔带：一条浅色横条 + 居中「续下表」提示会更醒目，但图里加字
            # 容易和表格内容混淆，这里只画色带，靠留白区分。
            draw.rectangle([0, y + 2, width, y + gap - 2], fill=SEP_BG)
            y += gap
    return out


def _render_block(head: list[str], rows: list[list[str]],
                  *, all_cols: list[int], title: str = "",
                  max_w: int = MAX_W, part: tuple[int, int] = (0, 1)
                  ) -> "object | None":
    """画**一块**表：只取 ``all_cols`` 指定的列。

    布局：``title``（可选）→ 表头 → 数据行（斑马纹），每格按需折行。
    列宽按「这一列最长内容 / 表头」的实测像素分配，总宽不够就整体收窄到
    ``max_w`` 并把内容折行 —— 不缩放整图，保证数字清晰。

    ``part=(序号, 总数)`` 在有多块时用来在标题后加「（第 2/3 部分）」，
    免得看图的人以为表格只有这些列。
    """
    from PIL import Image, ImageDraw

    ncol = max([len(head)] + [len(r) for r in rows]) if (head or rows) else 0
    if not ncol or not rows:
        return None

    def cell(row, i):
        return row[i] if i < len(row) else ""

    # 只保留本块要画的列；顺序按 ``all_cols`` 给（键列在前）
    head = [cell(head, i) for i in all_cols]
    rows = [[cell(r, i) for i in all_cols] for r in rows]
    ncol = len(all_cols)

    idx, total = part
    if total > 1:
        suffix = f"（第 {idx + 1}/{total} 部分）"
        title = f"{title}{suffix}" if title else suffix.strip("（）")

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
        natural.append(max(MIN_COL_W, min(int(w) + 2 * PAD_X, MAX_COL_W)))

    avail = max_w - 2 * MARGIN
    total_w = sum(natural)
    if total_w > avail:
        scale = avail / total_w
        widths = [max(MIN_COL_W, int(w * scale)) for w in natural]
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
