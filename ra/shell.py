"""应用外壳：用程序自带的 Chromium 以 --app 模式承载界面，形成单一应用窗口。

页面里的 JS 通过 __raInvoke 调用后端方法，后端通过排队后的 evaluate 回推事件；
所有 Playwright 调用都固定在外壳线程上执行。
"""

from __future__ import annotations

import json
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
                 fullscreen: bool = True) -> None:
        self.ui_dir = Path(ui_dir)
        self.profile_dir = Path(profile_dir)
        self.on_invoke = on_invoke
        self.on_close = on_close
        self.size = size
        self.fullscreen = fullscreen
        self._tasks: "queue.Queue[tuple]" = queue.Queue()
        self._outbox: "queue.Queue[str | tuple]" = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ra-shell", daemon=True)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ra-api")
        self._pw = None
        self._context = None
        self._cdp = None
        self._window_id = 0
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

    # -- 窗口一体化：全屏时 Chromium 不画自己的标题栏 ---------------------
    def _init_window(self) -> None:
        """建立 CDP 会话并进入全屏无边框；失败时退回窗口模式，功能不受影响。

        这里已经在窗口线程上，必须直接调用，不能再走 _remote 排队（会自己等自己）。
        """
        self._cdp_open()
        if self.fullscreen:
            self._cdp_state("fullscreen")

    def _cdp_open(self) -> bool:
        try:
            self._cdp = self._context.new_cdp_session(self.page)
            self._window_id = self._cdp.send("Browser.getWindowForTarget")["windowId"]
            return True
        except Exception:
            self._cdp = None
            self._window_id = 0
            return False

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
        return state

    def _remote(self, name: str, *args):
        """Playwright 对象只能在窗口线程里用，其它线程排队提交。"""
        future: Future = Future()
        self._outbox.put(("remote", name, args, future))
        try:
            return future.result(timeout=15)
        except Exception:
            return None

    def _probe(self) -> dict:
        from . import winframe

        hwnd = winframe.find_window(self.title)
        rect = winframe.window_rect(hwnd) if hwnd else (0, 0, 0, 0)
        monitor = winframe.monitor_rect(hwnd) if hwnd else (0, 0, 0, 0)
        covers = bool(hwnd) and rect != (0, 0, 0, 0) and rect == monitor
        caption = winframe.has_caption(hwnd) if hwnd else True
        return {"hwnd": bool(hwnd), "rect": rect, "monitor": monitor, "caption": caption,
                "frameless": covers and not caption, "fullscreen": self.fullscreen,
                "cdp": bool(self._cdp), "supported": bool(self._cdp)}

    def win_state(self) -> dict:
        return self._probe()

    def win_drag(self) -> dict:
        # 全屏没有可拖动的边框；窗口模式下由 Chromium 自己的标题栏负责拖动。
        return {"ok": not self.fullscreen}

    def win_minimize(self) -> dict:
        return {"ok": self._remote("_cdp_state", "minimized") == "minimized"}

    def win_maximize(self) -> dict:
        """最大化按钮 = 全屏无边框 / 窗口模式 切换。"""
        applied = self._remote("_cdp_state", "normal" if self.fullscreen else "fullscreen")
        return {"state": applied, "maximized": applied == "fullscreen",
                "frameless": self._probe()["frameless"]}

    def win_fullscreen(self, on: bool = True) -> dict:
        applied = self._remote("_cdp_state", "fullscreen" if on else "normal")
        return {"state": applied, "maximized": applied == "fullscreen",
                "frameless": self._probe()["frameless"]}

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
                    future.set_result(getattr(self, name)(*args))
                except Exception as exc:  # noqa: BLE001
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
