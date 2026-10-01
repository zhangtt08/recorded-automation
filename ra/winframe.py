"""只读的 Win32 窗口状态查询：用于核对界面窗口是否真的没有系统标题栏。

窗口控制（全屏 / 最小化 / 关闭）走 CDP，不在这里改样式——Chromium 自己画的标题栏
不受 WS_CAPTION 影响，只有它进入全屏状态时才会消失。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_VISIBLE = 0x10000000
MONITOR_DEFAULTTONEAREST = 2

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


def find_window(title: str, owner_pid: int | None = None) -> int:
    user32 = _api()
    if user32 is None:
        return 0
    found = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if owner_pid:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != owner_pid:
                return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value == title:
            found.append(hwnd)
        return False

    user32.EnumWindows(_ENUM_PROC(callback), 0)
    return found[0] if found else 0


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
