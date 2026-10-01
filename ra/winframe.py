"""Windows 无边框窗口控制：彻底去掉系统标题栏，把关闭/最大化/最小化交给界面画。

只依赖 ctypes；非 Windows 或找不到窗口时所有操作返回 False，界面仍可用键盘操作。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

GWL_STYLE = -16
WS_BORDER = 0x00800000
WS_CAPTION = 0x00C00000
WS_DLGFRAME = 0x00400000
WS_THICKFRAME = 0x00040000
SW_MINIMIZE = 6
SW_RESTORE = 9
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2
SWP_FRAMECHANGED = 0x0020
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOSIZE = 0x0001
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_NCRENDERING_POLICY = 2
DWMWCP_ROUND = 2
DWMNCRP_DISABLED = 1

_user32 = None
_keep: list = []


def available() -> bool:
    return os.name == "nt"


def _api():
    global _user32
    if _user32 is None:
        _user32 = ctypes.windll.user32
        _user32.SetWindowLongW.restype = ctypes.c_long
        _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        _user32.GetWindowLongW.restype = ctypes.c_long
        _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        _user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        _user32.MonitorFromWindow.restype = wintypes.HMONITOR
        _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    return _user32


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _Margin(ctypes.Structure):
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cyTopHeight", ctypes.c_int),
                ("cxRightWidth", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _Rect), ("rcWork", _Rect), ("dwFlags", wintypes.DWORD)]


EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def find_window(title: str, owner_pid: int | None = None) -> int:
    """按标题找可见的顶层窗口；给出 owner_pid 时只认该进程的窗口。"""
    if not available():
        return 0
    user32 = _api()
    found: list[int] = []

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
        if owner_pid and pid.value != owner_pid:
            return True
        found.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return found[0] if found else 0


def _style(hwnd: int) -> int:
    return _api().GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF


def has_caption(hwnd: int) -> bool:
    if not available() or not hwnd:
        return False
    return bool(_style(hwnd) & WS_CAPTION)


def strip_frame(hwnd: int) -> bool:
    """去掉标题栏与细边框，保留可拉伸边框；圆角与无边框渲染交给 DWM。"""
    if not available() or not hwnd:
        return False
    user32 = _api()
    style = _style(hwnd)
    user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~(WS_CAPTION | WS_BORDER | WS_DLGFRAME))
    try:
        dwm = ctypes.windll.dwmapi
        corner = ctypes.c_int(DWMWCP_ROUND)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
                                  ctypes.byref(corner), ctypes.sizeof(corner))
        policy = ctypes.c_int(DWMNCRP_DISABLED)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), DWMWA_NCRENDERING_POLICY,
                                  ctypes.byref(policy), ctypes.sizeof(policy))
        # 把非客户区边框铺满客户区，消除残留的 1px 分隔线
        dwm.DwmExtendFrameIntoClientArea(ctypes.c_void_p(hwnd),
                                         ctypes.byref(_Margin(-1, -1, -1, -1)))
    except OSError:
        pass
    user32.SetWindowPos(ctypes.c_void_p(hwnd), 0, 0, 0, 0, 0,
                        SWP_FRAMECHANGED | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    return True


def place(hwnd: int, x: int, y: int, width: int, height: int) -> bool:
    """把（预先放在屏幕外的）窗口移到目标位置并显示。"""
    if not available() or not hwnd:
        return False
    _api().SetWindowPos(ctypes.c_void_p(hwnd), 0, x, y, width, height,
                        SWP_NOZORDER | SWP_SHOWWINDOW)
    return True


def drag(hwnd: int) -> bool:
    """让界面里的标题条可以拖动窗口。"""
    if not available() or not hwnd:
        return False
    user32 = _api()
    user32.ReleaseCapture()
    user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0)
    return True


def minimize(hwnd: int) -> bool:
    if not available() or not hwnd:
        return False
    _api().ShowWindow(ctypes.c_void_p(hwnd), SW_MINIMIZE)
    return True


def _work_area(hwnd: int) -> tuple[int, int, int, int]:
    user32 = _api()
    monitor = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(_MonitorInfo)
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _set_rect(hwnd: int, rect: tuple[int, int, int, int]) -> None:
    user32 = _api()
    user32.SetWindowPos(ctypes.c_void_p(hwnd), 0, rect[0], rect[1],
                        rect[2] - rect[0], rect[3] - rect[1], SWP_NOZORDER)


def _get_rect(hwnd: int) -> tuple[int, int, int, int]:
    user32 = _api()
    rect = _Rect()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _state(hwnd: int) -> dict:
    for entry in _keep:
        if entry.get("hwnd") == hwnd:
            return entry
    entry = {"hwnd": hwnd, "maximized": False, "restore": None}
    _keep.append(entry)
    return entry


def toggle_maximize(hwnd: int) -> str:
    """在“铺满工作区”和“还原”之间切换（不依赖系统最大化，避免盖住任务栏）。"""
    if not available() or not hwnd:
        return "unsupported"
    state = _state(hwnd)
    if state.get("maximized"):
        _set_rect(hwnd, state["restore"])
        state["maximized"] = False
        return "restored"
    state["restore"] = _get_rect(hwnd)
    _set_rect(hwnd, _work_area(hwnd))
    state["maximized"] = True
    return "maximized"


def is_maximized(hwnd: int) -> bool:
    """用工作区尺寸判断“已铺满”，因为最大化是我们自己做的。"""
    if not available() or not hwnd:
        return False
    return all(abs(left - right) <= 2 for left, right in zip(_get_rect(hwnd), _work_area(hwnd)))


def close(hwnd: int) -> bool:
    if not available() or not hwnd:
        return False
    _api().PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)  # WM_CLOSE
    return True


def restore(hwnd: int) -> bool:
    if not available() or not hwnd:
        return False
    _api().ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
    _api().SetForegroundWindow(ctypes.c_void_p(hwnd))
    return True
