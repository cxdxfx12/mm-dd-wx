"""手动转发最近 N 条消息到微信群（默认只预览）。

与自动转发只差一件事：**条数由人指定**。消息来源两选一：

    screen（默认）  抓钉钉当前画面一帧，取最下面（最新）的 N 条
    db             从已入库记录里取最近 N 条 —— 用来补发漏掉或失败的

用法：

    python tools/forward_recent.py                         # 预览最近 3 条
    python tools/forward_recent.py -n 5                    # 预览最近 5 条
    python tools/forward_recent.py -n 5 --ask              # 预览后就地确认发送（推荐）
    python tools/forward_recent.py -n 5 --yes              # 跳过确认，直接真实转发
    python tools/forward_recent.py --from db --only-failed --ask   # 补发失败的
    python tools/forward_recent.py --to "某某群" --ask       # 换目标会话
    python tools/forward_recent.py --plain --ask           # 不加【手动补发】提示行

安全设计（四条，缺一都可能发错东西）：

  1. **默认只预览**：不碰微信、不写数据库，只打印「将要发出去的内容」；
     预览连微信进程都不启动，因此秒出结果。
  2. **`--ask` 就地确认**：在**同一条命令**里预览→确认→发送。命令行菜单用它，
     避免「预览一次、发送又一次」两次抓帧之间来了新消息，发出去的不是刚确认的那几条。
  3. **标题护栏**（screen 模式）：沿用自动转发那一套 —— 当前钉钉窗口不是
     配置里的目标会话时直接中止。手动转发没有「新消息」这层天然保护，
     窗口切错群就会把别的群的对话原样发出去。
  4. **真实发送走 `BridgeService.handle(force=True)`**：去重被跳过（人工点了
     发送就是要再发一次，返回「重复，跳过」等于什么都没干），但入库与投递
     记录照记 —— 补发过的事实会留在库里，便于事后对账。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bridge.config import load_config                          # noqa: E402
from bridge.formatter import DEFAULT_TEMPLATE, render          # noqa: E402
from bridge.models import Message                              # noqa: E402
from bridge.router import Router                               # noqa: E402
from bridge.storage import Storage, row_to_message             # noqa: E402

TEST_BANNER = "【手动补发】本条由人工触发转发，可忽略。"
MAX_COUNT = 50
CLIP_TOP_PX = 8          # 单元上沿离视口顶部小于这个像素数 → 判定为被截断
LINE = "-" * 64


def units_to_messages(units: list[dict], cfg, chat_name: str) -> list[Message]:
    """把屏幕上的消息单元转成 Message，保持**发生顺序**（旧 → 新）。

    指纹与 `DingTalkOCRSource._emit` 用同一套（`unit_fp` + 序号），
    这样同一条消息在手动/自动两条路径下拿到的 `uid` 是一致的，
    投递记录不会分叉成两条互不相干的消息。
    """
    from bridge.sources.base import infer_company
    from bridge.sources.dingtalk_ocr import parse_clock, unit_fp

    now = datetime.now()
    seqs: Counter = Counter()
    out: list[Message] = []
    for u in units:
        fp = unit_fp(u)
        seqs[fp] += 1
        hm = parse_clock(u.get("time") or "")
        when = (now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
                if hm else now)
        sender = "(我)" if u.get("is_self") else (u.get("sender") or "未知")
        out.append(Message(
            source="dingtalk", chat_id="", chat_name=chat_name,
            sender_id="", sender_name=sender,
            company=infer_company(cfg.dingtalk.companies, chat_name, sender,
                                  u["content"]),
            content=u["content"], msg_type="text", msg_time=when,
            raw_id=f"ocr:{fp}:{seqs[fp]}",
        ))
    return out


def pick_from_screen(cfg, count: int) -> tuple[list[Message], str] | tuple[None, str]:
    """抓一帧，取画面最下面（最新）的 N 条。返回 ``(msgs, chat_name)`` 或 ``(None, 原因)``。"""
    from bridge.sources.dingtalk_ocr import grab_frame_units, is_cross_day

    units, info = grab_frame_units(cfg)
    if info["title"]:
        print(f"[标题条] {info['title']!r}")
        print(f"[护栏] 标题校验：{'通过' if info['passed'] else '不通过'}"
              f"（相似度 {info['ratio']:.2f}，命中 {info['matched']!r}）")
    else:
        print(f"[失败] {info['why']}")
        return None, info["why"]

    if units is None:
        return None, info["why"]
    if not units:
        return None, "当前画面没有解析出任何消息（整屏可能都是图片/表情）"
    if len(units) < count:
        print(f"[提示] 画面上只解析出 {len(units)} 条消息，将全部转发。")

    # 会话名与自动路径保持一致：优先用护栏命中的那个**配置名**
    # （设置中心存下来的常是片段，如「数字质控」），路由规则按它匹配。
    chat_name = info["matched"] or (cfg.dingtalk.chat_names or [""])[0]
    picked = units[-count:]

    # 两个「内容可能不对」的真实情况，都只提示不拦截 —— 用户看着预览里的
    # 正文自己判断，替他把消息悄悄丢掉会更难解释。
    if picked and picked[0]["y0"] < CLIP_TOP_PX:
        print(f"[提示] 最上面那条紧贴视口上沿（y={picked[0]['y0']}px），"
              f"很可能是被截断的半截消息，请核对正文。")
    old = [u for u in picked if is_cross_day(u["time"])]
    if old:
        print(f"[提示] 其中有 {len(old)} 条是往日消息（被钉钉标了日期），"
              f"自动转发会丢掉它们，本次按你的指定照发。")

    return units_to_messages(picked, cfg, chat_name), chat_name


def pick_from_db(cfg, count: int, only_failed: bool,
                 chat: str | None) -> tuple[list[Message], str] | tuple[None, str]:
    """从已入库记录取最近 N 条（按发生顺序返回）。"""
    st = Storage(cfg.resolve(cfg.storage.db_path))
    try:
        rows = st.recent(count, chat_name=chat or None, only_failed=only_failed)
    finally:
        st.close()
    if not rows:
        tail = "最近一次投递失败的" if only_failed else "已入库"
        return None, f"数据库里没有符合条件（{tail}）的消息 —— 是不是还没跑起来过？"
    return [row_to_message(r) for r in rows], ""


def main() -> int:
    ap = argparse.ArgumentParser(
        description="手动转发最近 N 条消息到微信群（默认只预览）")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--count", "-n", type=int, default=3, metavar="N",
                    help=f"转发最近几条（默认 3，允许 1~{MAX_COUNT}）")
    ap.add_argument("--from", dest="source", choices=("screen", "db"),
                    default="screen",
                    help="screen=抓钉钉当前画面（默认）；db=从已入库记录取")
    ap.add_argument("--only-failed", action="store_true",
                    help="仅 --from db 时有效：只取「最近一次投递失败」的消息")
    ap.add_argument("--chat", metavar="NAME",
                    help="仅 --from db 时有效：限定来源会话名（默认不限）")
    ap.add_argument("--to", metavar="NAME", help="覆盖目标会话（不改配置文件）")
    ap.add_argument("--plain", action="store_true", help="不加【手动补发】提示行")
    ap.add_argument("--ask", action="store_true",
                    help="在同一条命令里预览→确认→发送（推荐：避免两次抓帧之间消息变动）")
    ap.add_argument("--yes", action="store_true",
                    help="确认真实发送（不加则只预览，不碰微信、不写库）")
    args = ap.parse_args()

    cfg = load_config(args.config)

    count = max(1, min(MAX_COUNT, args.count))
    if count != args.count:
        print(f"[提示] 条数已收敛到 {count}（允许范围 1~{MAX_COUNT}）")
    only_failed = bool(args.only_failed)
    if only_failed and args.source != "db":
        print("[提示] --only-failed 只在 --from db 时有效，本次已忽略。")
        only_failed = False

    if args.to:
        # 覆盖成单一目标：其它一切（模板、限流、记录）保持原样
        for r in cfg.routes:
            r.targets = [args.to]

    # ---------------- 取消息 ----------------
    print(LINE)
    if args.source == "db":
        print(f"来源：数据库 · 最近 {count} 条"
              + ("（只看最近一次投递失败的）" if only_failed else "")
              + (f" · 会话={args.chat}" if args.chat else ""))
        picked, why = pick_from_db(cfg, count, only_failed, args.chat)
    else:
        print(f"来源：钉钉当前画面 · 最近 {count} 条")
        picked, why = pick_from_screen(cfg, count)

    if picked is None:
        print(f"[中止] {why}")
        return 1

    msgs: list[Message] = picked
    router = Router(cfg.routes)

    # ---------------- 逐条渲染 + 预告 ----------------
    plan: list[tuple[Message, list[str], str]] = []
    for m in msgs:
        matched = router.match(m)
        body = render(m, matched.template or DEFAULT_TEMPLATE)
        if not args.plain:
            body = f"{TEST_BANNER}\n{body}"
        plan.append((m, matched.targets, body))

    targets = sorted({t for _, ts, _ in plan for t in ts})
    print(f"目标：{'、'.join(targets) if targets else '（没有命中任何规则，不会发送）'}")
    print(LINE)

    skipped = 0
    for i, (m, ts, body) in enumerate(plan, 1):
        head = m.msg_time.strftime("%H:%M") if m.msg_time else "--:--"
        print(f"[{i}/{len(plan)}] {head}  {m.sender_name}")
        if not ts:
            skipped += 1
            print("      （未命中转发规则 → 不会发送）")
        else:
            for line in body.splitlines():
                print(f"      {line}")
        if i != len(plan):
            print(LINE)

    print(LINE)
    if skipped:
        print(f"共 {len(plan)} 条，其中 {skipped} 条未命中规则")
    if not targets:
        print("没有任何消息命中转发规则，无需发送。")
        print("提示：检查 config.yaml 的 routes[].chat_names / companies 是否过窄。")
        return 1

    # ---------------- 预览止步 / --ask 就地确认 ----------------
    if not args.yes:
        if not args.ask:
            print("[预览模式] 未发送任何内容，也没有写数据库。")
            print("确认无误后加 --yes 真实转发：")
            tail = f" --from {args.source}" if args.source != "screen" else ""
            if only_failed:
                tail += " --only-failed"
            print(f"    python tools/forward_recent.py -n {len(plan)}{tail} --yes")
            return 0
        # --ask 的价值就在于**不重新抓帧**：上面打印的这一份就是要发的那一份。
        # 若改成「先 --yes 预览、再 --yes 发送」，两次抓帧之间来了新消息，
        # 发出去的就未必是刚确认过的那几条。
        print("[待确认] 以上就是要发送的全部内容（不会再重新抓帧）。")
        try:
            ans = input("确认发送到微信群？输入 yes 发送，其他任意键取消: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("[已取消] 未发送任何内容。")
            return 0
        if ans.lower() not in ("yes", "y", "是", "确认", "1"):
            print("[已取消] 未发送任何内容。")
            return 0
        print()

    # ---------------- 真实发送 ----------------
    if cfg.wechat.dry_run:
        print("[提示] 配置里 wechat.dry_run=true，本次手动发送已按你的确认改为真实发送。")
    cfg.wechat.dry_run = False

    from bridge.service import BridgeService

    svc = BridgeService(cfg)
    try:
        problems = svc.preflight()
        if problems:
            # 一条都不发，而不是发一半 —— 半途失败会让人分不清哪些已发出
            for p in problems:
                print(f"[问题] {p}")
            return 1

        print(f"开始发送 {len(plan)} 条到「{'、'.join(targets)}」…")
        print(LINE)
        sent = failed = filtered = 0
        for i, (m, _ts, _body) in enumerate(plan, 1):
            s0, f0, x0 = (svc.counter["sent"], svc.counter["failed"],
                          svc.counter["filtered"])
            svc.handle(m, force=True)
            d_sent = svc.counter["sent"] - s0
            d_fail = svc.counter["failed"] - f0
            d_filt = svc.counter["filtered"] - x0
            head = f"[{i}/{len(plan)}] {m.sender_name}"
            if d_sent:
                sent += d_sent
                print(f"{head} → 已发送")
            elif d_fail:
                failed += d_fail
                print(f"{head} → 发送失败（详情见日志）")
            elif d_filt:
                filtered += d_filt
                print(f"{head} → 未命中规则，跳过")
            else:
                print(f"{head} → 未发送（原因不明，请查看日志）")

        print(LINE)
        print(f"完成：成功 {sent} | 失败 {failed} | 未命中 {filtered}")
        if failed:
            print("失败排查：微信是否登录 / 群名与客户端是否一致 / 窗口是否被最小化")
            print("补发：python tools/forward_recent.py --from db --only-failed --yes")
        return 1 if failed else 0
    finally:
        svc.close()


if __name__ == "__main__":
    raise SystemExit(main())
