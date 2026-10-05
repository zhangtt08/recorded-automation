"""本机 Agent 服务的共用防护（personal-agent-hub/docs/AGENT_API_STANDARD.md 的「本地服务标准」）。

为什么要有这一份：`ra/agentapi.py`（界面进程内服务）与 `agent/server.py`（独立进程服务）是同一个
契约的两个入口，防护必须完全一致，否则攻击者只要挑另一个没设防的入口就行。所以判据只写在这里一处。

四道闸门（顺序即执行顺序）：

1. **只绑回环**（调用方负责，见两个服务里的 `127.0.0.1`）。本模块不参与绑定。
2. **`Host` 必须是回环名**：`127.0.0.1:<port>` / `localhost:<port>` / `[::1]:<port>`（端口可缺省，
   带了就必须是自己这一台）。这一条挡的是 DNS rebinding：攻击者把域名解析到 127.0.0.1 之后，
   浏览器发来的 `Host` 会是那个域名而不是 `127.0.0.1`。
   ⚠ **绝不拿 `Origin` 去和本次请求的 `Host` 比**——rebinding 场景下两者恰好相等，比了等于没防。
   `Origin`/`Referer` 只和「回环地址清单」比，参考的是常量而不是自己的 Host。
3. **`Origin` / `Referer` 不是回环就拒**（`{ok:false,error:{code:...}}` + HTTP 403）。
   非浏览器客户端（curl、node、MCP 桥）不发这两个头，照常放行。
4. **非 GET 必须带共享令牌**（`x-ra-token`，定时安全比较）。令牌是每次服务启动时从
   `token_file()` 读出来的本机秘密：谁读不到这个文件，谁就调用不了工具。

另外两条不属于本模块但必须一起成立：响应里**不再有 `access-control-allow-origin: *`**（任何路由都
不发通配 CORS），预检 `OPTIONS` 也不带 ACAO —— 浏览器页面连预检都过不去，`fetch` 在真正发请求之前
就被拦下，所以「写操作」不可能由跨源网页驱动（reviewer 说的 write-only CSRF 也一并挡掉）。

令牌存哪、为什么：默认 `%LOCALAPPDATA%\\RecordedAutomation\\agent-token`（非 Windows 走
`XDG_DATA_HOME`）。刻意**不放在仓库里**：仓库目录可能被同步、被打包、被 `git add -A`，而这个文件
的读取权限就是它的全部价值 —— Windows 上 %LOCALAPPDATA% 的 ACL 只给当前用户与管理员组，
POSIX 上再 `chmod 600`。要换位置用 `RA_AGENT_TOKEN_FILE`，要直接给定令牌用 `RA_AGENT_TOKEN`
（两者都是给测试与编排器留的口，正常运行不需要）。
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

TOKEN_HEADER = "x-ra-token"
TOKEN_ENV = "RA_AGENT_TOKEN"
TOKEN_FILE_ENV = "RA_AGENT_TOKEN_FILE"
ENDPOINT_FILE_ENV = "RA_AGENT_ENDPOINT_FILE"

#: 回环主机名清单。这是常量，永远不与请求里的 Host 相互参照。
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")

TOKEN_BYTES = 32
TOKEN_RE = re.compile(r"^[0-9a-f]{32,128}$")

_write_lock = threading.Lock()


class GuardError(Exception):
    """防护层的结构化错误：与 AgentError 同构（code + message），但状态码是 401/403。"""

    def __init__(self, code: str, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- Host / Origin
def _split_hostport(value: str) -> tuple[str, int | None]:
    """把 `Host` 头拆成 (主机部分, 端口或 None)。非法端口不抛异常，交给调用方判拒。"""
    raw = (value or "").strip()
    if not raw:
        return "", None
    if raw.startswith("["):                       # IPv6：[::1]:8795
        end = raw.find("]")
        if end < 0:
            return raw, -1
        host = raw[:end + 1]
        tail = raw[end + 1:]
        if not tail:
            return host, None
        if not tail.startswith(":"):
            return host, -1
        try:
            return host, int(tail[1:])
        except ValueError:
            return host, -1
    if raw.count(":") == 1:
        host, _, port = raw.partition(":")
        try:
            return host, int(port)
        except ValueError:
            return host, -1
    return raw, None


def host_allowed(value: str | None, port: int | None = None) -> bool:
    """`Host` 是否为回环名。端口缺省可以，带了就必须等于本服务自己的端口。"""
    host, given = _split_hostport(value or "")
    if host.lower() not in LOOPBACK_HOSTS:
        return False
    if given is None:
        return True
    if port is None:
        return given >= 0
    return given == int(port)


def _origin_host(value: str) -> tuple[str, int | None]:
    """从 `Origin`/`Referer` 里取主机与端口；取不出来就当作不可信。"""
    raw = (value or "").strip()
    if not raw:
        return "", None
    try:
        parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
    except ValueError:
        return "", None
    if parsed.scheme not in ("http", "https"):    # chrome-extension://null:// 之类一律不可信
        return "", None
    try:
        hostname = (parsed.hostname or "").lower()
        return f"[{hostname}]" if ":" in hostname else hostname, parsed.port
    except ValueError:
        return "", None


def origin_allowed(value: str | None) -> bool:
    """缺省（非浏览器客户端）放行；带了就必须是回环 http(s) 源。

    判据是回环清单这个常量，不是本次请求的 Host —— 后者正是 DNS rebinding 的漏洞所在。
    """
    if value is None or not (value or "").strip():
        return True
    host, _port = _origin_host(value)
    return host in LOOPBACK_HOSTS


def header_snapshot(headers: Any) -> dict[str, str]:
    """HTTP 头名不区分大小写。`self.headers`（http.server）与普通 dict 都收成小写键。"""
    snapshot: dict[str, str] = {}
    items = []
    try:
        items = list(headers.items())          # BaseHTTPRequestHandler.headers 与 dict 都有
    except AttributeError:
        pass
    for key, value in items:
        snapshot[str(key).lower()] = value
    return snapshot


def check_headers(headers: Any, port: int | None = None) -> None:
    """按标准顺序过闸门：Host → Origin → Referer。不合规抛 GuardError（403）。"""
    snapshot = header_snapshot(headers)
    if not host_allowed(snapshot.get("host"), port):
        raise GuardError(
            "host_not_allowed",
            "只接受回环地址的 Host 头（127.0.0.1 / localhost / [::1]）。"
            "看起来像 DNS rebinding 或端口扫描，请求已拒绝。", 403)
    for name in ("origin", "referer"):
        value = snapshot.get(name)
        if value and not origin_allowed(value):
            raise GuardError(
                "origin_not_allowed",
                f"{name.title()}: {str(value).strip()} 不是本机回环来源，本服务只允许本机程序调用。", 403)


# --------------------------------------------------------------------------- 令牌
def default_token_dir() -> Path:
    """与 `ra.paths.data_root()` 同一套解析，但**不建目录**（读令牌不该有副作用）。

    单独写一份是为了让 `agent/server.py` 之外的 Node 侧（MCP 桥）能按同样的规则算出路径：
    Windows = %LOCALAPPDATA%\\RecordedAutomation，其他系统 = $XDG_DATA_HOME 或 ~/.local/share。
    """
    from .paths import DATA_DIRNAME

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
    else:
        base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else (Path.home() / ".local" / "share")
    return root / DATA_DIRNAME


def token_file() -> Path:
    """令牌文件位置：`RA_AGENT_TOKEN_FILE` 优先，否则 `<本机数据目录>/agent-token`。"""
    override = os.environ.get(TOKEN_FILE_ENV)
    if override:
        return Path(override)
    return default_token_dir() / "agent-token"


def endpoint_file(default: Path) -> Path:
    """端点发现文件位置（默认在 agent/ 目录里，保持与旧版 `.endpoint` 同一形状）。

    测试与并行实例用 `RA_AGENT_ENDPOINT_FILE` 指到临时目录，避免覆盖正在运行的那一份。
    """
    override = os.environ.get(ENDPOINT_FILE_ENV)
    return Path(override) if override else default


def _read_token_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return text if TOKEN_RE.fullmatch(text) else ""


def _write_token_file(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with _write_lock:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass                      # Windows 上 chmod 只动只读位，真实边界是目录 ACL


def load_token(create: bool = True) -> tuple[str, Path]:
    """取出本机令牌；文件不存在（或内容坏了）时生成一份并落盘。

    进程内服务与独立服务共用同一个文件，所以「录放台开着 + 又起了一个 agent/server.py」
    用的是同一个令牌，谁也不用手工传。
    """
    env = (os.environ.get(TOKEN_ENV) or "").strip()
    path = token_file()
    if env and TOKEN_RE.fullmatch(env):
        return env, path
    stored = _read_token_file(path)
    if stored:
        return stored, path
    if not create:
        return "", path
    fresh = secrets.token_hex(TOKEN_BYTES)
    try:
        _write_token_file(path, fresh)
    except OSError:
        return fresh, path            # 落不了盘也要有一个进程内令牌：调用方拿不到它就调不动，
    return fresh, path                # 这比「存不进文件就退回无鉴权」诚实


def token_matches(candidate: str | None, expected: str) -> bool:
    """定时安全比较。缺令牌与错令牌给出同样的错误，避免探测。"""
    if not expected:
        return False
    return hmac.compare_digest(str(candidate or "").strip(), expected)


def check_token(method: str, headers: Any, expected: str) -> None:
    """非 GET 必须带正确的 `x-ra-token`（也接受 `authorization: bearer <token>`）。"""
    if (method or "GET").upper() == "GET":
        return
    snapshot = header_snapshot(headers)
    given = snapshot.get(TOKEN_HEADER) or ""
    auth = str(snapshot.get("authorization") or "")
    if not given and auth.lower().startswith("bearer "):
        given = auth[7:]
    if not token_matches(given, expected):
        raise GuardError(
            "token_required",
            "这个服务的所有写操作都要带本机共享令牌：请求头 "
            f"{TOKEN_HEADER}: <{token_file()} 里的内容>。"
            "合法的本机 Agent（MCP 桥）会自动读那个文件，不需要手工传。", 401)


# --------------------------------------------------------------------------- 响应外壳
def guard_payload(error: GuardError, endpoint_hint: str = "") -> dict[str, Any]:
    """拒绝时的 JSON 外壳：与契约的 {ok:false,error:{code,message}} 同形，外加出路。"""
    body: dict[str, Any] = {"ok": False, "error": {"code": error.code, "message": error.message}}
    if endpoint_hint:
        body["error"]["hint"] = endpoint_hint
    return body


def json_bytes(body: dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
