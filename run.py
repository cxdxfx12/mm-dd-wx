"""入口脚本。

常用命令：
    python run.py --simulate                # 用样例消息跑通全链路（不需要微信）
    python run.py --check                   # 启动自检
    python run.py --live                    # 正式运行：Stream 长连接采集 → 真实投递微信
    python run.py --stats                   # 查看统计
    python run.py --export out.csv          # 导出消息
    python run.py --notify-test             # 发一条钉钉通知，验证 webhook 配对
    python run.py --notify-report           # 把当前统计发到钉钉群
    python run.py --notify-say "交班了"      # 往钉钉群发自定义内容
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Windows 控制台默认可能是 GBK，中文会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bridge.config import load_config                      # noqa: E402
from bridge.models import Message                          # noqa: E402
from bridge.service import BridgeService                   # noqa: E402


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def sample_messages() -> list[Message]:
    """构造样例消息，覆盖：命中规则、被过滤、重复投递三种情况。

    会话名与真实目标一致（【数字质控】…），保证 --simulate 能验证真实路由规则。
    """
    now = datetime.now()
    base = dict(chat_id="cid_demo", chat_name="【数字质控】浙江杭州萧山区大江东公司",
                source="dingtalk")
    other = dict(chat_id="cid_other", chat_name="其他无关群", source="dingtalk")
    return [
        Message(**base, sender_id="u1001", sender_name="张三", company="",
                content="今日订单 1200 单，已全部发出。", msg_type="text",
                msg_time=now, raw_id="demo-001"),
        Message(**base, sender_id="u1002", sender_name="李四", company="",
                content="明早 8 点开始卸货，请提前安排月台。", msg_type="text",
                msg_time=now + timedelta(minutes=3), raw_id="demo-002"),
        Message(**other, sender_id="u1003", sender_name="王五", company="",
                content="这条不该被转发，用来验证规则过滤。", msg_type="text",
                msg_time=now + timedelta(minutes=5), raw_id="demo-003"),
        Message(**base, sender_id="u1001", sender_name="张三", company="",
                content="今日订单 1200 单，已全部发出。", msg_type="text",
                msg_time=now + timedelta(minutes=8), raw_id="demo-001"),  # 重复
    ]


def run_interactive(svc, cfg) -> int:
    """交互测试：手动输入消息，实时观察过滤、格式化与投递结果。"""
    chat = (cfg.dingtalk.chat_names or ["测试会话"])[0]
    company = (cfg.dingtalk.companies or [""])[0]
    print("=" * 52)
    print("交互测试模式")
    print("=" * 52)
    print(f"  默认会话：{chat}")
    print(f"  默认公司：{company or '(未配置，路由可能不命中)'}")
    print("  输入格式：发送者|消息内容   （不写发送者则用「测试用户」）")
    print("  输入 :q 退出")
    print("-" * 52)
    seq = 0
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in (":q", ":quit", "exit"):
            break
        sender, _, content = line.partition("|")
        if not content:
            sender, content = "测试用户", line
        seq += 1
        svc.handle(Message(
            source="dingtalk", chat_id="interactive", chat_name=chat,
            sender_id=f"interactive-{seq}", sender_name=sender.strip(),
            company=company, content=content.strip(), msg_type="text",
            msg_time=datetime.now(), raw_id=f"interactive-{seq}",
        ))
    print("-" * 52)
    print(svc.summary())
    return 0


MENU_ITEMS: list[tuple[str, str, str, list[str]]] = [
    ("1", "模拟测试", "样例消息跑通全链路（不需要微信 / 钉钉）", ["run.py", "--simulate"]),
    ("2", "交互测试", "手动输入消息，实时看过滤与转发效果", ["run.py", "--interactive"]),
    ("3", "钉钉OCR预览", "截一帧看钉钉群消息识别效果（只读）", ["tools/test_dingtalk_ocr.py"]),
    ("4", "转发一条", "抓一条钉钉消息，预览后确认发送到微信群", ["tools/forward_one.py"]),
    ("5", "转发最近N条", "手动转最近 N 条，预览后确认发送", ["tools/forward_recent.py"]),
    ("6", "启动自检", "检查配置、微信连接与钉钉窗口", ["run.py", "--check"]),
    ("7", "正式运行", "采集钉钉消息并投递微信（Ctrl+C 停止）", ["run.py", "--live"]),
    ("8", "能力探针", "检测钉钉凭证、SDK 与本机客户端", ["probe_dingtalk.py"]),
    ("9", "查看统计", "累计入库与投递情况", ["run.py", "--stats"]),
    ("10", "导出消息", "把已入库消息导出为 CSV", ["run.py", "--export"]),
    ("11", "通知测试", "往钉钉群发一条测试消息，验证 webhook", ["run.py", "--notify-test"]),
    ("12", "发运行报告", "把累计统计发到钉钉群", ["run.py", "--notify-report"]),
]


def _disp_width(text: str) -> int:
    """按终端显示宽度计算：CJK 字符占 2 列。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(1, width - _disp_width(text))


def run_menu(cfg_path: str) -> int:
    """交互式菜单。

    每个动作都通过子进程调用自身，避免长驻服务与菜单状态互相污染，
    也让 Ctrl+C 能干净地只中断当前动作。
    """
    python = sys.executable
    while True:
        print()
        print("=" * 64)
        print("           钉钉 → 微信   消息转发桥")
        print("=" * 64)
        for key, title, desc, _ in MENU_ITEMS:
            print(f"   [{key}]  {_pad(title, 14)}{desc}")
        print(f"   [0]  {_pad('退出', 14)}关闭本程序")
        print("=" * 64)

        try:
            choice = input("   请输入序号并回车: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice in ("", "0", "q", "quit", "exit"):
            return 0

        hit = next((item for item in MENU_ITEMS if item[0] == choice), None)
        if hit is None:
            print(f"   「{choice}」不是有效序号，请重新输入。")
            continue

        _, title, _, argv_tail = hit
        argv = [python, str(BASE_DIR / argv_tail[0]), *argv_tail[1:]]
        if title == "导出消息":
            try:
                name = input("   导出文件名（回车默认 messages.csv）: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            argv.append(name or "messages.csv")
        elif title == "转发最近N条":
            # 用 --ask 让「预览 → 确认 → 发送」在同一个进程里完成：
            # 分两次跑会在两次抓帧之间把新来的消息混进来。
            try:
                n = input("   转发最近几条（回车默认 3）: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            argv += ["-n", n or "3", "--ask"]
        argv += ["--config", cfg_path]

        print("-" * 64)
        try:
            if title == "转发一条":
                # 先只读预览，让用户看清将要发出去的内容，再决定是否真实发送
                subprocess.call(argv, cwd=str(BASE_DIR))
                try:
                    ans = input("   确认发送到微信群？输入 yes 发送，其他任意键取消: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 0
                if ans.lower() in ("yes", "y", "是", "确认", "1"):
                    subprocess.call(argv + ["--yes"], cwd=str(BASE_DIR))
                else:
                    print("   已取消，未发送任何消息。")
            else:
                subprocess.call(argv, cwd=str(BASE_DIR))
        except KeyboardInterrupt:
            print("\n   已中断当前动作。")
        print("-" * 64)

        try:
            input("   按回车返回菜单...")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="钉钉 → 微信消息转发桥")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"), help="配置文件路径")
    parser.add_argument("--menu", action="store_true", help="交互式菜单（start.bat 调用）")
    parser.add_argument("--simulate", action="store_true", help="用样例消息跑通链路")
    parser.add_argument("--interactive", action="store_true", help="交互测试：手动输入消息看效果")
    parser.add_argument("--check", action="store_true", help="启动自检")
    parser.add_argument("--live", action="store_true", help="正式运行（采集真实消息）")
    parser.add_argument("--dry-run", action="store_true", help="强制 dry-run，不真实发送")
    parser.add_argument("--stats", action="store_true", help="打印统计")
    parser.add_argument("--export", metavar="CSV", help="导出消息到 CSV")
    parser.add_argument("--notify-test", action="store_true",
                        help="给钉钉机器人发一条测试消息")
    parser.add_argument("--notify-report", action="store_true",
                        help="把累计统计发到钉钉群")
    parser.add_argument("--notify-say", metavar="文本",
                        help="把自定义内容发到钉钉群（想发什么就写什么）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args()

    setup_logging(args.verbose)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"配置加载失败：{exc}")
        print(f"提示：可复制 config.example.yaml 为 {Path(args.config).name} 后修改")
        return 2

    if args.menu:
        return run_menu(args.config)

    if args.export:
        svc = BridgeService(cfg)
        path = svc.storage.export_csv(args.export)
        print(f"已导出到：{path}")
        svc.close()
        return 0

    if args.stats:
        svc = BridgeService(cfg)
        print(svc.storage.stats())
        svc.close()
        return 0

    if args.notify_test or args.notify_report or args.notify_say:
        # 空内容属于「参数写错了」，先说清楚，别让它落到后面被一串
        # errcode 排查提示盖住 —— 那会让人以为是 webhook 配错了。
        if args.notify_say is not None and not args.notify_say.strip():
            print("消息内容是空的。--notify-say 后面要跟上要发送的文字，例如：")
            print('  python run.py --notify-say="今天设备检修，下午 2 点恢复"')
            return 1
        # 刻意不构造 BridgeService：通知测试不该依赖微信/钉钉环境，
        # 换台没装微信的机器也应该能验证 webhook 是否配对。
        from bridge.notify import DingTalkNotifier, mask_webhook
        nt = DingTalkNotifier.from_config(cfg.notify)
        if nt is None:
            if not getattr(cfg.notify, "enabled", False):
                print("钉钉通知未启用。请到「设置中心 → 钉钉通知」里开启，"
                      "或把 config.yaml 的 notify.enabled 改成 true。")
            else:
                print("钉钉通知已开启，但没有填 webhook，无法发送。")
                print("请把自定义机器人的 webhook 地址填到 notify.webhooks"
                      "（一个机器人一项）或 notify.webhook；"
                      "也可以用环境变量 DINGTALK_ROBOT_WEBHOOK 下发。")
            return 1
        for i, rb in enumerate(nt.robots, 1):
            print(f"webhook {i}/{len(nt.robots)}：{mask_webhook(rb.url)}"
                  + ("（已加签）" if rb.secret else ""))
        print(f"通道状态：{nt.describe()}")
        if args.notify_test:
            ok, detail = nt.test()
        elif args.notify_say:
            ok, detail = nt.send_custom(args.notify_say)
        else:
            from bridge.storage import Storage
            st = Storage(cfg.resolve(cfg.storage.db_path))
            try:
                s = st.stats()
            finally:
                st.close()
            d = s.get("deliveries") or {}
            ok, detail = nt.send_text(
                f"📊 运行报告（手动查询）\n"
                f"累计入库消息：{s['total_messages']} 条\n"
                f"累计投递成功：{int(d.get('ok', 0))} | 失败：{int(d.get('fail', 0))}\n"
                f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
        if ok:
            print("已发送，请到钉钉群里查看。")
            return 0
        print(f"发送失败：{detail}")
        print()
        print("排查提示：")
        print("  errcode=310000 = 机器人的「安全设置」不匹配：")
        print("    开了「自定义关键词」→ 把关键词填到 notify.keyword")
        print("    开了「加签」        → 把 SEC 开头的密钥填到 notify.secret")
        print("  网络类错误         → 多半是本机出网受限，或被 IP 白名单拦下")
        return 1

    if args.dry_run:
        cfg.wechat.dry_run = True

    sim_db: Path | None = None
    if args.simulate:
        # 模拟测试必须完全无副作用：
        #   1) 强制 dry-run，避免用户关掉 dry_run 后样例消息被真的发进微信群
        #   2) 用一次性临时库，避免污染真实数据、也保证每次结果可复现
        cfg.wechat.dry_run = True
        sim_db = Path(tempfile.gettempdir()) / f"ddwx_simulate_{os.getpid()}.db"
        sim_db.unlink(missing_ok=True)
        cfg.storage.db_path = str(sim_db)

    svc = BridgeService(cfg)
    try:
        if args.simulate:
            print("=" * 52)
            print("模拟投递（临时库 + dry-run，不碰真实数据）")
            print("=" * 52)
            for msg in sample_messages():
                svc.handle(msg)
            print("-" * 52)
            print(svc.summary())
            print("-" * 52)
            print("预期结果：收到 3 | 重复 1 | 未命中规则 1 | 已发送 2 | 累计入库 3")
            return 0

        if args.check:
            problems = svc.preflight()
            ok_t, detail_t = svc.last_target_check
            print(f"  投递端   : {svc.target.name}（{type(svc.target).__name__}）")
            print(f"  微信连接 : {'通过' if ok_t else '未通过'} — {detail_t}")
            from bridge.sources.factory import build_source
            try:
                src = build_source(cfg)
                ok, detail = src.check()
                print(f"  采集端   : {'通过' if ok else '未通过'} — {detail}")
                if not ok:
                    problems.append(f"采集端不可用：{detail}")
            except Exception as exc:
                problems.append(f"采集端构建失败：{type(exc).__name__}: {exc}")
            print(f"  dry-run  : {cfg.wechat.dry_run}")
            print(f"  钉钉通知 : {svc.notify_status()}")
            print(f"  转发目标 : {cfg.routes[0].targets if cfg.routes else []}")
            if problems:
                print()
                for p in problems:
                    print(f"[问题] {p}")
                return 1
            print("\n自检通过")
            return 0

        if args.interactive:
            return run_interactive(svc, cfg)

        if args.live:
            problems = svc.preflight()
            from bridge.sources.factory import build_source
            source = build_source(cfg)
            ok, detail = source.check()
            print(f"  采集端自检：{'通过' if ok else '未通过'} — {detail}")
            if not ok:
                problems.append(f"采集端不可用：{detail}")
            if problems:
                for p in problems:
                    print(f"[问题] {p}")
                return 1
            print(f"启动采集：{source}")
            if svc.notifier:
                print(f"  钉钉通知：{svc.notify_status()}")
            # 通知与巡检只在正式运行时挂上：自检/预览这类短命令没必要打扰群
            svc.start_watchdog()
            svc.announce_start(repr(source))
            try:
                # 第二个回调是附件转发：手机抓回的 Excel → 表格图 → 微信。
                # 第三个回调是持久去重（storage.is_duplicate）：基线打捞
                # 靠它判断「这条入库过没」。只有安卓网关源收三个参数
                # （OCR 等旧源只有一个），按签名分发，别让别的采集源在这里
                # 炸 TypeError。
                import inspect

                params = len(inspect.signature(source.run).parameters)
                if params >= 3:
                    source.run(svc.handle, svc.handle_attachment,
                               svc.storage.is_duplicate)
                elif params >= 2:
                    source.run(svc.handle, svc.handle_attachment)
                else:
                    source.run(svc.handle)
            finally:
                svc.announce_stop()
            return 0

        parser.print_help()
        return 0
    except KeyboardInterrupt:
        print("\n已停止")
        return 0
    finally:
        svc.close()
        if sim_db is not None:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{sim_db}{suffix}").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
