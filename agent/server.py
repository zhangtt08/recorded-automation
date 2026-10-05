#!/usr/bin/env python3
"""Agent API server（标准实现，Python 项目用）。项目只需在同目录提供 tools.py 并导出 TOOLS / PROJECT。

契约见 personal-agent-hub/docs/AGENT_API_STANDARD.md。只用标准库，只监听 127.0.0.1。

⚠ 相对标准模板的有意偏离（本项目验收要求，别在同步模板时改回去）：
1. 加了 `ra/localguard.py` 的四道闸门（Host 必须是回环名、Origin/Referer 必须匹配本机、
   所有非 GET 必带本机共享令牌、任何响应都不发通配 CORS）。模板原来发的是
   `access-control-allow-origin: *`，等于允许用户访问的任意网页驱动本项目全部工具。
2. 默认端口 8790 → 8795（AGENT_API_STANDARD 给录放台分配的那一个）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from errors import AgentError  # noqa: E402  与 tools.py 共用同一个类对象
from ra import localguard      # noqa: E402  与进程内服务（ra/agentapi.py）共用同一份防护判据

START = time.time()
MAX_BODY = 8 * 1024 * 1024


def load_tools() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    p = HERE / "tools.py"
    if not p.exists():
        raise SystemExit(f"missing {p}: 项目必须实现 agent/tools.py")
    spec = importlib.util.spec_from_file_location("agent_tools", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return getattr(mod, "PROJECT", {"name": "agent", "version": "0.0.0"}), list(getattr(mod, "TOOLS"))


def validate(schema: dict[str, Any] | None, data: Any) -> None:
    if not schema or schema.get("type") != "object":
        return
    obj = data if isinstance(data, dict) else {}
    missing = [k for k in schema.get("required", []) if obj.get(k) in (None, "")]
    if missing:
        raise AgentError("bad_input", "缺少必填参数：" + ", ".join(missing))
    if schema.get("additionalProperties") is False:
        props = schema.get("properties", {})
        unknown = [k for k in obj if k not in props]
        if unknown:
            raise AgentError("bad_input", f"未知参数：{', '.join(unknown)}；可用：{', '.join(props) or '无'}")


def serve(project: dict[str, Any], tools: list[dict[str, Any]], port: int = 8795, host: str = "127.0.0.1") -> None:
    by_name = {t["name"]: t for t in tools}
    descriptor = lambda t: {"name": t["name"], "description": t["description"], "input_schema": t.get("input_schema", {}), "risk": t.get("risk", "read")}
    token, token_path = localguard.load_token()
    denied = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "RecordedAutomation-Agent/1"

        def _send(self, status: int, body: dict[str, Any]) -> None:
            raw = localguard.json_bytes(body)
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(raw)))
            # 没有 access-control-allow-origin：这个端点只给本机程序用（与 ra/agentapi.py 一致）
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _drain(self) -> None:
            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                length = 0
            if length > 0:
                try:
                    self.rfile.read(min(length, MAX_BODY))
                except OSError:
                    pass

        def _deny(self, error: localguard.GuardError) -> None:
            denied["n"] += 1
            self._drain()
            self.close_connection = True
            self._send(error.status, localguard.guard_payload(
                error, endpoint_hint="本机 Agent 请读 agent/.endpoint 与令牌文件；见 agent/README.md"))

        def _guard(self) -> bool:
            try:
                localguard.check_headers(self.headers, bound_port)
                localguard.check_token(self.command, self.headers, token)
            except localguard.GuardError as error:
                self._deny(error)
                return False
            return True

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._drain()
            self.send_response(204)
            self.send_header("content-length", "0")
            self.send_header("cache-control", "no-store")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            route = self.path.split("?")[0].rstrip("/") or "/"
            if route == "/api/health":
                self._send(200, {"ok": True, "data": {"project": project["name"], "version": project.get("version", "0.0.0"), "agent_api": 1, "tools": len(tools), "auth": "token", "token_header": localguard.TOKEN_HEADER, "token_file": str(token_path), "denied": denied["n"], "uptime_ms": int((time.time() - START) * 1000)}})
            elif route == "/api/agent/tools":
                self._send(200, {"ok": True, "data": [descriptor(t) for t in tools]})
            elif route == "/api/agent/manifest":
                self._send(200, {"ok": True, "data": {"project": project["name"], "version": project.get("version", "0.0.0"), "description": project.get("summary", ""), "base_url": f"http://{host}:{bound_port}", "tools": [descriptor(t) for t in tools], "api": {"token_header": localguard.TOKEN_HEADER, "token_env": localguard.TOKEN_ENV, "token_file": str(token_path)}}})
            else:
                self._send(404, {"ok": False, "error": {"code": "not_found", "message": f"未知路径 {route}", "endpoints": ["/api/health", "/api/agent/tools", "/api/agent/manifest", "POST /api/agent/tool"]}})

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            route = self.path.split("?")[0].rstrip("/") or "/"
            if route != "/api/agent/tool":
                self._drain()
                self._send(404, {"ok": False, "error": {"code": "not_found", "message": f"未知路径 {route}"}})
                return
            try:
                length = int(self.headers.get("content-length") or 0)
                if length > MAX_BODY:
                    raise AgentError("too_large", "请求体超过 8MB")
                body = json.loads(self.rfile.read(length) or b"{}")
                tool = by_name.get(body.get("tool"))
                if tool is None:
                    self._send(400, {"ok": False, "error": {"code": "unknown_tool", "message": f"未注册的工具：{body.get('tool')}", "available": list(by_name)}})
                    return
                t0 = time.time()
                validate(tool.get("input_schema"), body.get("input"))
                fn: Callable[..., Any] = tool["handler"]
                data = fn(body.get("input") or {})
                self._send(200, {"ok": True, "tool": tool["name"], "ms": int((time.time() - t0) * 1000), "data": data})
            except AgentError as e:
                status = 400 if e.code == "bad_input" else 500
                self._send(status, {"ok": False, "error": {"code": e.code, "message": str(e)}})
            except json.JSONDecodeError:
                self._send(400, {"ok": False, "error": {"code": "bad_json", "message": "请求体不是合法 JSON"}})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"ok": False, "error": {"code": "handler_failed", "message": f"{type(e).__name__}: {e}"}})

        def log_message(self, *_a: Any) -> None:
            pass

    bound = None
    for p in range(port, port + 12):
        try:
            httpd = ThreadingHTTPServer((host, p), Handler)
            bound = p
            break
        except OSError:
            continue
    if bound is None:
        raise SystemExit(f"端口 {port}~{port+11} 全部被占用")
    # 闭包里的 bound_port 必须在任何请求进来之前就位（serve_forever 还没启动，这里赋值是安全的）
    bound_port = bound
    endpoint = localguard.endpoint_file(HERE / ".endpoint")
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    endpoint.write_text(f"http://{host}:{bound}\n", encoding="utf-8")
    # 控制台可能是 GBK，中文会乱码甚至抛 UnicodeEncodeError，因此启动行只用 ASCII。
    print(f"[agent] {project['name']} v{project.get('version','0.0.0')} -> http://{host}:{bound} ({len(tools)} tools) auth=token", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    proj, tl = load_tools()
    # 与标准模板唯一的差别就是这一处默认端口：AGENT_API_STANDARD 给录放台分配的是 8795。
    serve(proj, tl, int(os.environ.get("AGENT_PORT", "8795")))
