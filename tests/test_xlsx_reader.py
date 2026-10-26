"""xlsx_reader 的回归测试。

样本两条路都造：
  · inlineStr —— 真机 2026-10-27 抓回来的 export.xlsx 就是这种（sharedStrings
    是空的，只认 sharedStrings 的实现会把整张表读成全空）；
  · sharedStrings —— 常规导出形态。
"""

import io
import unittest
import zipfile

from bridge.xlsx_reader import XlsxError, read_rows

_HEAD = ["运单号", "履约状态", "小件员编码", "小件员名称"]


def _inline_xlsx(rows: list[list[str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        body = "".join(
            "<row r=\"%d\">%s</row>" % (
                i + 1,
                "".join(
                    "<c r=\"%s%d\" t=\"inlineStr\"><is><t>%s</t></is></c>"
                    % (chr(65 + j), i + 1, v) for j, v in enumerate(row))
            ) for i, row in enumerate(rows))
        z.writestr("xl/worksheets/sheet1.xml",
                   "<?xml version=\"1.0\"?><worksheet xmlns=\"http://schemas"
                   ".openxmlformats.org/spreadsheetml/2006/main\"><sheetData>"
                   + body + "</sheetData></worksheet>")
    return buf.getvalue()


def _shared_xlsx(rows: list[list[str]]) -> bytes:
    buf = io.BytesIO()
    strings: list[str] = []

    def sid(v: str) -> int:
        if v not in strings:
            strings.append(v)
        return strings.index(v)

    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        body = "".join(
            "<row r=\"%d\">%s</row>" % (
                i + 1,
                "".join(
                    "<c r=\"%s%d\" t=\"s\"><v>%d</v></c>"
                    % (chr(65 + j), i + 1, sid(v)) for j, v in enumerate(row))
            ) for i, row in enumerate(rows))
        z.writestr("xl/worksheets/sheet1.xml",
                   "<?xml version=\"1.0\"?><worksheet xmlns=\"http://schemas"
                   ".openxmlformats.org/spreadsheetml/2006/main\"><sheetData>"
                   + body + "</sheetData></worksheet>")
        z.writestr("xl/sharedStrings.xml",
                   "<sst xmlns=\"http://schemas.openxmlformats.org/"
                   "spreadsheetml/2006/main\">"
                   + "".join("<si><t>%s</t></si>" % s for s in strings)
                   + "</sst>")
    return buf.getvalue()


class TestInlineStr(unittest.TestCase):
    """真机那种 inlineStr 存储必须读得出内容（曾经的坑：读成全空）。"""

    def test_read_inline_rows(self):
        p = str(__import__("pathlib").Path(__file__).parent
                / "_tmp_inline.xlsx")
        with open(p, "wb") as f:
            f.write(_inline_xlsx(
                [_HEAD, ["773448088970309", "履约失败", "3113921681", "尹丙辉"],
                 ["772083831526435", "未履约", "3112428847", "朱俞锋"]]))
        try:
            rows = read_rows(p)
        finally:
            import os
            os.unlink(p)
        self.assertEqual(rows[0], _HEAD)
        self.assertEqual(rows[1][3], "尹丙辉")
        self.assertEqual(len(rows), 3)


class TestSharedStrings(unittest.TestCase):
    def test_read_shared_rows(self):
        p = str(__import__("pathlib").Path(__file__).parent
                / "_tmp_shared.xlsx")
        with open(p, "wb") as f:
            f.write(_shared_xlsx([_HEAD, ["770353990502679", "履约失败",
                                          "3112093007", "陶龙"]]))
        try:
            rows = read_rows(p)
        finally:
            import os
            os.unlink(p)
        self.assertEqual(rows[0], _HEAD)
        self.assertEqual(rows[1], ["770353990502679", "履约失败",
                                   "3112093007", "陶龙"])


class TestTrim(unittest.TestCase):
    def test_trims_empty_edges(self):
        p = str(__import__("pathlib").Path(__file__).parent / "_tmp_trim.xlsx")
        with open(p, "wb") as f:
            f.write(_inline_xlsx([[], [], _HEAD, ["1", "2", "3", "4"], []]))
        try:
            rows = read_rows(p)
        finally:
            import os
            os.unlink(p)
        self.assertEqual(rows, [_HEAD, ["1", "2", "3", "4"]])

    def test_bad_file_raises(self):
        p = str(__import__("pathlib").Path(__file__).parent / "_tmp_bad.xlsx")
        with open(p, "wb") as f:
            f.write(b"not a zip")
        try:
            with self.assertRaises(XlsxError):
                read_rows(p)
        finally:
            import os
            os.unlink(p)


if __name__ == "__main__":
    unittest.main()
