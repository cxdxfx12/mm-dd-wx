"""在**保留注释**的前提下改写 config.yaml。

为什么不直接 ``yaml.safe_load`` + ``yaml.dump``
    config.yaml 里大量注释是使用说明（每一项在讲「为什么这样设」），
    一旦整体重写就会全部丢失。所以这里做「按行手术」：只替换指定
    key 的值，其余字节原样不动。

支持的能力
    set_scalar(path, section, key, value)   替换标量
    set_list(path, section, key, items)     整体替换一个列表
    backup(path)                            写入前先备份到 data/backup/
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

_SECTION_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*(?:#.*)?$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _section_range(lines: list[str], section: str) -> tuple[int, int] | None:
    """返回顶层 section 的行区间 ``[start, end)``（含 ``section:`` 那一行）。"""
    start = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line.rstrip("\n"))
        if m and _indent(line) == 0:
            if start is not None:          # 上一个顶层段结束
                return start, i
            if m.group(1) == section:
                start = i
    if start is not None:
        return start, len(lines)
    return None


def _key_range(lines: list[str], lo: int, hi: int, key: str) -> tuple[int, int] | None:
    """在 ``[lo, hi)`` 内找 ``key:``，返回它的块区间 ``[k, end)``。

    块的定义：键所在行本身，加上后续「缩进更深」或「列表项」的行。
    同级或更浅缩进的注释/空行都视为**下一个键的开头**，不计入本块。
    """
    key_re = re.compile(r"^(\s*)" + re.escape(key) + r"\s*:(.*)$")
    for i in range(lo + 1, hi):
        raw = lines[i].rstrip("\n")
        m = key_re.match(raw)
        if not m:
            continue
        pad = len(m.group(1))
        end = i + 1
        while end < hi:
            nxt = lines[end].rstrip("\n")
            if not nxt.strip():
                # 空行先记着；若后面还有同块内容则并入，否则不吞掉它
                j = end
                while j < hi and not lines[j].strip():
                    j += 1
                if j < hi:
                    s = lines[j].rstrip("\n")
                    if _indent(s) > pad or (s.lstrip().startswith("-")
                                            and _indent(s) >= pad):
                        end = j
                        continue
                break
            if _indent(nxt) > pad or (nxt.lstrip().startswith("-")
                                      and _indent(nxt) >= pad):
                end += 1
                continue
            break
        return i, end
    return None


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def _write(path: Path, lines: list[str]) -> None:
    text = "".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8", newline="")


def backup(path: Path) -> Path | None:
    """备份当前配置到 data/backup/，返回备份文件路径。"""
    if not path.is_file():
        return None
    out_dir = path.parent / "data" / "backup"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"config-{datetime.now():%Y%m%d-%H%M%S}.yaml"
    try:
        shutil.copy2(path, dst)
    except Exception:
        return None
    return dst


def set_scalar(path: str | Path, section: str, key: str, value) -> bool:
    """替换 ``section`` 下 ``key`` 的标量值，保留行内其它内容与全部注释。"""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    sr = _section_range(lines, section)
    if sr is None:
        return False
    kr = _key_range(lines, sr[0], sr[1], key)
    if kr is None:
        return False
    i = kr[0]
    raw = lines[i]
    tail = "\n" if raw.endswith("\n") else ""
    # ⚠ 组 1 必须把缩进包进去：缩进丢了会让这一行变成顶层键，
    #   下一轮按段查找时区间判断全乱，进而误删后面的配置（踩过）。
    m = re.match(r"^(\s*" + re.escape(key) + r"\s*:)(.*)$", raw.rstrip("\n"))
    if not m:
        return False
    keypart, after = m.group(1), m.group(2)
    comment = ""
    if "#" in after:
        comment = "  " + after[after.index("#"):].strip()
    lines[i] = f"{keypart} {_fmt(value)}{comment}{tail}"
    if _indent(lines[i]) != _indent(raw):      # 兜底：缩进变了就整体放弃
        return False
    # 标量：把原块里多余的行（比如同键的旧列表项）删掉
    for j in range(kr[1] - 1, i, -1):
        del lines[j]
    _write(p, lines)
    return True


def set_list(path: str | Path, section: str, key: str, items: list[str]) -> bool:
    """整体替换一个列表键（写成块列表，便于人工继续编辑）。"""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    sr = _section_range(lines, section)
    if sr is None:
        return False
    kr = _key_range(lines, sr[0], sr[1], key)
    if kr is None:
        return False
    i = kr[0]
    m = re.match(r"^(\s*)" + re.escape(key) + r"\s*:", lines[i].rstrip("\n"))
    if not m:
        return False
    pad = len(m.group(1))
    new: list[str] = [f"{m.group(1)}{key}:" + (" []" if not items else "") + "\n"]
    for it in items:
        new.append(f"{m.group(1)}  - {_fmt(it)}\n")
    lines[i:kr[1]] = new
    _write(p, lines)
    return True


ROUTES_HEADER = (
    "routes:\n"
    "  # ⚠ 本节由界面「设置 → 转发规则」自动生成，手工改动请保持同样的结构。\n"
    "  #   一条规则 = 一个【钉钉群】→ 一个/多个【微信会话】。\n"
    "  #   chat_names: 对来源会话名做**子串**匹配（不区分大小写）。\n"
    "  #   targets   : 微信里的名字，必须与客户端里**完全一致**，否则发不出去。\n"
    "  #   template  : 可用占位符 {company} {sender} {chat} {time} {content} {type}\n"
)


def _yaml_str(s: str) -> str:
    return json.dumps(str(s), ensure_ascii=False)


def set_routes(path: str | Path, rules: list[dict]) -> bool:
    """重建 ``routes`` 段（整段替换）。

    前提：``routes`` 必须是配置文件里**最后一个**顶层段（本项目的配置就是
    这样组织的）。如果后面还有别的顶层段就拒绝写入，避免误删内容。

    ``rules`` 每项：``{"name": str, "sources": [str], "targets": [str],
    "template": str}``，``sources`` 为空表示不限来源。
    """
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    sr = _section_range(lines, "routes")
    if sr is None:
        return False
    start, end = sr
    if end != len(lines):
        # routes 后面还有别的顶层段 —— 不敢整段重写
        tail_top = [l for l in lines[end:] if l.strip() and not l.startswith((" ", "\t", "#"))]
        if tail_top:
            return False

    out = lines[:start]
    out.append(ROUTES_HEADER)
    for r in rules:
        sources = [s for s in (r.get("sources") or []) if str(s).strip()]
        targets = [t for t in (r.get("targets") or []) if str(t).strip()]
        if not targets:
            continue
        name = str(r.get("name") or "").strip() or (
            f"{sources[0] if sources else '全部会话'} → {targets[0]}")
        out.append(f'  - name: {_yaml_str(name)}\n')
        if sources:
            out.append("    chat_names:\n")
            for s in sources:
                out.append(f"      - {_yaml_str(s)}\n")
        else:
            out.append("    chat_names: []\n")
        out.append("    companies: []\n")
        out.append("    msg_types: []\n")
        out.append("    keywords: []\n")
        out.append("    targets:\n")
        for t in targets:
            out.append(f"      - {_yaml_str(t)}\n")
        tpl = str(r.get("template") or "{content}")
        out.append(f"    template: {_yaml_str(tpl)}\n")
    if not out[-1].endswith("\n"):
        out[-1] += "\n"
    _write(p, out)
    return True


def read_rules(path: str | Path) -> list[dict]:
    """读取 routes 段（只解析本项目会写出的那种简单结构，不依赖 PyYAML）。

    返回 ``[{"name":…, "sources":[…], "targets":[…], "template":…}]``。
    """
    p = Path(path)
    if not p.is_file():
        return []
    raw_lines = p.read_text(encoding="utf-8").splitlines()
    sr = _section_range([l + "\n" for l in raw_lines], "routes")
    if sr is None:
        return []
    rules: list[dict] = []
    cur: dict | None = None
    section = ""
    base_indent = None

    for raw in raw_lines[sr[0] + 1:sr[1]]:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        ind = _indent(raw)
        if s.startswith("- "):
            if base_indent is None:
                base_indent = ind
            if ind <= base_indent:                    # 新规则
                if cur:
                    rules.append(cur)
                cur = {"name": "", "sources": [], "targets": [], "template": ""}
                section = ""
                head = s[2:]
                if ":" in head:
                    k, _, v = head.partition(":")
                    _assign(cur, k.strip(), v.strip())
                continue
            _append(cur, section, s[2:])              # 列表项
            continue
        if cur is None or ":" not in s:
            continue
        k, _, v = s.partition(":")
        k, v = k.strip(), v.strip()
        section = k
        if v:
            _assign(cur, k, v)
    if cur:
        rules.append(cur)
    return rules


def _inline_list(v: str) -> list[str] | None:
    """把 ``["a", "b"]`` / ``[a, b]`` 解析成列表；不是行内列表则返回 None。"""
    v = (v or "").strip()
    if not (v.startswith("[") and v.endswith("]")):
        return None
    inner = v[1:-1].strip()
    if not inner:
        return []
    return [_unquote(x) for x in inner.split(",") if x.strip()]


def _assign(rule: dict, key: str, value: str) -> None:
    items = _inline_list(value)
    if key in ("chat_names", "sources"):
        rule["sources"] = items if items is not None else (
            [_unquote(value)] if value else [])
    elif key == "targets":
        rule["targets"] = items if items is not None else (
            [_unquote(value)] if value else [])
    elif key == "name":
        rule["name"] = _unquote(value)
    elif key == "template":
        rule["template"] = _unquote(value)


def _append(rule: dict, section: str, value: str) -> None:
    value = _unquote(value)
    if not value:
        return
    if section in ("chat_names", "sources"):
        rule["sources"].append(value)
    elif section == "targets":
        rule["targets"].append(value)


def _unquote(v: str) -> str:
    v = (v or "").strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except Exception:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1]
    return v


if __name__ == "__main__":       # 自检：改一处再改回来
    import sys
    cfg = Path(__file__).resolve().parent.parent / "config.yaml"
    src = cfg.read_text(encoding="utf-8")

    rules = read_rules(cfg)
    print(f"读出规则 {len(rules)} 条：")
    for r in rules:
        print("   ", r)

    set_scalar(cfg, "wechat", "interval", 3.5)
    set_list(cfg, "dingtalk", "chat_names", ["【数字质控】浙江杭州萧山区大江东公司"])
    set_scalar(cfg, "wechat", "interval", 3.0)
    round1 = cfg.read_text(encoding="utf-8")

    # routes 段重建 → 读回 → 必须与重建前语义一致
    set_routes(cfg, rules)
    back = read_rules(cfg)
    ok_rules = [{"name": r["name"], "sources": r["sources"],
                 "targets": r["targets"], "template": r["template"]}
                for r in rules] == [{"name": r["name"], "sources": r["sources"],
                                     "targets": r["targets"],
                                     "template": r["template"]} for r in back]
    set_routes(cfg, rules)
    now = cfg.read_text(encoding="utf-8")

    if now != src:
        print("警告：往返写入后内容有差异")
        import difflib
        sys.stdout.writelines(difflib.unified_diff(
            src.splitlines(True), now.splitlines(True), "before", "after"))
    elif not ok_rules:
        print("警告：routes 段读回与写入不一致")
    else:
        print("cfgfile 自检通过：标量与 routes 往返写入均无损")
