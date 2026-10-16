"""OCR 引擎/模型对比基准（离线，不依赖钉钉窗口）。

用法：
    python tools/ocr_bench.py tools/shots/dd_area_now.png            # 当前生产引擎(v3)
    python tools/ocr_bench.py <图> --engine new                      # 新版 rapidocr(v4/v5)
    python tools/ocr_bench.py <图> --engine both                     # 两个都跑并对比

预处理与生产路径一致：整图 LANCZOS 放大 OCR_SCALE 倍后送 OCR。
「关键内容」断言：运单号 12 位数字 + 【】标签 + @所有人 —— 这些丢了就是漏转发。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from bridge.sources.dingtalk_ocr import OCR_SCALE  # noqa: E402  生产同款放大倍数

TRIP_ID = re.compile(r"\d{12}")          # 运单号
TAG = re.compile(r"【[^】]{2,12}】")       # 【送货上门】等
AT_ALL = "@所有人"
VOTES = re.compile(r"[（(]\d+票[)）]")    # （1票）（2票）…——「孤立数字」检出度量

# 参数档位。v3 的补丁是 2026-10-16 围绕「表格孤立数字漏检」实测标定的；
# v6 的「等效补丁」按同语义映射到 3.x 的配置键，用于验证是否还需要这组补丁。
PARAM_PROFILES = {
    "default": {},
    "patch": {"Det.limit_side_len": 1536, "Det.box_thresh": 0.35,
              "Det.thresh": 0.25, "Global.text_score": 0.35},
}


def run(engine: str, img_path: Path, profile: str = "patch"
        ) -> tuple[list[str], float]:
    import numpy as np
    from PIL import Image

    img = Image.open(img_path).convert("RGB")
    scaled = img.resize((int(img.width * OCR_SCALE), int(img.height * OCR_SCALE)),
                        Image.LANCZOS)
    arr = np.array(scaled)

    t = time.time()
    if engine == "legacy":
        from rapidocr_onnxruntime import RapidOCR
        if profile == "patch":
            ocr = RapidOCR(det_limit_side_len=1536, det_box_thresh=0.35,
                           det_thresh=0.25, text_score=0.35, det_model_path=None)
        else:
            ocr = RapidOCR()
    else:
        from rapidocr import RapidOCR
        ocr = RapidOCR(params=PARAM_PROFILES.get(profile) or None)
    out = ocr(arr)
    dt = time.time() - t

    # legacy 1.x 返回 (result, elapse) 元组；3.x 返回 RapidOCROutput(.txts/.scores)
    if isinstance(out, tuple) and len(out) == 2 and not isinstance(out[1], str):
        out = out[0]
    if out is None:
        pairs: list[tuple[str, float]] = []
    elif hasattr(out, "txts"):
        txts = getattr(out, "txts", None) or []
        scores = getattr(out, "scores", None) or [1.0] * len(txts)
        pairs = list(zip((str(t).strip() for t in txts),
                         (float(s) for s in scores)))
    else:
        pairs = [(str(item[1]).strip(), float(item[2])) for item in out]
    lines = [f"{conf:.2f}  {text}" for text, conf in pairs if text]
    return lines, dt


def score(lines: list[str]) -> dict:
    body = "\n".join(lines)
    ids = set(TRIP_ID.findall(body))
    tags = set(TAG.findall(body))
    return {"lines": len(lines), "trip_ids": len(ids), "tags": len(tags),
            "votes": len(set(VOTES.findall(body))),
            "at_all": AT_ALL in body}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path, nargs="?",
                    default=BASE_DIR / "tools" / "shots" / "dd_area_now.png")
    ap.add_argument("--engine", choices=["legacy", "new", "both"], default="legacy")
    ap.add_argument("--profile", choices=["default", "patch"], default="patch",
                    help="default=该引擎出厂参数；patch=v3 时代的补丁参数(或其 v6 等效)")
    ap.add_argument("--quiet", action="store_true", help="只打汇总，不打逐行")
    args = ap.parse_args()

    engines = ["legacy", "new"] if args.engine == "both" else [args.engine]
    results: dict[str, tuple[list[str], float]] = {}
    for eng in engines:
        label = (f"{'v3' if eng == 'legacy' else 'v6'} / {args.profile}")
        try:
            results[eng] = run(eng, args.image, args.profile)
        except ImportError as exc:
            print(f"[跳过] {label}：{exc}")
            continue

    for eng, (lines, dt) in results.items():
        s = score(lines)
        label = "当前生产 v3" if eng == "legacy" else "新版 v6"
        print("=" * 72)
        print(f"[{label} / {args.profile}]  耗时 {dt:.2f}s  行数 {s['lines']}"
              f"  运单号 {s['trip_ids']} 个  【】标签 {s['tags']} 个"
              f"  票数标记 {s['votes']} 个"
              f"  @所有人 {'有' if s['at_all'] else '无'}")
        print("=" * 72)
        if not args.quiet:
            for ln in lines:
                print(f"  {ln}")

    if len(results) == 2:
        a, b = (results["legacy"], results["new"])
        sa, sb = score(a[0]), score(b[0])
        print("=" * 72)
        print(f"[对比] 行数 {sa['lines']}→{sb['lines']}   "
              f"运单号 {sa['trip_ids']}→{sb['trip_ids']}   "
              f"标签 {sa['tags']}→{sb['tags']}   "
              f"耗时 {a[1]:.2f}s→{b[1]:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
