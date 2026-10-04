"""窗口几何与只读探测：把 Chromium 自绘的标题条挪出屏幕，形成真无边框窗口。

Chromium 的 --app 窗口把自己的标题条画在 Win32 客户区内部（实测 31px），清掉
WS_CAPTION 这类样式位并不会让它消失；只有进入全屏它才不画。所以窗口模式下我们
把窗口顶边放到屏幕外（top = 内容顶边 - 标题条高度），让那条标题条落在屏幕之外，
页面内容正好从屏幕上的目标位置开始铺满 —— 移动与缩放由界面自己画的手势经 CDP 驱动。

这里只做几何换算与只读探测；真正改窗口状态（normal / minimized / fullscreen）走 CDP，
因为只有 Chromium 自己切换时它的窗口状态机才不会把我们的位置改回去。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_VISIBLE = 0x10000000
MONITOR_DEFAULTTONEAREST = 2
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020

_ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
_user32 = None


def available() -> bool:
    return _api() is not None


def _api():
    global _user32
    if _user32 is None:
        try:
            _user32 = ctypes.windll.user32
        except AttributeError:      # 非 Windows
            return None
    return _user32


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _Rect),
                ("rcWork", _Rect), ("dwFlags", wintypes.DWORD)]


def is_window(hwnd: int) -> bool:
    user32 = _api()
    if user32 is None or not hwnd:
        return False
    return bool(user32.IsWindow(ctypes.c_void_p(hwnd)))


def is_iconic(hwnd: int) -> bool:
    user32 = _api()
    if user32 is None or not hwnd:
        return False
    return bool(user32.IsIconic(ctypes.c_void_p(hwnd)))


def work_area(hwnd: int) -> tuple[int, int, int, int]:
    """窗口所在显示器去掉任务栏后的可用区域。"""
    user32 = _api()
    if user32 is None or not hwnd:
        return (0, 0, user32.GetSystemMetrics(0) if user32 else 1280,
                user32.GetSystemMetrics(1) if user32 else 720)
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    handle = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), MONITOR_DEFAULTTONEAREST)
    if not handle or not user32.GetMonitorInfoW(ctypes.c_void_p(handle), ctypes.byref(info)):
        return monitor_rect(hwnd)
    r = info.rcWork
    return (r.left, r.top, r.right, r.bottom)


def set_rect(hwnd: int, x: int, y: int, width: int, height: int) -> bool:
    """把窗口挪到指定位置。Chromium 不会拦这种回到屏幕内的正常移动。"""
    user32 = _api()
    if user32 is None or not hwnd:
        return False
    user32.ShowWindow(ctypes.c_void_p(hwnd), 9)          # SW_RESTORE：先退出最大化/最小化
    return bool(user32.SetWindowPos(ctypes.c_void_p(hwnd), 0, int(x), int(y), int(width), int(height),
                                    SWP_NOZORDER | SWP_FRAMECHANGED))


def mostly_on_screen(rect: tuple[int, int, int, int], area: tuple[int, int, int, int]) -> bool:
    """窗口是否基本落在可用区域内；被记到屏幕外（旧档里常见）时要拉回来。"""
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if w <= 0 or h <= 0:
        return False
    left, top = max(rect[0], area[0]), max(rect[1], area[1])
    right, bottom = min(rect[2], area[2]), min(rect[3], area[3])
    visible = max(0, right - left) * max(0, bottom - top)
    return visible >= w * h * 0.6


def find_window(title: str, owner_pid: int | None = None, prefixes=()) -> int:
    """按窗口标题找应用窗口；先要精确同名，再退到前缀同名。

    Chromium 在页面给出标题之前会先把 URL 当窗口标题，所以调用方可以把「我们自己的地址」
    作为前缀一起给进来，避免启动头几秒认不出窗口。
    """
    user32 = _api()
    if user32 is None:
        return 0
    exact = []
    prefix = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        # 先按进程过滤：GetWindowText 会向别的进程的窗口发消息，
        # 万一有个卡死的同名进程（例如上一次自检留下的），这里就会被拖住。
        if owner_pid:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != int(owner_pid):
                return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        value = buffer.value
        if value == title:
            exact.append(hwnd)
            return False
        if value.startswith(title):
            prefix.append(hwnd)
        elif any(value.startswith(item) for item in prefixes):
            prefix.append(hwnd)
        return True

    user32.EnumWindows(_ENUM_PROC(callback), 0)
    for pool in (exact, prefix):
        if pool:
            return pool[0]
    return 0


def _style(hwnd: int) -> int:
    user32 = _api()
    if user32 is None or not hwnd:
        return 0
    return user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE) & 0xFFFFFFFF


def has_caption(hwnd: int) -> bool:
    return bool(_style(hwnd) & WS_CAPTION)


def is_visible(hwnd: int) -> bool:
    return bool(_style(hwnd) & WS_VISIBLE)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    user32 = _api()
    if user32 is None or not hwnd:
        return (0, 0, 0, 0)
    rect = _Rect()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    return (rect.left, rect.top, rect.right, rect.bottom)


def client_size(hwnd: int) -> tuple[int, int]:
    """客户区尺寸（页面真正能用的那块），用于核对视口是否铺满窗口。"""
    user32 = _api()
    if user32 is None or not hwnd:
        return (0, 0)
    rect = _Rect()
    user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    return (rect.right, rect.bottom)


def monitor_rect(hwnd: int) -> tuple[int, int, int, int]:
    """窗口所在显示器的完整区域（含任务栏），用于判断是否真全屏。"""
    user32 = _api()
    if user32 is None or not hwnd:
        return (0, 0, 0, 0)
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    handle = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), MONITOR_DEFAULTTONEAREST)
    if not handle or not user32.GetMonitorInfoW(ctypes.c_void_p(handle), ctypes.byref(info)):
        return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    r = info.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def client_origin(hwnd: int) -> tuple[int, int]:
    """客户区左上角的屏幕坐标：Chromium 的标题条就画在这块区域的最上面。"""
    user32 = _api()
    if user32 is None or not hwnd:
        return (0, 0)
    point = wintypes.POINT(0, 0)
    user32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(point))
    return (point.x, point.y)


def frame_insets(hwnd: int) -> dict:
    """窗口矩形与客户区之间的四边留白（实测：左右各 8、顶部 0、底部 8）。"""
    wr = window_rect(hwnd)
    cw, ch = client_size(hwnd)
    cx, cy = client_origin(hwnd)
    return {"left": cx - wr[0], "top": cy - wr[1],
            "right": max(0, wr[2] - cx - cw), "bottom": max(0, wr[3] - cy - ch)}


def set_bounds(hwnd: int, left: int, top: int, width: int, height: int) -> bool:
    """按 (左,上,宽,高) 摆放窗口，不动最小化状态。CDP 不可用时的兜底通路（实测同样有效）。"""
    user32 = _api()
    if user32 is None or not hwnd:
        return False
    return bool(user32.SetWindowPos(ctypes.c_void_p(hwnd), 0, int(left), int(top), int(width), int(height),
                                    SWP_NOZORDER))


def screen_pixel(x: int, y: int) -> tuple[int, int, int]:
    """读屏幕上某一个点的颜色（GDI，不依赖第三方库）。用于像素级证明标题条已出屏。"""
    user32 = _api()
    if user32 is None:
        return (-1, -1, -1)
    hdc = user32.GetDC(0)
    try:
        value = ctypes.windll.gdi32.GetPixel(ctypes.c_void_p(hdc), int(x), int(y))
    finally:
        user32.ReleaseDC(0, ctypes.c_void_p(hdc))
    if value == -1 or value == 0xFFFFFFFF:      # CLR_INVALID / 全白透传
        return (-1, -1, -1)
    return (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


# -- 纯几何换算：内容矩形 <-> 窗口矩形 ------------------------------------
def content_to_window(content, insets, strip):
    """把「页面内容想占的屏幕矩形」换算成 Chromium 的窗口矩形。

    strip 是 Chromium 自绘标题条的高度：窗口顶边要往上挪这么多，让它落到屏幕外。
    """
    x, y, w, h = content
    return (x - insets["left"], y - insets["top"] - strip,
            w + insets["left"] + insets["right"], h + insets["top"] + insets["bottom"] + strip)


def rect_to_bounds(rect):
    """GetWindowRect 的 (左,上,右,下) -> CDP 用的 (左,上,宽,高)。"""
    return (rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1])


def window_to_content(bounds, insets, strip):
    """content_to_window 的逆运算：两个函数都用 (左,上,宽,高)，别把 (右,下) 传进来。"""
    l, t, w, h = bounds
    return (l + insets["left"], t + insets["top"] + strip,
            w - insets["left"] - insets["right"], h - insets["top"] - insets["bottom"] - strip)


def clamp_content(content, work_area, minimum=(900, 560)):
    """把内容矩形限制在屏幕可用区域内，并保证不小于最小尺寸。"""
    x, y, w, h = content
    min_w, min_h = minimum
    ax0, ay0, ax1, ay1 = work_area
    w = max(min_w, int(w))
    h = max(min_h, int(h))
    w = min(w, max(min_w, ax1 - ax0))
    h = min(h, max(min_h, ay1 - ay0))
    x = max(ax0, min(x, ax1 - w))
    y = max(ay0, min(y, ay1 - h))
    return (int(x), int(y), int(w), int(h))


def named_windows(title: str) -> list:
    """列出所有同名可见窗口（句柄 / 标题 / pid / 矩形 / 是否图标化）。

    自检用它确认「我们摆的那个窗口」和「屏幕上看到的窗口」是同一个：
    Chromium 可能留着第二个同名窗口，只按标题取第一个会摆错对象。
    """
    user32 = _api()
    if user32 is None:
        return []
    found = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value != title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append({"hwnd": int(hwnd), "title": buffer.value, "pid": pid.value,
                      "rect": window_rect(int(hwnd)), "iconic": bool(user32.IsIconic(ctypes.c_void_p(hwnd)))})
        return True

    user32.EnumWindows(_ENUM_PROC(callback), 0)
    return found
