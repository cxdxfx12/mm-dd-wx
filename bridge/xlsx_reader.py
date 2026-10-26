"""读 xlsx 附件 —— **只用标准库**，不引 openpyxl。

## 为什么自己写

附件转发的解析对象是钉钉机器人卡片导出的 Excel（真机 2026-10-27 实测
``export.xlsx``：表头「运单号/履约状态/小件员编码/小件员名称」+ 明细行）。
这类文件形状非常规整（单表、首行表头、纯文本），用 openpyxl 引一个依赖
（还会被塞进安装包 payload）去换一份「反正用不到的兼容性」不划算。

## xlsx 的两种字符串存储（都要认）

真机抓回来的那份用 **inlineStr**（字符串内联在单元格里，``sharedStrings``
是空的）——网上大多数教程只演示 sharedStrings 那种，照抄会把整张表读成
全空。所以这里两种都实现：

    <c r="A1" t="inlineStr"><is><t>运单号</t></is></c>   ← 真机这种
    <c r="A1" t="s"><v>0</v></c>                        ← 共享字符串下标

数字单元（没有 t 属性）取 ``<v>`` 的原样文本，不做格式化 —— 这些导出表
本来就是文本，别自作聪明转浮点再丢精度。
"""
from __future__ import annotations

import zipfile
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class XlsxError(Exception):
    """xlsx 读不了（不是 zip / 没有 sheet / 结构坏）。"""


def read_rows(path) -> list[list[str]]:
    """读第一个工作表，返回二维字符串表格（首行通常是表头）。

    会把**两端的全空行**和**尾部全空列**裁掉：导出表经常带空边，留着
    只会让渲染出的图片多出无意义的空白。中间的空行保留（有分隔语义）。
    解析不出任何行时抛 :class:`XlsxError`。
    """
    try:
        with zipfile.ZipFile(path) as z:
            shared = _shared_strings(z)
            sheet = _first_sheet(z)
            rows = _sheet_rows(sheet, shared)
    except XlsxError:
        raise
    except Exception as exc:                      # noqa: BLE001
        raise XlsxError(f"xlsx 打不开：{exc}") from exc
    rows = _trim_empty(rows)
    if not rows:
        raise XlsxError("xlsx 里没有数据行")
    return rows


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    out = []
    for si in root.findall(f"{NS}si"):
        # rich text 的 si 里可能有多个 <r><t>，全部拼起来
        out.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
    return out


def _first_sheet(z: zipfile.ZipFile) -> ET.Element:
    """取第一个工作表。先按 workbook 的表顺序找，找不到就按文件名兜底。"""
    names = z.namelist()
    target = ""
    if "xl/workbook.xml" in names:
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            rid = ""
            for sheet in wb.iter(f"{NS}sheet"):
                rid = sheet.get(f"{_REL_NS}id") or ""
                break
            if rid:
                rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
                for rel in rels:
                    if rel.get("Id") == rid:
                        tgt = (rel.get("Target") or "").lstrip("/")
                        if tgt and not tgt.startswith("xl/"):
                            tgt = "xl/" + tgt
                        target = tgt
        except Exception:                          # noqa: BLE001
            target = ""                            # 结构怪就走文件名兜底
    if not target:
        cands = sorted(n for n in names
                       if n.startswith("xl/worksheets/") and n.endswith(".xml"))
        if not cands:
            raise XlsxError("xlsx 里找不到工作表")
        target = cands[0]
    if target not in names:
        raise XlsxError(f"工作表 {target} 不在包里")
    return ET.fromstring(z.read(target))


def _col_index(ref: str) -> int:
    """"A1" → 0，"AB12" → 27。没有字母前缀就返回 0。"""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return max(0, n - 1)


def _sheet_rows(sheet: ET.Element, shared: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in sheet.iter(f"{NS}row"):
        cells: dict[int, str] = {}
        for c in row.findall(f"{NS}c"):
            ref = c.get("r") or ""
            idx = _col_index(ref) if ref else len(cells)
            t = c.get("t")
            if t == "inlineStr":
                is_el = c.find(f"{NS}is")
                text = ("".join(tt.text or "" for tt in is_el.iter(f"{NS}t"))
                        if is_el is not None else "")
            elif t == "s":
                v = c.find(f"{NS}v")
                try:
                    text = shared[int(v.text)] if (v is not None and v.text) else ""
                except (ValueError, IndexError):
                    text = ""
            else:
                v = c.find(f"{NS}v")
                text = (v.text or "") if v is not None else ""
            cells[idx] = (text or "").strip()
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
        else:
            rows.append([])
    return rows


def _trim_empty(rows: list[list[str]]) -> list[list[str]]:
    """裁掉两端全空行与尾部全空列。中间空行保留。"""
    def empty_row(r: list[str]) -> bool:
        return not any(r)

    while rows and empty_row(rows[0]):
        rows.pop(0)
    while rows and empty_row(rows[-1]):
        rows.pop()
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    for r in rows:
        r.extend([""] * (width - len(r)))
    while width > 0 and all(len(r) < width or not r[width - 1] for r in rows):
        width -= 1
    return [r[:width] for r in rows]
