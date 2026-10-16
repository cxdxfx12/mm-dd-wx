#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WorkBuddy 账号切换助手：盘点本机数据归属 + 一键备份。

背景
----
WorkBuddy 把本机 ``~/.workbuddy`` 下的数据分成三层：

  1) 账号级（按账号 UUID 隔离，换账号后新账号读不到）
       connectors/<uuid>/            连接器授权文件 + 账号里的 MCP 配置(mcp.json)
       plugin-account-state/<uuid>.json
       security/<uuid>/
       <uuid>/                       侧边栏快照 / 启动路由
       memory/<uuid>_memory.md       云端记忆的本地副本（对话里注入的 <memory> 块）
       user-<uuid>-personal/         个人档案 BOOTSTRAP / IDENTITY / SOUL / USER.md

  2) 设备级（跟登录账号无关，换账号照旧可用）
       skills/  binaries/  plugins/  settings.json
       projects/  sessions/  logs/  cache/  blobs/  device-id

  3) 项目级（在工作目录里，彻底与账号无关）
       <项目>/.workbuddy/memory/     项目记忆与日志
       <项目>/config.yaml 等含真实数据的配置

  换账号只会新建一个 UUID 命名空间，旧的不会被删 —— 两个账号数据并存、可随时切回。

用法
----
    python tools/wb_account_switch.py                     # 只盘点，不写任何文件
    python tools/wb_account_switch.py --backup D:/wb-bak  # 备份到指定目录

刻意不做的事：不搬迁 ``connectors/<uuid>/.master.key`` 与连接器授权态。
那些文件是用**该账号**的主密钥加密的，跨账号复制解不开，只会制造一个
「看起来连上了、其实一直 401」的坏状态。换账号后连接器一律重新授权。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def wb_root() -> Path:
    """定位 ~/.workbuddy（Windows 上是 %USERPROFILE%\\.workbuddy）。"""
    env = os.environ.get("WORKBUDDY_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".workbuddy"


def _is_uuid(name: str) -> bool:
    return bool(UUID_RE.match(name))


def scan_accounts(root: Path) -> list[str]:
    """扫出本机出现过的所有账号 UUID（含已退出登录的历史账号）。"""
    found: set[str] = set()
    for parent in (root, root / "connectors", root / "security", root / "plugin-account-state"):
        if not parent.is_dir():
            continue
        for child in parent.iterdir():
            name = child.stem if child.is_file() else child.name
            if _is_uuid(name):
                found.add(name.lower())
    if (root / "memory").is_dir():
        for child in (root / "memory").iterdir():
            m = re.match(r"^([0-9a-f-]{36})_memory\.md(\.bak)?$", child.name, re.I)
            if m:
                found.add(m.group(1).lower())
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and child.name.lower().endswith("-personal"):
                stem = child.name[: -len("-personal")]
                if _is_uuid(stem):
                    found.add(stem.lower())
    return sorted(found)


def _count(path: Path) -> str:
    if not path.exists():
        return "不存在"
    if path.is_file():
        return f"{path.stat().st_size / 1024:.1f} KB"
    n_files = sum(1 for p in path.rglob("*") if p.is_file())
    return f"{n_files} 个文件"


def report(root: Path) -> int:
    """打印盘点结果。返回当前账号数（0 表示没扫到）。"""
    accounts = scan_accounts(root)
    print("=" * 68)
    print("WorkBuddy 本机数据盘点")
    print(f"根目录：{root}")
    print("=" * 68)

    if not accounts:
        print("\n[!] 没扫到任何账号 UUID。若 WorkBuddy 尚未登录过，属正常。")
        return 0

    # 用「顶层 <uuid>/ 目录是否存在」粗略判断谁在登录状态：新版会为当前账号建这个目录
    active = [u for u in accounts if (root / u).is_dir()]
    current = active[-1] if active else None

    print(f"\n共发现 {len(accounts)} 个账号 UUID：")
    for i, u in enumerate(accounts, 1):
        tag = ""
        if u == current:
            tag = "  <== 大概率是当前登录账号"
        print(f"  {i}. {u}{tag}")

    print("\n" + "-" * 68)
    print("【账号级】换账号后新账号读不到（需要搬运或重新配置）")
    print("-" * 68)
    for u in accounts:
        mark = " *当前*" if u == current else ""
        print(f"\n  账号 {u}{mark}")
        items = [
            ("连接器授权 / MCP 配置", root / "connectors" / u),
            ("插件账号态", root / "plugin-account-state" / f"{u}.json"),
            ("安全凭据", root / "security" / u),
            ("侧边栏/启动快照", root / u),
            ("云端记忆本地副本", root / "memory" / f"{u}_memory.md"),
            ("个人档案 personal", root / f"user-{u}-personal"),
        ]
        for label, p in items:
            print(f"      {label:<22} {_count(p)}")

    print("\n" + "-" * 68)
    print("【设备级】跟账号无关，换账号后照旧可用（不用动）")
    print("-" * 68)
    device_items = [
        ("用户级技能", root / "skills"),
        ("运行时 binaries", root / "binaries"),
        ("插件包 plugins", root / "plugins"),
        ("设置 settings.json", root / "settings.json"),
        ("会话历史 projects", root / "projects"),
        ("sessions", root / "sessions"),
        ("设备标识 device-id", root / "device-id"),
    ]
    for label, p in device_items:
        print(f"   {label:<22} {_count(p)}")

    if (root / "mcp.json").exists():
        print(f"   {'MCP 配置 mcp.json':<22} {_count(root / 'mcp.json')}  (设备级，换账号保留)")
    else:
        print(f"   {'MCP 配置 mcp.json':<22} 不存在")

    print("\n" + "-" * 68)
    print("【项目级】在工作目录里，彻底与账号无关")
    print("-" * 68)
    print("   D:/wxauto-main/.workbuddy/memory/          项目记忆与日志")
    print("   D:/wxauto-main/ddwx-bridge/config.yaml    含真实群名 + 机器人 token（勿入库）")
    print("   D:/wxauto-main/ddwx-bridge/data/bridge.db 转发台账")
    print("   D:/wxauto-main/ddwx-bridge/               git 仓库本体（远端 mm-dd-wx）")

    print("\n" + "=" * 68)
    print("结论：项目代码 / 项目记忆 / 真实配置 都不在账号里，换账号不影响继续开发。")
    print("      真正需要在换之前备份的，是上面【账号级】里你在意的几项。")
    print("=" * 68)
    return len(accounts)


def backup(root: Path, dest: Path, project_root: Path) -> int:
    """把「换账号后想带走」的东西复制到 dest。返回复制条目数。"""
    accounts = scan_accounts(root)
    if not accounts:
        print("[!] 没扫到账号 UUID，改成只备份项目级数据。")
    dest = dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    box = dest / f"workbuddy-account-backup-{stamp}"
    box.mkdir(parents=True, exist_ok=True)
    copied = 0

    def take(src: Path, rel: str) -> None:
        nonlocal copied
        if not src.exists():
            return
        target = box / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True)
            else:
                shutil.copy2(src, target)
            copied += 1
            print(f"  [ok] {rel}   <-  {src}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [!!] {rel} 失败：{exc}")

    print(f"\n备份到：{box}\n")

    print("-- 账号级：云端记忆副本 + 个人档案 --")
    for u in accounts:
        take(root / "memory" / f"{u}_memory.md", f"accounts/{u}/memory/{u}_memory.md")
        take(root / f"user-{u}-personal", f"accounts/{u}/personal")

    print("\n-- 账号级：MCP 配置（连接器密钥刻意不搬）--")
    for u in accounts:
        take(root / "connectors" / u / "mcp.json", f"accounts/{u}/connectors-mcp.json")
    take(root / "mcp.json", "device/mcp.json")

    print("\n-- 设备级：自定义技能 + 设置 --")
    take(root / "skills", "device/skills")
    take(root / "settings.json", "device/settings.json")

    print("\n-- 项目级：真实配置与项目记忆 --")
    take(project_root / "config.yaml", "project/config.yaml")
    take(project_root / "data" / "bridge.db", "project/data/bridge.db")
    take(project_root.parent / ".workbuddy" / "memory", "project/workspace-memory")

    print("\n-- 生成 README（换账号后照着做）--")
    readme = box / "README-换账号后必做.md"
    readme.write_text(
        "# 换 WorkBuddy 账号后的收尾清单\n\n"
        f"备份时间：{stamp}\n\n"
        "1. 用新账号登录 WorkBuddy。\n"
        "2. 重新授权连接器：在输入框左下角「+」菜单里开启（企业微信、GitHub 等）。\n"
        "   连接器授权是账号级的，且用各账号自己的主密钥加密，不能直接复制。\n"
        "3. MCP 服务器：把 `device/mcp.json` 或本备份里各账号的 `connectors-mcp.json`\n"
        "   中的 `mcpServers` 合并进 `~/.workbuddy/mcp.json`（该文件是设备级）。\n"
        "   合并后在连接器管理页右上角对新服务器点「信任」才会生效。\n"
        "4. 用户级记忆：把 `accounts/<旧uuid>/memory/*_memory.md` 里有价值的内容\n"
        "   手动写进 `~/.workbuddy/user-<新uuid>-personal/MEMORY.md`。\n"
        "   云端记忆随账号走，新账号不会自动继承旧账号的。\n"
        f"5. 项目记忆无需搬运：直接用 WorkBuddy 打开 `{project_root}`，\n"
        "   `.workbuddy/memory/` 会自动被读到。\n"
        "6. 检查 `git remote -v` 与 SSH 部署密钥是否仍可用（部署密钥跟 WorkBuddy 账号无关，\n"
        "   但若 Git 托管方也换了账号，需要重新添加部署公钥）。\n"
        "7. 首次运行程序前确认 `config.yaml` 里的真实群名与机器人 token 仍在。\n",
        encoding="utf-8",
    )
    copied += 1
    print(f"  [ok] {readme.name}")

    print(f"\n完成：共备份 {copied} 项 -> {box}")
    print("提示：备份包含群名与机器人 token，请勿提交到 git 或上传到公开网盘。")
    return copied


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    here = Path(__file__).resolve()
    default_project = here.parent.parent  # tools/ 的上一级 = ddwx-bridge/

    ap = argparse.ArgumentParser(
        description="WorkBuddy 账号切换助手：盘点本机数据归属，可选备份。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--backup", metavar="DIR", help="备份到指定目录（默认只盘点不写文件）")
    ap.add_argument("--root", metavar="PATH", help="WorkBuddy 数据根目录（默认 ~/.workbuddy）")
    ap.add_argument("--project", metavar="PATH", help=f"项目根目录（默认 {default_project}）")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else wb_root()
    project = Path(args.project).resolve() if args.project else default_project

    if not root.is_dir():
        print(f"[!] 没找到 WorkBuddy 数据目录：{root}")
        print("    如果你把 WorkBuddy 装在别处，请用 --root 指定。")
        return 2

    n = report(root)
    if args.backup:
        backup(root, Path(args.backup), project)
    elif n:
        print("\n要备份的话，加 --backup <目录> 再跑一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
