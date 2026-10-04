"""应用外壳：用程序自带的 Chromium 以 --app 模式承载界面，形成单一应用窗口。

页面里的 JS 通过 __raInvoke 调用后端方法，后端通过排队后的 evaluate 回推事件；
所有 Playwright 调用都固定在外壳线程上执行。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from .engine import launch_persistent

ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "record_script.js": "text/javascript; charset=utf-8",
    "icon.png": "image/png",
}

BRIDGE_JS = """
(() => {
  if (window.__raShell) return;
  window.__raShell = true;
  const pending = new Map();
  let counter = 0;
  window.__raResolve = (id, text) => {
    const settle = pending.get(id);
    if (!settle) return;
    pending.delete(id);
    let value = null;
    try { value = text === null || text === undefined ? null : JSON.parse(text); }
    catch (error) { value = {error: '返回内容不是 JSON: ' + String(error)}; }
    if (value && value.__exception) settle.reject(new Error(value.__exception)); else settle.resolve(value);
  };
  const invoke = (method, args) => new Promise((resolve, reject) => {
    if (window.__raCalls) window.__raCalls.push(method);
    if (window.__raBlock && window.__raBlock(method)) {
      resolve({error: '按钮体检模式：这一项没有真正调用后端', blocked: method});
      return;
    }
    const id = 'r' + (++counter);
    pending.set(id, {resolve, reject});
    try { window.__raInvoke(id, method, JSON.stringify(args === undefined ? [] : args)); }
    catch (error) { pending.delete(id); reject(error); }
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error(method + ' 超时未返回')); } }, 180000);
  });
  window.pywebview = {
    api: new Proxy({}, {get: (target, name) => {
      if (typeof name !== 'string' || name === 'then' || name === 'catch' || name === 'finally' || name === 'toJSON') return undefined;
      return (...args) => invoke(name, args);
    }}),
    fire: () => undefined,
    platform: 'chromium-shell',
    toggle_fullscreen: () => invoke('__toggle_fullscreen', [])
  };
  window.dispatchEvent(new Event('pywebviewready'));
})();
"""


def applied_mode_ok(mode: str, state: dict) -> bool:
    """切换后按实测判断成没成：全屏要看窗口真的铺满显示器，停靠要看标题条真的出屏。"""
    if mode == "fullscreen":
        return bool(state.get("fullscreen")) and tuple(state.get("rect") or ()) == tuple(state.get("monitor") or ())
    if mode == "docked":
        return state.get("mode") == "docked" and bool(state.get("titlebar_hidden"))
    return state.get("mode") == "free" and not state.get("titlebar_hidden")


class _AssetServer(ThreadingHTTPServer):
    daemon_threads = True


def _make_handler(root: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server 约定
            name = self.path.split("?")[0].lstrip("/") or "index.html"
            if name not in ASSETS:                      # 只暴露界面资源
                self.send_error(404)
                return
            path = root / name
            if not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ASSETS[name])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静默
            pass

    return Handler


class Shell:
    """Owns the app window and the JS <-> Python bridge."""

    title = "录放台"

    def __init__(self, ui_dir: Path, profile_dir: Path, on_invoke: Callable[[str, list], object],
                 on_close: Callable[[], None] | None = None, size=(1360, 900),
                 fullscreen: bool = False, mode: str = "docked", geometry: dict | None = None,
                 on_geometry: Callable[[tuple, str], None] | None = None) -> None:
        self.ui_dir = Path(ui_dir)
        self.profile_dir = Path(profile_dir)
        self.on_invoke = on_invoke
        self.on_close = on_close
        self.size = size
        self.mode = mode if mode in {"docked", "free", "fullscreen"} else "docked"
        self.fullscreen = bool(fullscreen) or self.mode == "fullscreen"
        if self.fullscreen:
            self.mode = "fullscreen"
        self.docked = self.mode == "docked"
        self.geometry = dict(geometry or {})
        self.on_geometry = on_geometry
        self.maximized = False
        self._restore: tuple | None = None
        self._strip = 31
        self._insets = {"left": 8, "top": 0, "right": 8, "bottom": 8}
        self._viewport = (0, 0)
        self._content = (0, 0, 0, 0)
        self._asked = (0, 0, 0, 0)
        self._place_error = ""
        self._cdp_error = ""
        self._init_error = ""
        self._debug: list[str] = []
        self._via = ""
        self._calibrated = False
        self._pixel_ok = None
        # None = 这一条测不了（窗口被盖住）；整数 = 实测偏离了几像素。
        self._last_drift: int | None = None
        self._pixel_note = ""
        self._calibrating = False
        self._last_heal = 0.0
        self._healing = False
        self._place_lock = threading.Lock()
        self._tasks: "queue.Queue[tuple]" = queue.Queue()
        self._outbox: "queue.Queue[str | tuple]" = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ra-shell", daemon=True)
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ra-api")
        self._pw = None
        self._context = None
        self._cdp = None
        self._window_id = 0
        self._browser_pid = 0
        self._hwnd = 0
        self.page = None
        self.engine = ""
        self.url = ""
        self.error = ""

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "Shell":
        self._thread.start()
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.page is not None or self.error:
                break
            time.sleep(0.2)
        if self.error:
            raise RuntimeError(self.error)
        return self

    def wait(self) -> None:
        self._thread.join()

    def close(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- bridge ----------------------------------------------------------
    def evaluate(self, script: str) -> None:
        """Thread-safe script delivery into the app window (used by api.push)."""
        self._outbox.put(script)

    def evaluate_js(self, script: str) -> None:
        self.evaluate(script)

    def evaluate_value(self, script: str, timeout: float = 25.0):
        """Run script in the page and wait for its value (shell thread executes it)."""
        future: Future = Future()
        self._outbox.put(("value", script, future))
        try:
            return future.result(timeout=timeout)
        except Exception:
            return None

    def screenshot(self, path: Path, timeout: float = 20.0) -> bool:
        """Capture the app window's page into a PNG file (used by the UI self-check)."""
        future: Future = Future()
        self._outbox.put(("shot", str(path), future))
        try:
            return bool(future.result(timeout=timeout))
        except Exception:
            return False

    # -- 窗口一体化：把 Chromium 自绘的标题条藏到工作区之外 ----------------
    MIN_CONTENT = (960, 540)

    def _init_window(self) -> None:
        """建立 CDP 会话并把窗口摆成无边框。已在窗口线程上，必须直接调用，不能走 _remote。"""
        from . import winframe

        try:
            self._init_window_body()
        except Exception as exc:  # noqa: BLE001 - 摆放失败不能把界面线程带走，但原因必须留在界面上
            import traceback

            self._init_error = f"{type(exc).__name__}: {exc} | " + traceback.format_exc(limit=3).splitlines()[-2]
        self._place_error = self._place_error or self._init_error
        self._write_debug_trail()

    def _log(self, step: str, value="") -> None:
        self._debug.append(f"{step}={value}")

    def _write_debug_trail(self) -> None:
        """把窗口摆放的每一步留在本机：双击启动看不出问题时用它排查。"""
        try:
            state = self._probe()
            lines = [f"{index + 1}. {item}" for index, item in enumerate(self._debug)]
            lines += [f"mode={state['mode']} strip={state['strip']} via={self._via} cdp={state['cdp']}",
                      f"content={state['content']} rect={state['rect']} asked={state['asked']}",
                      f"titlebar_hidden={state['titlebar_hidden']} pixel_ok={state['pixel_ok']}",
                      f"place_error={state['place_error']}"]
            (self.profile_dir.parent / "window-debug.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _init_window_body(self) -> None:
        from . import winframe

        self._log("url", self.url)
        self._log("cdp", self._cdp_open())
        try:
            browser = self._context.browser
        except Exception:
            browser = None
        self._browser_pid = 0          # persistent context 的 Browser 没有 process_id，只能按标题找
        self._log("hwnd", self._find_hwnd())
        self._log("iconic", bool(self._hwnd) and winframe.is_iconic(self._hwnd))
        if not self._hwnd:
            self._init_error = "启动 8 秒内没按标题认出应用窗口，先按默认位置摆放，界面里的「当前窗口实测」会显示原因"
        # 浏览器档案可能把窗口记成最小化：外部 ShowWindow 会被它的状态机改回去，只能让它自己切
        if self._hwnd and winframe.is_iconic(self._hwnd):
            self._cdp_state("normal")
            self._log("after_restore", self._find_hwnd(2.0))
        self._log("measure", self._measure())
        if self.fullscreen:
            self._cdp_state("fullscreen")
            self._measure()
            return
        self._content = self._initial_content()
        self._log("initial", self._content)
        first = self._place(verify=True)
        self._log("place", first)
        if first.get("drift"):          # 像素复核没通过：几何差值不可信，显式标定一次再摆
            self._log("recalibrate", self._calibrate())
            self._log("replace", self._place(verify=True))

    def _find_hwnd(self, timeout: float = 8.0) -> int:
        from . import winframe

        deadline = time.time() + timeout
        while time.time() < deadline:
            host = self.url.split("//")[-1].split("/")[0] if self.url else ""
            names = tuple(item for item in (f"http://{host}", f"https://{host}", "127.0.0.1") if item)
            hwnd = (winframe.find_window(self.title, self._browser_pid, names)
                    or winframe.find_window(self.title, None, names))
            if hwnd:
                self._hwnd = hwnd
                return hwnd
            time.sleep(0.2)
        return self._hwnd

    def _measure(self, tries: int = 8, settle: float = 0.12) -> dict:
        """量出 Chromium 自绘标题条的高度与客户区四边留白（全屏时标题条高度为 0）。

        启动这几秒里浏览器档案会把窗口尺寸改好几轮，一次读到的「客户区高 - 视口高」可能差出
        几十像素（实测 31 被读成 76，窗口就会被顶过头）。所以要连读到两次完全一致才算数。
        """
        from . import winframe

        hwnd = self._hwnd if winframe.is_window(self._hwnd) else self._find_hwnd()
        if not hwnd:
            return {}
        last = None
        fallback: dict = {}
        for _ in range(tries):
            cw, ch = winframe.client_size(hwnd)
            wr = winframe.window_rect(hwnd)
            try:
                vp = self.page.evaluate("() => ({iw: innerWidth, ih: innerHeight, dpr: devicePixelRatio || 1})")
            except Exception:
                vp = None
            if vp and vp.get("ih"):
                dpr = float(vp.get("dpr") or 1)
                vw, vh = int(round(vp["iw"] * dpr)), int(round(vp["ih"] * dpr))
                insets = winframe.frame_insets(hwnd)
                strip = ch - vh
                consistent = vw == cw and 0 <= strip <= 120 and insets["left"] == insets["right"]
                fallback = {"strip": strip, "insets": insets, "client": (cw, ch), "vp": vp, "stable": False}
                if consistent:
                    sample = (strip, tuple(sorted(insets.items())), wr)
                    if last == sample:
                        self._strip, self._insets, self._viewport = strip, dict(insets), (vw, vh)
                        return {"strip": strip, "insets": dict(self._insets), "client": (cw, ch),
                                "vp": vp, "stable": True}
                    last = sample
                else:
                    last = None
            time.sleep(settle)
        if fallback:
            self._strip = int(fallback["strip"])
            self._insets = dict(fallback["insets"])
            self._viewport = (fallback["vp"]["iw"], fallback["vp"]["ih"])
        return fallback

    # -- 像素级标定：几何差值会被启动期的尺寸抖动污染，这里用屏幕像素定死真实高度 ----
    MARKER_RGB = (234, 90, 36)          # 与 app.css 里 body[data-verify="1"]::before 的颜色一致
    MARKER_TOLERANCE = 26

    def _marker(self, on: bool) -> None:
        try:
            self.page.evaluate("(value) => { document.body.dataset.verify = value; return true; }",
                               "1" if on else "")
        except Exception:
            pass

    def _find_marker_row(self, x: int, top: int, bottom: int) -> int:
        """在屏幕某一列上自上而下找标记色横条，返回它相对 top 的行号；找不到返回 -1。"""
        from . import winframe

        want = self.MARKER_RGB
        for row in range(0, max(0, bottom - top)):
            got = winframe.screen_pixel(x, top + row)
            if got[0] >= 0 and all(abs(got[index] - want[index]) <= self.MARKER_TOLERANCE for index in range(3)):
                return row
        return -1

    def _verify_marker(self, x: int, y: int) -> int | None:
        """标记条应当正好落在内容顶边：返回偏离了几像素，0 = 摆放精确。

        返回 **None** 表示「屏幕上根本找不到标记条」——最常见的成因是应用窗口被别的程序
        盖住了（这台机器上多个 AI 应用会抢前台），`GetPixel` 取到的是别人的像素。
        以前这种情况返回 -1，而调用方把任何非零值都当成像素偏差去改 `strip`，于是
        「测不到」被伪装成「差 1 像素」，strip 被一次次推高（实测 31 → 33），
        反而把原本合格的无边框摆放弄坏。测不到就必须说测不到，并且不许动几何参数。
        """
        from . import winframe

        got = winframe.screen_pixel(x, y + 2)
        want = self.MARKER_RGB
        if got[0] >= 0 and all(abs(got[index] - want[index]) <= self.MARKER_TOLERANCE for index in range(3)):
            return 0
        row = self._find_marker_row(x, max(0, y - 4), y + 140)
        return None if row < 0 else row - 2

    def _recalibrating(self) -> bool:
        """正在标定里：那段时间窗口被故意顶到工作区顶边，像素复核测出来的东西不可信。"""
        return bool(self._calibrating)

    def _calibrate(self) -> dict:
        """把窗口临时顶到工作区顶边（不加偏移），页面最上面画一条标记色横条：
        它出现在第几行，Chromium 自绘的标题条就有多高。量准之后才按这个高度把标题条顶出屏幕 ——
        只信几何差值会把界面顶部切掉几十像素（实测 31 被读成 76）。"""
        if not self._live_hwnd():
            return {"ok": False, "note": "没有窗口句柄，无法标定"}
        area = self._work_area()
        target = tuple(self._content)
        x = target[0] if target[2] else area[0] + 200
        w = target[2] if target[2] else 1400
        h = target[3] if target[3] else 860
        self._content = (x, area[1], w, h)
        self._marker(True)
        self._calibrating = True
        time.sleep(0.25)
        best = -1
        try:
            for _ in range(6):
                self._send_bounds(strip_override=0)  # 窗口顶边 = 内容顶边 = 工作区顶边
                time.sleep(0.18)
                row = self._find_marker_row(x + 120, area[1], area[1] + 200)
                if row >= 0:
                    best = row
                    break
        finally:
            self._calibrating = False
            self._marker(False)
            self._content = target
        if best < 0:
            self._log("calibrate", "屏幕上没找到标定标记条，沿用几何差值 " + str(self._strip))
            return {"ok": False, "strip": self._strip, "note": "屏幕上没找到标定标记条"}
        self._strip = best
        self._calibrated = True
        return {"ok": True, "strip": best}

    def _work_area(self) -> tuple[int, int, int, int]:
        from . import winframe

        return winframe.work_area(self._hwnd) if self._hwnd else (0, 0, 1920, 1040)

    def _normalize(self, content) -> tuple[int, int, int, int]:
        """把内容矩形限制在工作区内；停靠模式额外把顶边钉在工作区顶边。"""
        from . import winframe

        area = self._work_area()
        rect = winframe.clamp_content(content, area, self.MIN_CONTENT)
        if self.docked:
            rect = (rect[0], area[1], rect[2], min(rect[3], area[3] - area[1]))
        return rect

    def _initial_content(self) -> tuple[int, int, int, int]:
        area = self._work_area()
        geo = dict(self.geometry or {})
        w = int(geo.get("w") or min(self.size[0], area[2] - area[0]))
        h = int(geo.get("h") or min(self.size[1], area[3] - area[1]))
        if geo.get("x") is not None:
            x = int(geo["x"])
        else:
            x = area[0] + max(0, (area[2] - area[0] - w) // 2)
        y = int(geo.get("y") or area[1])
        return self._normalize((x, y, w, h))

    def _send_bounds(self, strip_override: int | None = None) -> tuple:
        """按当前内容矩形发一次窗口边界，返回发出去的窗口矩形。CDP 不通就走 Win32。"""
        from . import winframe

        strip = 0 if self.fullscreen else (self._strip if strip_override is None else strip_override)
        win = winframe.content_to_window(self._content, self._insets, strip)
        self._asked = tuple(int(v) for v in win)
        bounds = {"left": win[0], "top": win[1], "width": win[2], "height": win[3]}
        if not self.fullscreen:
            bounds["windowState"] = "normal"
        if self._cdp:
            # windowId 会随 Chromium 重建窗口而变旧；拿旧 id 发边界，第一次有效、之后会被
            # 它自己回弹到上一次位置（实测第二次拖动就弹回去）。每次发之前重新问一次。
            try:
                self._window_id = self._cdp.send("Browser.getWindowForTarget")["windowId"]
            except Exception as exc:  # noqa: BLE001
                self._cdp_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
        for attempt in range(2):
            if self._cdp:
                try:
                    self._cdp.send("Browser.setWindowBounds", {"windowId": self._window_id, "bounds": bounds})
                    self._via = "cdp"
                    return win
                except Exception as exc:  # noqa: BLE001 - 一次失败不代表会话死了，先试着重开
                    self._cdp_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
                    self._cdp = None
                    if attempt == 0:
                        self._cdp_open()
                        continue
                    break
            else:
                break
        self._via = "win32"
        winframe.set_bounds(self._live_hwnd(), *win)
        return win

    def _place(self, content=None, sync: bool = True, verify: bool = False) -> dict:
        """按内容矩形摆放窗口：窗口顶边 = 内容顶边 - 标题条高度，让那条标题条出屏。"""
        from . import winframe

        if not self._live_hwnd():
            return {"ok": False, "note": "还没认出自己的窗口句柄"}
        # 串行化：手势与自愈不能交错改写同一个内容矩形（谁后写谁赢，但中途不能互踩）
        with self._place_lock:
            return self._place_locked(content, sync, verify)

    def _place_locked(self, content=None, sync: bool = True, verify: bool = False) -> dict:
        from . import winframe

        if content is not None:
            self._content = self._normalize(content)
        elif self._content == (0, 0, 0, 0):
            self._content = self._initial_content()
        # 模式变了就要按新模式重新归一化：停靠把顶边钉回工作区顶边，自由窗口不钉。
        # 少了这一步，从自由窗口切回停靠会沿用旧的 y，标题条又露出来（自检实测到过）。
        self._content = self._normalize(self._content)
        self._place_error = ""
        win = self._send_bounds()
        # 启动期档案会再改几轮窗口尺寸，Windows 也可能把负顶边拉回屏幕内：
        # 反复按目标重新摆放，稳定后仍不一致就以实际为准（不谎报已经摆好）。
        settled = False
        for _ in range(10):
            time.sleep(0.12)
            actual = self._read_content()
            if max(abs(a - b) for a, b in zip(actual, self._content)) <= 3:
                settled = True
                break
            self._send_bounds()
        actual = self._read_content()
        if not settled:
            self._content = self._normalize(actual)
            self._place_error = "窗口没能稳定在目标位置，已按实际位置继续（到设置页点「重新测量」可重试）"
        if sync and verify and not self.fullscreen:
            if self._recalibrating():
                self._pixel_ok = False
                self._last_drift = None
                self._pixel_note = "标定过程中：这一步的像素复核不作数"
            else:
                self._marker(True)
                time.sleep(0.2)
                drift = self._verify_marker(self._content[0] + 40, self._content[1])
                self._marker(False)
                self._last_drift = drift
                if drift is None:
                    # 屏幕上找不到标记条 = 窗口被别的应用盖住了，这条测不了：如实说出来，
                    # 并且绝不据此改 strip（改了就是一次真实的几何破坏）。
                    self._pixel_ok = False
                    self._pixel_note = "像素复核这一条测不了：应用窗口被别的程序盖住了，请把它带到前台再点「重新测量」"
                else:
                    self._pixel_note = ""
                    if drift:  # 正数=内容被切掉（标题条量多了）；负数=顶部露出浏览器标题条（量少了）
                        self._pixel_ok = False
                        self._strip = max(0, self._strip - drift)
                        self._send_bounds()
                        time.sleep(0.2)
                        self._marker(True)
                        time.sleep(0.15)
                        again = self._verify_marker(self._content[0] + 40, self._content[1])
                        self._marker(False)
                        self._pixel_ok = again == 0
                        if again is None:
                            self._pixel_note = "校正后窗口被别的程序盖住，无法确认可否"
                    else:
                        self._pixel_ok = True
        if sync:
            self._sync_page()
        return {"ok": settled, "content": self._content, "window": win, "asked": list(self._asked),
                "actual": list(actual), "via": self._via, "strip": self._strip,
                "strip_measured": self._calibrated, "pixel_note": self._pixel_note,
                "pixel_ok": self._pixel_ok, "drift": self._last_drift}

    def _live_hwnd(self, timeout: float = 0.6) -> int:
        """Chromium 会换掉它的原生窗口句柄（实测：摆放下发后旧句柄仍报告已到位，而屏幕上的
        窗口根本没动）。所有几何读写都走这里重新确认句柄，缓存只在还有效时用。"""
        from . import winframe

        if self._hwnd and winframe.is_window(self._hwnd):
            return self._hwnd
        return self._find_hwnd(timeout)

    def _read_content(self) -> tuple[int, int, int, int]:
        from . import winframe

        hwnd = self._live_hwnd()
        if not hwnd:
            return (0, 0, 0, 0)

        strip = 0 if self.fullscreen else self._strip
        return winframe.window_to_content(winframe.rect_to_bounds(winframe.window_rect(hwnd)),
                                          self._insets, strip)

    def _sync_page(self) -> None:
        try:
            self.page.evaluate("() => { window.__raSyncFrame && window.__raSyncFrame(); return true; }")
        except Exception:
            pass

    def _cdp_open(self) -> bool:
        try:
            self._cdp = self._context.new_cdp_session(self.page)
            self._window_id = self._cdp.send("Browser.getWindowForTarget")["windowId"]
            self._cdp_error = ""
            return True
        except Exception as exc:  # noqa: BLE001 - 没有 CDP 就走 Win32 兜底，但原因要留在界面上
            self._cdp = None
            self._window_id = 0
            self._cdp_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
            return False

    def cdp_available(self) -> bool:
        return bool(self._cdp)

    def _cdp_state(self, state: str) -> str:
        """只能在窗口线程执行：切换 Chromium 的窗口状态。"""
        if not self._cdp:
            return ""
        try:
            self._cdp.send("Browser.setWindowBounds",
                           {"windowId": self._window_id, "bounds": {"windowState": state}})
        except Exception:
            return ""
        if state in ("fullscreen", "normal"):
            self.fullscreen = state == "fullscreen"
            self.mode = state if state == "fullscreen" else ("docked" if self.docked else "free")
            self._measure()
            if not self.fullscreen:
                self._place()
        self._sync_page()
        return state

    def _remote(self, name: str, *args):
        """Playwright 对象只能在窗口线程里用，其它线程排队提交。"""
        future: Future = Future()
        self._outbox.put(("remote", name, args, future))
        try:
            return future.result(timeout=20)
        except Exception:
            return None

    def _probe(self) -> dict:
        from . import winframe

        hwnd = self._live_hwnd(0.4)
        wr = winframe.window_rect(hwnd) if hwnd else (0, 0, 0, 0)
        monitor = winframe.monitor_rect(hwnd) if hwnd else (0, 0, 0, 0)
        work = self._work_area()
        content = self._read_content() if hwnd else (0, 0, 0, 0)
        covers = bool(hwnd) and wr != (0, 0, 0, 0) and wr == monitor
        caption = winframe.has_caption(hwnd) if hwnd else True
        # 标题条是否真的看不见：它占据屏幕上的 [窗口顶边+上留白, 内容顶边)，与可见区域没有交集
        band_bottom = wr[1] + self._insets.get("top", 0) + self._strip
        hidden = bool(hwnd) and (self.fullscreen or band_bottom <= max(work[1], monitor[1]) + 1)
        return {"hwnd": bool(hwnd), "pid": self._browser_pid, "rect": wr, "monitor": monitor, "work": work,
                "content": content, "caption": caption, "strip": self._strip, "insets": dict(self._insets),
                "viewport": list(self._viewport), "iconic": winframe.is_iconic(hwnd) if hwnd else False,
                "client": winframe.client_size(hwnd) if hwnd else (0, 0),
                "frameless": bool(hwnd) and (covers and not caption or hidden),
                "titlebar_hidden": hidden, "docked": self.docked, "mode": self.mode,
                "maximized": self.maximized, "fullscreen": self.fullscreen,
                "cdp": bool(self._cdp), "supported": bool(self._cdp),
                "place_error": self._place_error or self._cdp_error or self._init_error,
                "debug": list(self._debug), "debug_dir": str(self.profile_dir.parent), "asked": list(self._asked),
                "pixel_ok": self._pixel_ok, "drift": self._last_drift,
                # strip 是否真的被像素标过：没标过（或测不到）时界面要写「大约」，不能写成精确值。
                "strip_measured": self._calibrated, "strip_note": self._pixel_note,
                "calibrated": self._calibrated, "via": self._via,
                "band_bottom": band_bottom, "work_top": work[1]}

    def win_state(self) -> dict:
        """界面每 2.5 秒会问一次。Chromium 的窗口档案可能在启动之后才把窗口拉回它记住的
        位置（实测出现过：摆好后又回到默认位置），所以停靠模式下发现标题条重新露出来就自己补一次。

        自愈必须便宜且一次只跑一个：以前它每次都重量一遍标题条（最长约 10 秒），
        把窗口线程占满，用户拖标题条的手势被排在后面直接超时。
        """
        state = self._probe()
        # 只补「标题条又露出来」这一种：Chromium 也会把自己记住的位置刷回来（实测摆到 x=400
        # 后又回到 340），那种回弹去硬顶就是互相拉扯，所以不追位置，只保证无边框形态不被破坏。
        needs = state.get("mode") == "docked" and not state.get("titlebar_hidden")
        if needs and not self._healing and time.time() - self._last_heal > 5.0:
            self._last_heal = time.time()
            self._healing = True
            # 只投递、不等待：在池线程里同步等窗口线程会把 4 个 worker 全占住，
            # 结果用户的手势调用排在后面拿不到线程（实测：拖标题条完全不动）。
            self._outbox.put(("remote", "win_ensure_docked", (), None))
            state["healed"] = True
        return state

    def win_ensure_docked(self) -> dict:
        """只按已知标题条高度重摆一次，不重新测量（测量留给启动标定与「重新测量」按钮）。"""
        self._log("heal", self._content)
        try:
            result = self._place()
            self._remember()
            return result
        finally:
            self._healing = False

    def win_remeasure(self) -> dict:
        """设置页「重新测量」：重量标题条高度，再按目标摆放并做像素复核。"""
        return self._remote("win_remeasure_body") or {"ok": False, "error": "窗口线程没有响应"}

    def win_remeasure_body(self) -> dict:
        self._log("remeasure", self._measure())
        result = self._place(verify=True)
        # 只有「实测到确实差了几像素」才值得重标定；drift 为 None 是测不了（窗口被盖住），
        # 拿它去标定只会把标定失败当成量到了新高度。
        if result.get("drift"):
            self._log("remeasure_calibrate", self._calibrate())
            result = self._place(verify=True)
        return {**result, **self._probe()}

    # -- 界面里的手势：拖动标题条、拖边缘缩放 ------------------------------
    def win_move(self, left: int, top: int) -> dict:
        """按指针屏幕坐标移动窗口（顶边由停靠规则决定，不需要界面自己算标题条高度）。"""
        return self._remote("win_move_body", int(left), int(top)) or {"ok": False, "error": "窗口线程没有响应"}

    def win_move_body(self, left: int, top: int) -> dict:
        self._log("gesture_move", [int(left), int(top)])
        y = self._work_area()[1] if self.docked else int(top)
        self._content = self._normalize((int(left), y, self._content[2], self._content[3]))
        return self._place()

    def win_resize(self, content) -> dict:
        return self._remote("win_resize_body", list(content)) or {"ok": False, "error": "窗口线程没有响应"}

    def win_resize_body(self, content) -> dict:
        self._log("gesture_resize", list(content))
        x, y, w, h = [int(v) for v in content]
        self._content = self._normalize((x, y, w, h))
        return self._place()

    def win_gesture_end(self) -> dict:
        """界面的一次拖动/缩放结束：把最终几何写回设置，下次启动保持同样的窗口形态。"""
        return self._remote("win_gesture_end_body") or {"ok": False, "error": "窗口线程没有响应"}

    def win_gesture_end_body(self) -> dict:
        """一次手势结束：把最终几何写回设置。这里刻意不再反复重摆 ——
        反复摆放会占住窗口线程，把界面上后续的桥调用全堵住（实测手势第二次调用就超时）。"""
        state = self._probe()
        self._log("gesture_end", state.get("content"))
        self._remember()
        return state

    def win_minimize(self) -> dict:
        if self._cdp:
            return {"ok": self._remote("_cdp_state", "minimized") == "minimized"}
        from . import winframe

        self._hwnd = self._live_hwnd()
        if not self._hwnd:
            return {"ok": False, "note": "没有窗口句柄，无法最小化"}
        import ctypes

        ok = bool(winframe._api().ShowWindow(ctypes.c_void_p(self._hwnd), 6))   # SW_MINIMIZE
        return {"ok": ok, "note": "用 Win32 最小化（CDP 不可用）"}

    def win_maximize(self) -> dict:
        """最大化按钮 = 铺满工作区 / 还原上一次的大小与位置。"""
        return self._remote("win_maximize_body") or {"ok": False, "error": "窗口线程没有响应"}

    def win_maximize_body(self) -> dict:
        area = self._work_area()
        if self.maximized:
            self.maximized = False
            self._content = self._normalize(self._restore or (area[0] + 120, area[1], 1400, 860))
        else:
            self._restore = self._content
            self.maximized = True
            self._content = self._normalize((area[0], area[1], area[2] - area[0], area[3] - area[1]))
        out = self._place()
        self._remember()
        return {**out, "maximized": self.maximized, "content": self._content}

    def win_mode(self, mode: str = "") -> dict:
        """docked（停靠无边框）/ free（可自由移动的窗口，会露出浏览器标题条）/ fullscreen。"""
        mode = str(mode or "").strip().lower()
        if mode not in {"docked", "free", "fullscreen"}:
            return {"ok": False, "error": "未知窗口模式：" + mode, "modes": ["docked", "free", "fullscreen"]}
        return self._remote("win_mode_body", mode) or {"ok": False, "error": "窗口线程没有响应"}

    def win_mode_body(self, mode: str) -> dict:
        if mode == "fullscreen":
            self.mode = "fullscreen"
            self.docked = False
            applied = self._cdp_state("fullscreen") == "fullscreen" and self.fullscreen
            if not applied:                              # 引擎不支持全屏：如实报告，不假装成功
                self.mode = "docked" if self._strip else "free"
                self.docked = self.mode == "docked"
        else:
            if self.fullscreen:
                self._cdp_state("normal")
            self.mode = mode
            self.docked = mode == "docked"
            self._measure()
            if mode == "free":
                # 自由窗口的意义就是能拖到任意位置，因此把内容顶边让开一条标题条的高度；
                # 停靠模式反过来：顶边贴住工作区，让那条标题条整段出屏。
                area = self._work_area()
                self._content = self._normalize((self._content[0], area[1] + self._strip + 8,
                                                 self._content[2] or 1400, self._content[3] or 820))
            self._place(verify=True)
        state = self._probe()
        if not applied_mode_ok(mode, state):
            self._log("mode_first_try", {k: state.get(k) for k in ("mode", "content", "asked", "titlebar_hidden")})
            # 上一次切换可能还在收尾（窗口状态机、档案记忆），重量一次再重摆一次
            self._log("retry", mode)
            self._measure()
            self._place(verify=True)
            state = self._probe()
        self._remember()
        error = "" if applied_mode_ok(mode, state) else "切换没有生效：" + (self._cdp_error or "窗口状态没变")
        self._write_debug_trail()
        return {"ok": applied_mode_ok(mode, state), "mode": state["mode"], "error": error,
                "frameless": state["frameless"], "titlebar_hidden": state["titlebar_hidden"],
                "strip": state["strip"], "content": state["content"], "fullscreen": state["fullscreen"],
                "asked": state["asked"], "requested": mode}

    def _remember(self) -> None:
        if self.on_geometry:
            try:
                self.on_geometry(self._content, self.mode)
            except Exception:
                pass

    def win_fullscreen(self, on: bool = True) -> dict:
        return self.win_mode("fullscreen" if on else ("docked" if self._strip else "free"))

    def win_close(self) -> dict:
        self._stop.set()
        return {"ok": True}

    def _on_invoke(self, source, request_id: str, method: str, args_json: str) -> None:
        """Runs on the shell thread; must return immediately."""
        def work():
            try:
                result = self.on_invoke(method, json.loads(args_json or "[]"))
                payload = json.dumps(result if result is not None else None, ensure_ascii=False, default=str)
            except Exception as exc:  # 把异常带回 JS 侧
                payload = json.dumps({"__exception": f"{type(exc).__name__}: {str(exc)[:200]}"}, ensure_ascii=False)
            self._outbox.put(("resolve", request_id, payload))

        self._pool.submit(work)

    # -- main loop -------------------------------------------------------
    def _run(self) -> None:
        server = _AssetServer(("127.0.0.1", 0), _make_handler(self.ui_dir))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/index.html"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            f"--app={self.url}",
            f"--window-size={self.size[0]},{self.size[1]}",
            f"--window-min-size={max(900, self.size[0] // 2)},{max(600, self.size[1] // 2)}",
            "--window-position=120,60",
            "--disable-features=Translate",
            "--no-default-browser-check",
            "--no-first-run",
        ]
        try:
            self._pw = sync_playwright().start()
            self._context, self.engine = launch_persistent(self._pw, self.profile_dir, headless=False,
                                                           args=args, viewport=None)
            self._context.expose_binding("__raInvoke", self._on_invoke)
            self._context.set_default_timeout(30_000)
            pages = list(self._context.pages)
            self.page = pages[0] if pages else self._context.new_page()
            self.page.on("close", lambda _page: self._stop.set())
            # 界面自己画确认框；这里只兜底关掉任何残留的原生对话框，避免卡住。
            self.page.on("dialog", lambda dialog: dialog.dismiss())
            self.page.add_init_script(BRIDGE_JS)
            if os.environ.get("RA_UI_AUDIT") == "1":
                from .uiaudit import audit_script  # noqa: E402 - 只在体检模式里用

                self.page.add_init_script(audit_script())
            self.page.goto(self.url, wait_until="domcontentloaded", timeout=45_000)
            try:
                self.page.evaluate(BRIDGE_JS)  # 首次加载已完成的兜底注入
            except PlaywrightError:
                pass
            self._init_window()
            self._pump()
        except PlaywrightError as exc:
            self.error = str(exc).splitlines()[0] if str(exc) else "无法启动应用窗口"
        finally:
            for closer in (self._context.close if self._context else None, self._pw.stop if self._pw else None):
                try:
                    if closer is not None:
                        closer()
                except Exception:
                    pass
            server.shutdown()
            server.server_close()
            self._pool.shutdown(wait=False)
            if self.on_close is not None:
                try:
                    self.on_close()
                except Exception:
                    pass

    def _pump(self) -> None:
        """Only this thread talks to the page; other threads queue work."""
        while not self._stop.is_set():
            try:
                item = self._outbox.get(timeout=0.15)
            except queue.Empty:
                continue
            if item is None:
                continue
            if isinstance(item, str):
                try:
                    self.page.evaluate(item)
                except PlaywrightError:
                    pass
                continue
            kind = item[0]
            if kind == "value":
                _, script, future = item
                try:
                    future.set_result(self.page.evaluate(script))
                except Exception as exc:  # noqa: BLE001
                    future.set_exception(exc)
                continue
            if kind == "remote":
                _, name, args, future = item
                try:
                    result = getattr(self, name)(*args)
                    if future is not None:
                        future.set_result(result)
                except Exception as exc:  # noqa: BLE001
                    if future is not None:
                        future.set_exception(exc)
                continue
            if kind == "shot":
                _, target, future = item
                try:
                    self.page.screenshot(path=target)
                    future.set_result(True)
                except Exception as exc:  # noqa: BLE001
                    future.set_exception(exc)
                continue
            _, request_id, payload = item
            try:
                self.page.evaluate("([id, text]) => { window.__raResolve && window.__raResolve(id, text); }",
                                   [request_id, payload])
            except PlaywrightError:
                pass
