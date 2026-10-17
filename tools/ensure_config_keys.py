# -*- coding: utf-8 -*-
"""把样板里新增的配置键补进用户的 config.yaml。

## 为什么需要它

安装包升级时只保证「顶层段存在」（`cfgfile.ensure_section`），**段内新增的键不会补**。
老用户的 config.yaml 是旧版本生成的，于是新版本读到的 `dingtalk.gateway_token` 等键
根本不存在 —— 表现为工作进程一启动就退出：

    采集端自检：未通过 — 安卓网关没配 token（dingtalk.gateway_token）

2026-10-17 真实发生过：装完新版后配置里缺 `gateway_* / android_*` 五个键。

## 用法

    python tools/ensure_config_keys.py                       # 补安装目录（自动探测）
    python tools/ensure_config_keys.py --target 某路径\\config.yaml
    python tools/ensure_config_keys.py --from 开发目录\\config.yaml
        # 值从另一份已配好的配置里取真实值，而不是样板占位符
        # （gateway_token 这类必须两边一致，用 --from 最省事）

默认**只补缺失的键**，已有值的键一律不动（用户改过的设置不会被覆盖）。
每次改动前都会备份到 `data/backup/`。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import cfgfile  # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("缺 pyyaml：pip install pyyaml")

DEFAULT_INSTALL = Path(r"C:\Program Files\喵喵钉钉-微信消息转发桥")


def guess_target() -> Path | None:
    for cand in (DEFAULT_INSTALL, Path.home() / "AppData/Local/Programs/喵喵钉钉-微信消息转发桥"):
        if (cand / "config.yaml").is_file():
            return cand / "config.yaml"
    return None


def scalar_keys(cfg: dict) -> list[tuple[str, str, object]]:
    """摊平成 (段, 键, 值)，只处理两层结构 —— 与 config.yaml 的实际形状一致。"""
    out = []
    for section, body in cfg.items():
        if not isinstance(body, dict):
            continue
        for key, value in body.items():
            if isinstance(value, (dict, list)):
                continue          # 路由规则、列表等结构交给 ensure_list_key / set_routes
            out.append((section, str(key), value))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐用户配置里缺失的键")
    ap.add_argument("--target", type=Path, help="要补的 config.yaml")
    ap.add_argument("--from", dest="ref", type=Path, default=None,
                    help="从这里取真实值（默认用本仓库的 config.example.yaml）")
    args = ap.parse_args()

    target = args.target or guess_target()
    if not target or not target.is_file():
        return print("找不到目标 config.yaml，请用 --target 指定") or 1
    ref_path = args.ref or (ROOT / "config.example.yaml")
    if not ref_path.is_file():
        return print(f"找不到参考配置：{ref_path}") or 1

    print(f"目标配置：{target}")
    print(f"取值来源：{ref_path}{'（真实值）' if args.ref else '（样板默认值）'}")

    tgt = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    ref = yaml.safe_load(ref_path.read_text(encoding="utf-8")) or {}

    missing = []
    for section, key, value in scalar_keys(ref):
        have = (tgt.get(section) or {}).get(key, "\0") if isinstance(tgt.get(section), dict) else "\0"
        if have == "\0":
            missing.append((section, key, value))

    if not missing:
        print("\n没有缺失的键，无需改动。")
        return 0

    print(f"\n缺 {len(missing)} 个键，开始补：")
    bak = cfgfile.backup(target)
    if bak:
        print(f"  已备份：{bak}")

    failed = 0
    for section, key, value in missing:
        try:
            ok = cfgfile.set_or_insert_scalar(target, section, key, value)
        except PermissionError:
            print("  写不进去 —— 目标在 Program Files 下，请用管理员身份运行本工具")
            return 1
        mark = "OK" if ok else "失败"
        if not ok:
            failed += 1
        shown = value if not isinstance(value, str) or len(value) < 30 else value[:27] + "…"
        print(f"  {section}.{key} = {shown!r}  {mark}")

    # 回读确认
    after = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    still = [f"{s}.{k}" for s, k, _ in missing
             if (after.get(s) or {}).get(k, "\0") == "\0"]
    print()
    if still:
        print("仍未写入：" + ", ".join(still))
        return 1
    print("全部补齐。**重启程序才会生效。**")
    if not args.ref and any(k == "gateway_token" for _, k, _ in missing):
        print("\n⚠ 刚补的 gateway_token 是空值/占位符。手机端必须填同一个值，"
              "请用设置中心填写，或用 --from 指向一份已配好的配置。")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
