"""VLM 兜底识别探针：把一张截图交给多模态模型，看它能不能精确读出
附件文件名 / 表格 / 正文 —— 用于验证 dd-to-wx-master 的「Qwen-VL 读图」
思路是否值得接进桥（2026-11-05）。

设计原则（与 tools/probe_dingtalk_db.py 一致：探针只验证，不进主链路）

· 纯 urllib，不引 openai 依赖 —— 探针工具不加重装负担；
· token 三级来源：--token > MODELSCOPE_ACCESS_TOKEN > DASHSCOPE_API_KEY，
  base_url 跟着 token 类型自动切（ModelScope / 阿里云百炼，两家都是
  OpenAI 兼容协议）；
· prompt 按「裁判」标准写：只报看得见的、文件名逐字符精确、表格转
  markdown —— 不是 dd-to-wx 那个通用群聊 prompt，因为它不校验附件名。

用法
    python tools/probe_vlm.py --image shot.png --token ms-xxxx
    python tools/probe_vlm.py --image shot.png                # 用环境变量
    python tools/probe_vlm.py --image shot.png --model Qwen/QVQ-72B-Preview

隐私提醒：截图（可能含运单号、手机号）会上传到模型服务商，正式接进
桥之前必须过一遍脱敏，见 README「VLM 兜底（试验）」一节。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "Qwen/Qwen2.5-VL-72B-Instruct"
MS_BASE = "https://api-inference.modelscope.cn/v1"
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 与 dd-to-wx-master/qwen-vl.py 的差别：多要 attachments / tables 两个字段，
# 并把「不猜测」从 Constrains 的一句话变成硬性输出规则 —— 文件名错一个
# 字符，下游按名字点行就会抓空，这是 0.58/0.59 两轮事故的根。
PROMPT = """你是消息转发桥的识别裁判。图里是钉钉群聊的一屏（可能含互动卡片）。
只根据图里**实际可见**的内容回答，禁止推测、禁止补全被截断的文字。

输出一个 JSON 对象（前后不得有任何解释或 ```json 标记）：
{
  "chat": "群名，标题栏可见时逐字抄写，否则空串",
  "records": [
    {
      "speaker": "发言人昵称，头像旁可见才填",
      "time": "时间条或气泡上的时刻，如 14:30",
      "content": "正文文字，保持原有换行",
      "tables": ["表格转成 markdown，每张一张；没有表格则为空数组"],
      "attachments": ["附件/文件的文件名，逐字符精确，含扩展名；没有则为空数组"]
    }
  ]
}

规则：
1. attachments 里的文件名必须与图中显示逐字符一致 —— 下游要拿它去点屏幕
   上的行，多一个空格、少一个字都算失败。看不清就写 "?"，不要编。
2. 表格按 markdown 管道表输出，表头用图里的列名；单元格文字逐字抄。
3. 界面上的按钮、图标、水印一律不算 content。"""


def pick_token(args) -> tuple[str, str]:
    """返回 (token, base_url)。base_url 跟 token 前缀自动判断。"""
    if args.token:
        return args.token, args.base_url or MS_BASE
    ms = os.environ.get("MODELSCOPE_ACCESS_TOKEN", "").strip()
    if ms:
        return ms, args.base_url or MS_BASE
    ds = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if ds:
        return ds, args.base_url or DASHSCOPE_BASE
    print("没有 token：--token / MODELSCOPE_ACCESS_TOKEN / DASHSCOPE_API_KEY 任一即可\n"
          "  ModelScope: https://modelscope.cn → 账号 → 访问令牌", file=sys.stderr)
    sys.exit(2)


def call_vl(token: str, base_url: str, model: str, image_path: str,
            timeout: float, prompt: str = PROMPT) -> str:
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    ext = os.path.splitext(image_path)[1].lstrip(".").lower() or "png"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": [{"type": "text", "text":
                                            "只输出 JSON。"}]},
            {"role": "user", "content": [
                {"type": "image_url", "image_url":
                 {"url": f"data:image/{ext};base64,{b64}"}},
                {"type": "text", "text": prompt},
            ]},
        ],
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode())
    return out["choices"][0]["message"]["content"]


def main() -> int:
    ap = argparse.ArgumentParser(description="VLM 读图验证探针")
    ap.add_argument("--image", required=True)
    ap.add_argument("--token", help="ModelScope 或百炼 token")
    ap.add_argument("--base-url", default="", help="覆盖 API 基址")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--raw", action="store_true", help="只打印原始返回")
    ap.add_argument("--ask", help="覆盖默认 prompt（直接问一句话更省事，"
                                  "例如「聊天气泡最后一条是什么」）")
    args = ap.parse_args()

    token, base_url = pick_token(args)
    if not os.path.isfile(args.image):
        print(f"图片不存在: {args.image}", file=sys.stderr)
        return 1
    print(f"模型 {args.model} @ {base_url}")
    print(f"图片 {args.image} "
          f"({os.path.getsize(args.image) / 1024:.0f} KB)，上传中…")
    t0 = time.time()
    try:
        text = call_vl(token, base_url, args.model, args.image, args.timeout,
                       args.ask or PROMPT)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}",
              file=sys.stderr)
        return 1
    dt = time.time() - t0
    if args.raw:
        print(text)
        return 0
    print(f"耗时 {dt:.1f} 秒\n")
    try:
        obj = json.loads(text)
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print("⚠ 返回不是纯 JSON（裁判 prompt 需要再收紧）：\n")
        print(text[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
