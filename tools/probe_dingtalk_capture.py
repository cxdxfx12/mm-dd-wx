"""钉钉截图能力探针（只读）。

验证两件事：
  1. 能否用 UIAutomation 定位钉钉聊天区（消息区）的屏幕矩形；
  2. 能否在**不抢焦点**的前提下把该区域截下来。

对比三种截屏方式：
  - printwindow_full : PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2) —— 可截被遮挡窗口
  - printwindow_basic: PrintWindow(hwnd, hdc, 0)                        —— 旧版兼容
  - grab_screen      : PIL.ImageGrab 抓屏幕像素                        —— 要求窗口可见

用法：
    python tools/probe_dingtalk_capture.py            # 跑三种方式，输出对比
    python tools/probe_dingtalk_capture.py --outdir out/shot
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bridge.config import load_config  # noqa: E402

# ---- Win32 ----
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def _load_uia():
    cfg = load_config()
    wxauto_path = Path(cfg.wechat.wxauto_path)
    if not wxauto_path.is_absolute():
        wxauto_path = (BASE_DIR / wxauto_path).resolve()
    if str(wxauto_path) not in sys.path:
        sys.path.insert(0, str(wxauto_path))
    from wxauto import uiautomation as uia
    return uia


def find_window(uia, timeout: float = 1.0):
    for cls in ("StandardFrame_DingTalk", "DingTalkPCMainWindow"):
        wnd = uia.WindowControl(ClassName=cls, searchDepth=1)
        if wnd.Exists(timeout):
            return wnd, cls
    wnd = uia.WindowControl(Name="钉钉", searchDepth=1)
    if wnd.Exists(timeout):
        return wnd, "Name=钉钉"
    return None, ""


def find_chat_area(wnd, uia):
    """定位聊天区域。返回 (rect_screen, 命中的类名)。"""
    for cls in ("im_chat::DTIMChatBox", "im_chat::ChatBubbleWidget",
                "im_chat::DTIMChatComponent"):
        ctrl = wnd.Control(ClassName=cls, searchDepth=14)
        try:
            if ctrl.Exists(0.5):
                r = ctrl.BoundingRectangle
                if r.width() > 50 and r.height() > 50:
                    return (r.left, r.top, r.right, r.bottom), cls
        except Exception:
            continue
    return None, ""


def print_window(hwnd: int, flags: int):
    """用 PrintWindow 抓整个窗口位图；返回 PIL.Image 或 None。"""
    from PIL import Image

    rect = wt.RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    hwnd_dc = user32.GetWindowDC(wt.HWND(hwnd))
    if not hwnd_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        ok = user32.PrintWindow(wt.HWND(hwnd), mem_dc, flags)
        if not ok:
            return None
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # 负数 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        if got == 0:
            return None
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(wt.HWND(hwnd), hwnd_dc)


def analyse(img) -> str:
    """粗略判断图片是否有效（非纯黑/纯色）。"""
    if img is None:
        return "失败"
    small = img.convert("L").resize((64, 36))
    px = list(small.getdata())
    lo, hi = min(px), max(px)
    uniq = len(set(px))
    verdict = "有内容" if (hi - lo) > 30 and uniq > 8 else "空白/纯色"
    return f"{verdict}  灰度范围={lo}-{hi} 灰阶数={uniq}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=str(BASE_DIR / "tools" / "shots"))
    args = ap.parse_args()

    cfg = load_config()
    uia = _load_uia()

    wnd, how = find_window(uia)
    if wnd is None:
        print("[失败] 未找到钉钉主窗口，请确认客户端已启动并登录")
        return 1

    rect = wnd.BoundingRectangle
    hwnd = wnd.NativeWindowHandle
    print(f"[窗口] 命中方式={how}")
    print(f"       句柄={hwnd}  矩形=({rect.left},{rect.top},{rect.right},{rect.bottom})"
          f"  尺寸={rect.width()}x{rect.height()}")
    try:
        print(f"       IsOffscreen={wnd.IsOffscreen}")
    except Exception:
        pass

    chat_rect, chat_cls = find_chat_area(wnd, uia)
    if chat_rect is None:
        print("[警告] 未定位到聊天区控件，将只测试整窗截图")
    else:
        print(f"[聊天区] {chat_cls}")
        print(f"         屏幕矩形={chat_rect}  尺寸={chat_rect[2]-chat_rect[0]}x{chat_rect[3]-chat_rect[1]}")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    print()

    results = []

    # --- PrintWindow 两种 flag ---
    for label, flag in (("printwindow_full", PW_RENDERFULLCONTENT),
                        ("printwindow_basic", 0)):
        t = time.time()
        try:
            img = print_window(hwnd, flag)
        except Exception as exc:
            results.append((label, "异常", f"{type(exc).__name__}: {exc}", None))
            continue
        cost = time.time() - t
        info = analyse(img)
        path = None
        if img is not None:
            path = outdir / f"dd_{label}.png"
            img.save(path)
        results.append((label, f"{cost:.2f}s", info, path))

    # --- 整窗 PrintWindow 成功后裁剪聊天区 ---
    full = None
    p_full = outdir / "dd_printwindow_full.png"
    if p_full.exists():
        from PIL import Image
        full = Image.open(p_full)
        cw, ch = rect.width(), rect.height()
        if full.size != (cw, ch):
            full = full.resize((cw, ch))
        if chat_rect:
            box = (chat_rect[0] - rect.left, chat_rect[1] - rect.top,
                   chat_rect[2] - rect.left, chat_rect[3] - rect.top)
            box = (max(0, box[0]), max(0, box[1]), min(cw, box[2]), min(ch, box[3]))
            if box[2] > box[0] and box[3] > box[1]:
                crop = full.crop(box)
                cp = outdir / "dd_chatarea_pw.png"
                crop.save(cp)
                results.append(("聊天区裁剪(PW)", "-", analyse(crop), cp))

    # --- 屏幕抓取 ---
    t = time.time()
    try:
        from PIL import ImageGrab
        if chat_rect:
            img = ImageGrab.grab(bbox=chat_rect, all_screens=True)
        else:
            img = ImageGrab.grab(all_screens=True)
        cost = time.time() - t
        path = outdir / "dd_grab_screen.png"
        img.save(path)
        results.append(("grab_screen", f"{cost:.2f}s", analyse(img), path))
    except Exception as exc:
        results.append(("grab_screen", "异常", f"{type(exc).__name__}: {exc}", None))

    print("=" * 78)
    print(f"{'方式':<22} {'耗时':<8} {'判定':<40}")
    print("-" * 78)
    for label, cost, info, path in results:
        print(f"{label:<22} {cost:<8} {info}")
    print("=" * 78)
    print(f"样张目录：{outdir}")

    # --- 对最优结果跑一次 OCR，验证可读性 ---
    best = None
    for pref in ("dd_chatarea_pw.png", "dd_grab_screen.png"):
        p = outdir / pref
        if p.exists():
            from PIL import Image
            im = Image.open(p)
            if "有内容" in analyse(im):
                best = p
                break
    if best is None:
        print("[OCR] 无可读样张，跳过")
        return 0

    print(f"\n[OCR] 对 {best.name} 识别中…")
    try:
        import numpy as np
        from bridge.sources.dingtalk_ocr import _build_ocr_engine
        ocr = _build_ocr_engine()
        print(f"[OCR] 引擎：{ocr.name}")
        from PIL import Image
        t = time.time()
        res = ocr(np.array(Image.open(best).convert("RGB")))
        print(f"[OCR] 耗时 {time.time()-t:.2f}s  文本块 {len(res or [])} 个")
        for r in (res or []):
            box = r[0]
            left = int(min(p[0] for p in box)) if len(box) else 0
            top = int(min(p[1] for p in box)) if len(box) else 0
            print(f"   x={left:<5} y={top:<5} {r[1]}")
    except Exception as exc:
        print(f"[OCR] 失败：{type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
