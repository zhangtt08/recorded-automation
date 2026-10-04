"""进程内 Agent 接口服务：让桌面程序自己就是那个「专供 Agent 调用」的后端。

契约与 agent/server.py 完全一致（/api/health、/api/agent/tools、/api/agent/manifest、
POST /api/agent/tool），区别只有两点：
1. 它跑在应用窗口同一个进程里，直接复用界面那套 Session/Api —— Agent 发起的录制与运行
   会实时反映在界面上，也不会和界面抢同一个浏览器档案目录。
2. 只监听 127.0.0.1；端口被占用时自动 +1，并把实际地址写进 agent/.endpoint 供 MCP 桥读取。

工具表从 agent/tools.py 加载（源码运行与封装版都走这条路径），加载失败时界面会显示原因，
而不是假装服务已经就绪。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .paths import resource_root

MAX_BODY = 8 * 1024 * 1024


class AgentApiError(Exception):
    """与 agent/errors.py 同构：code + message，用于把校验失败原样带回 JSON。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _agent_dir() -> Path:
    for candidate in (resource_root() / "agent", Path(__file__).resolve().parent.parent / "agent"):
        if (candidate / "tools.py").is_file():
            return candidate
    return resource_root() / "agent"


def load_tools() -> tuple[dict[str, Any], list[dict[str, Any]], Callable[[object, object], None], Path, type]:
    """加载 agent/tools.py，返回 (project, tools, bind, 目录, 错误类)。找不到时抛错并写清出路。

    错误类必须用 tools.py 自己 import 的那一个：工具里 raise 的是它，服务端 catch 的也得是它，
    否则 bad_input 会被当成未知异常报成 500。
    """
    here = _agent_dir()
    tools_path = here / "tools.py"
    if not tools_path.is_file():
        raise FileNotFoundError(f"缺少工具表文件：{tools_path}（请确认 agent/ 目录随程序一起分发）")
    for path in (str(here), str(here.parent)):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location("ra_agent_tools", tools_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ra_agent_tools"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    project = getattr(module, "PROJECT", {"name": "recorded-automation", "version": "0.0.0"})
    tools = list(getattr(module, "TOOLS", []))

    def bind(api, session) -> None:
        hook = getattr(module, "bind", None)
        if callable(hook):
            hook(api, session)

    error_cls = getattr(module, "AgentError", AgentApiError)
    return project, tools, bind, here, error_cls


def _descriptor(tool: dict[str, Any]) -> dict[str, Any]:
    return {"name": tool["name"], "description": tool["description"],
            "input_schema": tool.get("input_schema", {}), "risk": tool.get("risk", "read")}


class AgentServer:
    """一个可停止的 127.0.0.1 服务；界面用它显示端点，也让 Agent 直接调用同一套能力。"""

    def __init__(self, api=None, session=None, port: int = 8795, host: str = "127.0.0.1") -> None:
        self.api = api
        self.session = session
        self.requested_port = int(port)
        self.host = host
        self.port = 0
        self.error = ""
        self.tools: list[dict[str, Any]] = []
        self.project: dict[str, Any] = {}
        self.started_at = time.time()
        self.calls = 0
        self.error_cls: type = AgentApiError
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "AgentServer":
        try:
            project, tools, bind, here, self.error_cls = load_tools()
        except Exception as exc:  # noqa: BLE001 - 起不来要在界面上写清原因
            self.error = f"{type(exc).__name__}: {exc}"
            return self
        self.project, self.tools = project, tools
        if self.api is not None:
            try:
                bind(self.api, self.session)
            except Exception as exc:  # noqa: BLE001
                self.error = f"绑定界面会话失败：{type(exc).__name__}: {exc}"
        by_name = {tool["name"]: tool for tool in tools}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(raw)))
                self.send_header("access-control-allow-origin", "*")
                self.send_header("cache-control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self.send_header("access-control-allow-headers", "content-type")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                route = self.path.split("?")[0].rstrip("/") or "/"
                base = f"http://{outer.host}:{outer.port}"
                if route == "/api/health":
                    self._send(200, {"ok": True, "data": {
                        "project": outer.project.get("name", ""), "version": outer.project.get("version", ""),
                        "agent_api": 1, "tools": len(outer.tools), "in_process": True,
                        "serving": outer.api is not None, "endpoint": base,
                        "uptime_ms": int((time.time() - outer.started_at) * 1000), "calls": outer.calls}})
                elif route == "/api/agent/tools":
                    self._send(200, {"ok": True, "data": [_descriptor(tool) for tool in outer.tools]})
                elif route == "/api/agent/manifest":
                    self._send(200, {"ok": True, "data": {
                        "project": outer.project.get("name", ""), "version": outer.project.get("version", ""),
                        "description": outer.project.get("summary", ""), "base_url": base,
                        "in_process": True, "tools": [_descriptor(tool) for tool in outer.tools]}})
                else:
                    self._send(404, {"ok": False, "error": {"code": "not_found", "message": f"未知路径 {route}",
                                                            "endpoints": ["/api/health", "/api/agent/tools",
                                                                          "/api/agent/manifest", "POST /api/agent/tool"]}})

            def do_POST(self) -> None:  # noqa: N802
                route = self.path.split("?")[0].rstrip("/") or "/"
                if route != "/api/agent/tool":
                    self._send(404, {"ok": False, "error": {"code": "not_found", "message": f"未知路径 {route}"}})
                    return
                try:
                    length = int(self.headers.get("content-length") or 0)
                    if length > MAX_BODY:
                        raise AgentApiError("too_large", "请求体超过 8MB")
                    body = json.loads(self.rfile.read(length) or b"{}")
                    tool = by_name.get(body.get("tool"))
                    if tool is None:
                        self._send(400, {"ok": False, "error": {
                            "code": "unknown_tool", "message": f"未注册的工具：{body.get('tool')}",
                            "available": list(by_name)}})
                        return
                    t0 = time.time()
                    outer._validate(tool.get("input_schema"), body.get("input"))
                    handler: Callable[..., Any] = tool["handler"]
                    with outer._lock:
                        data = handler(body.get("input") or {})
                        outer.calls += 1
                    self._send(200, {"ok": True, "tool": tool["name"],
                                     "ms": int((time.time() - t0) * 1000), "data": data})
                except outer.error_cls as exc:      # 工具层的结构化错误：code 原样带回，状态码统一 400
                    code = getattr(exc, "code", "handler_failed") or "handler_failed"
                    self._send(400, {"ok": False, "error": {"code": code, "message": str(exc)}})
                except json.JSONDecodeError:
                    self._send(400, {"ok": False, "error": {"code": "bad_json", "message": "请求体不是合法 JSON"}})
                except Exception as exc:  # noqa: BLE001 - 工具内部异常要变成可读错误
                    self._send(500, {"ok": False, "error": {
                        "code": "handler_failed", "message": f"{type(exc).__name__}: {exc}"}})

            def log_message(self, *_a: Any) -> None:
                pass

        for candidate in range(self.requested_port, self.requested_port + 12):
            try:
                httpd = ThreadingHTTPServer((self.host, candidate), Handler)
            except OSError:
                continue
            httpd.daemon_threads = True
            self._httpd, self.port = httpd, candidate
            break
        if self._httpd is None:
            self.error = f"端口 {self.requested_port}~{self.requested_port + 11} 全部被占用"
            return self
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="ra-agent-api", daemon=True)
        self._thread.start()
        self._write_endpoint(here)
        return self

    def _write_endpoint(self, here: Path) -> None:
        try:
            (here / ".endpoint").write_text(f"http://{self.host}:{self.port}\n", encoding="utf-8")
        except OSError:
            pass

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._httpd = None

    def info(self) -> dict:
        return {"ok": bool(self.port) and not self.error, "port": self.port,
                "url": f"http://{self.host}:{self.port}" if self.port else "",
                "error": self.error, "tools": len(self.tools),
                "tool_names": [tool["name"] for tool in self.tools],
                "in_process": self.api is not None, "calls": self.calls,
                "uptime_s": round(time.time() - self.started_at, 1)}


    def _validate(self, schema: dict[str, Any] | None, data: Any) -> None:
        """按工具的 JSON Schema 做入口校验：缺参数与未知参数都回 bad_input，而不是猜。"""
        if not schema or schema.get("type") != "object":
            return
        obj = data if isinstance(data, dict) else {}
        missing = [key for key in schema.get("required", []) if obj.get(key) in (None, "", [])]
        if missing:
            raise self.error_cls("bad_input", "缺少必填参数：" + ", ".join(missing))
        if schema.get("additionalProperties") is False:
            props = schema.get("properties", {})
            unknown = [key for key in obj if key not in props]
            if unknown:
                raise self.error_cls("bad_input",
                                     f"未知参数：{', '.join(unknown)}；可用：{', '.join(props) or '无'}")
