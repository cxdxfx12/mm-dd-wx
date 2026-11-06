"""Windows Graphics Capture（WGC）窗口抓帧。

为什么要有这个模块
    2026-11-04 一整天都在救同一类事故：抓到的画面不是微信。
      * 微信被 WorkBuddy / WPS 表格盖住 → ImageGrab 按矩形截屏，截到的是
        遮挡者，OCR 只读出「耳(E)」「×、采集端」这类碎片，相似度 0.09；
      * 进程 DPI 未声明 → 矩形坐标被虚拟化，整块画面偏移 ~322px；
      * 窗口被拖到屏幕右边缘外 → 截到黑块。
    这些都不是「补丁没打对」，而是**「截屏幕」这个动作本身**带来的：
    它抓的是屏幕上那块矩形区域的像素，屏幕上是什么就抓什么。
    WGC 是 Win10 1809+ 的系统 API，直接从 DWM 合成器拿**该窗口自己的
    渲染表面** —— 被遮挡、被拖出屏幕、别的程序全屏都不影响，也不需要
    `SetWindowPos(TOPMOST)` 把微信顶起来（那会让用户眼前闪一下）。

实现要点
    * 按 hwnd 常驻一个采集会话，帧由库自己的线程回调过来；取帧直接读
      最新一帧的副本，比每次重建会话快一个量级。
    * 帧是**客户区**尺寸，而下游所有坐标都按「窗口矩形内像素」标定
      （ImageGrab(bbox=窗口矩形) 的坐标系）。所以这里把帧贴回窗口矩形
      大小的画布上，偏移量用 ``GetClientRect`` + ``ClientToScreen`` 现算。
      不做这步，点击会整体偏几个像素到十几像素。
    * 任何异常都返回 ``None``，由调用方回落到原来的 ImageGrab 路径 ——
      这个模块坏了不能让投递跟着坏。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes as wt

log = logging.getLogger(__name__)

user32 = ctypes.windll.user32

# hwnd -> _Session
_sessions: dict[int, "_Session"] = {}
_lock = threading.Lock()

# 库不可用（没装 / 平台不支持）时置 True，之后不再反复尝试
_unavailable = False


def available() -> bool:
    """WGC 是否可用（只做一次导入探测）。"""
    global _unavailable
    if _unavailable:
        return False
    try:
        import windows_capture  # noqa: F401
        return True
    except Exception as exc:                            # noqa: BLE001
        log.info("WGC 不可用（%s），抓帧回落到 ImageGrab", exc)
        _unavailable = True
        return False


class _Session:
    """一个窗口的常驻采集会话。"""

    # 拷贝节流：WGC 在内容变化时会按显示器刷新率推帧（最高 60fps），
    # 每帧都 copy 一份 872×1326 的 RGB 是纯浪费（~3.5MB/帧）。
    # 投递流程一秒最多取几次画面，120ms 完全够；帧旧最多 120ms，
    # 而窗口内容没变时新旧帧本来就是一回事。
    # ⚠ 不能用库自己的 ``minimum_update_interval``：Win10 的 Graphics
    #   Capture API 不支持该参数，传了直接抛
    #   «Setting a minimum update interval is not supported» —— 整个
    #   会话起不来，抓帧全回落 ImageGrab（2026-11-04 踩过）。
    MIN_COPY_INTERVAL = 0.12

    def __init__(self, hwnd: int):
        self.hwnd = int(hwnd)
        self.frame = None                 # np.ndarray，RGB
        self.ts = 0.0                     # 帧到达时间（monotonic）
        self.err: BaseException | None = None
        self.closed = False
        self.control = None
        self.event = threading.Event()
        self._last_copy = 0.0
        self._start()

    def _start(self) -> None:
        from windows_capture import WindowsCapture

        cap = WindowsCapture(
            cursor_capture=False,
            draw_border=None,             # 这个平台不支持画边框，显式 None
            window_hwnd=self.hwnd,        # 用句柄，别用标题（标题会变）
        )

        @cap.event
        def on_frame_arrived(frame, capture_control):   # noqa: ANN001
            try:
                now = time.monotonic()
                if now - self._last_copy < self.MIN_COPY_INTERVAL:
                    return                            # 节流：这帧丢掉
                # frame_buffer 是 BGRA 的 numpy 视图，库回调返回后可能被复用，
                # 必须 copy；顺便丢掉 alpha 并翻成 RGB。
                arr = frame.frame_buffer
                self.frame = arr[:, :, 2::-1].copy()
                self.ts = now
                self._last_copy = now
                self.event.set()
            except Exception as exc:                    # noqa: BLE001
                self.err = exc

        @cap.event
        def on_closed():                                # noqa: ANN001
            self.closed = True
            self.err = self.err or RuntimeError("采集会话被关闭")

        self.control = cap.start_free_threaded()

    def stop(self) -> None:
        try:
            if self.control is not None:
                self.control.stop()
        except Exception:                               # noqa: BLE001
            pass

    def latest(self, timeout: float = 1.2, max_age: float = 3.0):
        """取最新一帧（RGB ndarray）。

        ``max_age`` 内的帧直接返回：窗口内容没变时 WGC 不会推新帧，
        旧帧的内容和「新帧」本来就是一回事，等下去只是白等。
        """
        now = time.monotonic()
        if self.frame is not None and now - self.ts <= max_age:
            return self.frame
        # 没有帧（刚建会话）/帧太旧：等它推一帧
        deadline = now + timeout
        while time.monotonic() < deadline:
            if self.err is not None and self.frame is None:
                raise self.err
            if self.event.wait(0.05):
                self.event.clear()
                if self.frame is not None:
                    return self.frame
        if self.frame is not None:
            return self.frame
        raise TimeoutError("WGC 迟迟没有推来第一帧")


def _pad_to_window(arr, hwnd: int):
    """把客户区帧贴到「窗口矩形」大小的画布上，坐标系与 ImageGrab 对齐。"""
    from PIL import Image

    r = wt.RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
        return None
    win_w, win_h = r.right - r.left, r.bottom - r.top
    if win_w <= 0 or win_h <= 0:
        return None

    cr = wt.RECT()
    if not user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(cr)):
        return None
    cli_w, cli_h = cr.right - cr.left, cr.bottom - cr.top

    pt = wt.POINT(0, 0)
    if not user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(pt)):
        return None
    ox, oy = pt.x - r.left, pt.y - r.top

    img = Image.fromarray(arr, "RGB")
    if img.size != (cli_w, cli_h) and cli_w > 0 and cli_h > 0:
        # 窗口刚改过尺寸时 WGC 可能还推着旧帧；归一到当前客户区，
        # 保证下面的 ox/oy 偏移有意义。正常情况下（进程声明了
        # Per-Monitor V2）帧与客户区同为物理像素，这一步是 1:1。
        ratio = img.size[0] / cli_w if cli_w else 1.0
        if not 0.95 <= ratio <= 1.05:
            log.warning(
                "WGC 帧 %s 与客户区 %dx%d 差 %.2f× —— 检查进程是否声明了 "
                "Per-Monitor V2，以及微信是否跨了两块不同缩放的屏幕",
                img.size, cli_w, cli_h, ratio)
        img = img.resize((cli_w, cli_h))
    if ox == 0 and oy == 0 and img.size == (win_w, win_h):
        return img
    canvas = Image.new("RGB", (win_w, win_h), (0, 0, 0))
    # 别把负偏移钳成 0：某些无边框/带阴影窗口的客户区会伸到窗口矩形
    # 左上之外，PIL.paste 自己会裁掉越界部分。钳 0 反而让画面整体右移。
    canvas.paste(img, (ox, oy))
    return canvas


def grab(hwnd: int, timeout: float = 1.2):
    """抓该窗口自己的画面，返回 PIL.Image；失败返回 None（调用方回落）。"""
    if not available():
        return None
    sess = None
    try:
        with _lock:
            sess = _sessions.get(int(hwnd))
            if sess is None or sess.closed or sess.err is not None:
                if sess is not None:
                    sess.stop()
                sess = _Session(int(hwnd))
                _sessions[int(hwnd)] = sess
        arr = sess.latest(timeout=timeout)
        return _pad_to_window(arr, int(hwnd))
    except Exception as exc:                            # noqa: BLE001
        log.info("WGC 抓帧失败（%s）—— 本次回落 ImageGrab", exc)
        close(hwnd)
        return None


def close(hwnd: int) -> None:
    with _lock:
        sess = _sessions.pop(int(hwnd), None)
    if sess is not None:
        sess.stop()


def close_all() -> None:
    with _lock:
        items = list(_sessions.values())
        _sessions.clear()
    for s in items:
        s.stop()
