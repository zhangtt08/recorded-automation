"""本机 Agent 服务的防护闸门回归（评审 BLOCKER #1）。

评审的事实：`ra/agentapi.py` 原来给每个响应都发 `access-control-allow-origin: *`，并且没有任何
Host / Origin / 令牌校验，`agent/server.py` 同形。于是**用户访问的任意网页**都能驱动全部 25 个工具
（`ra.record_start` 指任意 URL + `ra.draft` = 对着装有真实登录态的档案敲键），唯一的"闸门"
`_require_confirm` 只要请求体里写 `"confirm": true` —— 那个值是攻击者控制的。

这里每条断言都对着上面一句话：

1. 伪造的 `Host` 一律拒（含 DNS rebinding 形状：Host 与 Origin 同为攻击者域名 —— 拿两者相比的写法
   在这一条上必然放行，所以这条就是「不许比 Origin 和 Host」的实测）。
2. 跨源网页的 POST（`Origin: http://evil.test`）一律拒。
3. 没带令牌的写请求一律拒；带错令牌同样拒（401，且两者不可区分）。
4. 合法的本机路径（回环 Host + 从令牌文件读出来的令牌）真的能调成功。
5. 浏览器过不了预检：`OPTIONS` 与所有 mutating 响应都不带 `access-control-allow-origin`。
6. 独立进程 `agent/server.py` 走的是同一套闸门（真起子进程实测，不是读代码）。
7. 公示清单（作品集验收器读的那一份 `/api/agent/tools`）按标准第 4 条自查一遍：风险档合法，
   被要求 confirm 的工具必须把 confirm 同时放在 properties 与 required 里（缺省不拒绝等于没有闸门）。

服务只在临时数据目录里跑（`RA_AGENT_TOKEN_FILE` / `RA_AGENT_ENDPOINT_FILE` / `LOCALAPPDATA` 全部指到
tempdir），不碰本机真实的 %LOCALAPPDATA%\\RecordedAutomation，也不启动任何浏览器。
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ra import localguard                       # noqa: E402
from ra.agentapi import AgentServer             # noqa: E402

LOOP = "127.0.0.1"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOP, 0))
        return int(sock.getsockname()[1])


def request(port: int, path: str, *, method: str = "GET", host: str | None = None,
            origin: str | None = None, referer: str | None = None,
            token: str | None = None, body: dict | None = None) -> tuple[int, dict, dict]:
    """低层 HTTP 客户端：Host 头要能自己伪造，urllib 做不到。"""
    headers = {"connection": "close"}
    headers["host"] = host if host is not None else f"{LOOP}:{port}"
    if origin is not None:
        headers["origin"] = origin
    if referer is not None:
        headers["referer"] = referer
    if token is not None:
        headers[localguard.TOKEN_HEADER] = token
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    conn = http.client.HTTPConnection(LOOP, port, timeout=15)
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = {"_raw": raw.decode("utf-8", errors="replace")}
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, parsed


class GuardUnitTests(unittest.TestCase):
    """纯函数层：把「比 Origin 和 Host」这种写法当场判死。"""

    def test_loopback_hosts_pass_and_nothing_else_does(self):
        self.assertTrue(localguard.host_allowed("127.0.0.1:8795", 8795))
        self.assertTrue(localguard.host_allowed("localhost:8795", 8795))
        self.assertTrue(localguard.host_allowed("[::1]:8795", 8795))
        self.assertTrue(localguard.host_allowed("127.0.0.1", 8795))         # 缺省端口可以
        for forged in ("evil.test:8795", "localhost:1", "127.0.0.1:1", "0.0.0.0:8795",
                       "[::ffff:127.0.0.1]:8795", "127.1:8795", "127.0.0.2:8795", "", None):
            self.assertFalse(localguard.host_allowed(forged, 8795), f"该拒的放行了：{forged}")

    def test_dns_rebinding_shape_is_refused_even_when_origin_matches_host(self):
        """把 Origin 和本次请求的 Host 相比 = 没有防护：rebinding 时两者天然相等。"""
        headers = {"host": "rebind.attacker.test:8795", "origin": "http://rebind.attacker.test:8795"}
        self.assertEqual(headers["origin"].split("//")[1], headers["host"])   # 相等，比了就会放行
        with self.assertRaises(localguard.GuardError) as caught:
            localguard.check_headers(headers, 8795)
        self.assertEqual(caught.exception.code, "host_not_allowed")

    def test_origin_and_referer_must_be_loopback_or_absent(self):
        self.assertTrue(localguard.origin_allowed(None))                     # 非浏览器客户端不发
        self.assertTrue(localguard.origin_allowed("http://127.0.0.1:51234"))
        self.assertTrue(localguard.origin_allowed("http://localhost:3000"))
        self.assertFalse(localguard.origin_allowed("http://evil.test"))
        self.assertFalse(localguard.origin_allowed("chrome-extension://abcdefghijklmnop"))
        self.assertFalse(localguard.origin_allowed("null"))                  # 沙箱 iframe 的 Origin
        headers = {"host": "127.0.0.1:8795", "origin": "http://evil.test"}
        with self.assertRaises(localguard.GuardError) as caught:
            localguard.check_headers(headers, 8795)
        self.assertEqual(caught.exception.code, "origin_not_allowed")
        headers = {"host": "127.0.0.1:8795", "referer": "http://evil.test/poke"}
        with self.assertRaises(localguard.GuardError) as caught:
            localguard.check_headers(headers, 8795)
        self.assertEqual(caught.exception.code, "origin_not_allowed")

    def test_token_is_required_on_every_non_get_and_get_is_open(self):
        localguard.check_token("GET", {}, "whatever")                        # 不抛
        with self.assertRaises(localguard.GuardError) as missing:
            localguard.check_token("POST", {}, "whatever")
        self.assertEqual(missing.exception.code, "token_required")
        self.assertEqual(missing.exception.status, 401)
        with self.assertRaises(localguard.GuardError) as wrong:
            localguard.check_token("POST", {localguard.TOKEN_HEADER: "guess"}, "whatever")
        self.assertEqual(wrong.exception.code, "token_required")             # 缺与错不可区分
        localguard.check_token("POST", {localguard.TOKEN_HEADER: "whatever"}, "whatever")
        localguard.check_token("POST", {"authorization": "Bearer whatever"}, "whatever")
        # 头名不区分大小写（浏览器与 curl 的写法各不相同）
        localguard.check_token("POST", {localguard.TOKEN_HEADER.upper(): "whatever"}, "whatever")

    def test_token_file_is_persisted_not_rotated_per_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "agent-token"
            with mock.patch.dict(os.environ, {localguard.TOKEN_FILE_ENV: str(path), localguard.TOKEN_ENV: ""}):
                first, where = localguard.load_token()
                again, _ = localguard.load_token()
                self.assertEqual(str(where), str(path))
                self.assertEqual(first, again, "进程内服务与独立服务必须共用同一份令牌")
                self.assertGreaterEqual(len(first), 32)
                self.assertTrue(localguard.TOKEN_RE.fullmatch(first), first)
                self.assertEqual(path.read_text(encoding="utf-8").strip(), first)
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("\n\n", text)

    def test_env_token_overrides_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {localguard.TOKEN_ENV: "a" * 40,
                                              localguard.TOKEN_FILE_ENV: str(Path(tmp) / "agent-token")}):
                token, _ = localguard.load_token()
                self.assertEqual(token, "a" * 40)
                self.assertFalse((Path(tmp) / "agent-token").exists())       # 指定了就不落盘


class InProcessApiGuardTests(unittest.TestCase):
    """进程内服务（界面那一个）：真实 HTTP 请求打进去。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ra-guard-"))
        cls.env = mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(cls.tmp / "localappdata"),
            localguard.TOKEN_FILE_ENV: str(cls.tmp / "agent-token"),
            localguard.ENDPOINT_FILE_ENV: str(cls.tmp / ".endpoint"),
            localguard.TOKEN_ENV: "",
        })
        cls.env.start()
        cls.port = free_port()
        cls.server = AgentServer(api=None, session=None, port=cls.port).start()
        if cls.server.error:
            cls.env.stop()
            raise AssertionError(f"进程内 Agent 服务起不来：{cls.server.error}")
        # 令牌只从文件读（和 MCP 桥一样的取法）。文件不存在 = 这一版还没有防护，
        # 那正是要判红的地方：别让夹具先炸掉，把失败留给下面每一条断言去说。
        path = Path(os.environ[localguard.TOKEN_FILE_ENV])
        cls.token = path.read_text(encoding="utf-8").strip() if path.exists() else ""

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.env.stop()

    # -- 1. Host ---------------------------------------------------------
    def test_spoofed_host_is_refused(self):
        for forged in ("evil.test", f"evil.test:{self.port}", "localhost:1", "0.0.0.0", "127.0.0.2"):
            status, _headers, body = request(self.port, "/api/health", host=forged)
            self.assertEqual(status, 403, f"伪造 Host {forged} 居然过了")
            self.assertEqual(body.get("error", {}).get("code"), "host_not_allowed")

    def test_rebinding_pair_of_host_and_origin_is_refused(self):
        status, _h, body = request(self.port, "/api/health", host=f"rebind.evil.test:{self.port}",
                                   origin="http://rebind.evil.test")
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error", {}).get("code"), "host_not_allowed")
        status, _h, body = request(self.port, "/api/agent/tool", method="POST",
                                   host=f"rebind.evil.test:{self.port}",
                                   origin=f"http://rebind.evil.test:{self.port}",
                                   token=self.token, body={"tool": "ra.status", "input": {}})
        self.assertEqual(status, 403, "带令牌也不该让 rebinding 的 Host 过去")

    # -- 2. Origin / Referer ----------------------------------------------
    def test_cross_origin_post_is_refused(self):
        for origin in ("http://evil.test", "https://evil.test", "chrome-extension://abcdefghijklmnop", "null"):
            status, _h, body = request(self.port, "/api/agent/tool", method="POST", origin=origin,
                                       token=self.token, body={"tool": "ra.status", "input": {}})
            self.assertEqual(status, 403, f"{origin} 的跨源 POST 居然过了")
            self.assertEqual(body.get("error", {}).get("code"), "origin_not_allowed")

    def test_cross_origin_get_is_refused_too(self):
        status, _h, body = request(self.port, "/api/agent/tools", origin="http://evil.test")
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error", {}).get("code"), "origin_not_allowed")

    # -- 3. 令牌 -----------------------------------------------------------
    def test_post_without_token_is_refused(self):
        status, headers, body = request(self.port, "/api/agent/tool", method="POST",
                                        body={"tool": "ra.status", "input": {}})
        self.assertEqual(status, 401)
        self.assertEqual(body.get("error", {}).get("code"), "token_required")
        self.assertNotIn("*", headers.get("access-control-allow-origin", ""))

    def test_post_with_wrong_token_is_refused_the_same_way(self):
        status, _h, body = request(self.port, "/api/agent/tool", method="POST", token="guess-me-not",
                                   body={"tool": "ra.status", "input": {}})
        self.assertEqual(status, 401)
        self.assertEqual(body.get("error", {}).get("code"), "token_required")

    def test_every_registered_write_tool_is_unreachable_without_a_token(self):
        """评审点名的高危工具：ra.record_start / ra.draft / ra.set_secret / ra.run_workflow / ra.delete_workflow。
        不带令牌的跨源网页现在连闸门都过不了 —— 一个都不例外。

        input 一律传空对象：闸门在参数校验**之前**，所以带修复的版本回 401；
        而万一防护没生效，空参数只会得到 bad_input，不会真的去开浏览器录一个陌生 URL。
        """
        cases = ("ra.record_start", "ra.draft", "ra.set_secret", "ra.run_workflow", "ra.delete_workflow")
        status, _h, listed = request(self.port, "/api/agent/tools")
        self.assertEqual(status, 200)
        registered = {item["name"] for item in listed["data"]}
        self.assertTrue(set(cases) <= registered, f"评审点的工具不在这份清单里：{registered - set(cases)}")
        for name in cases:
            status, _h, body = request(self.port, "/api/agent/tool", method="POST",
                                       body={"tool": name, "input": {}})
            self.assertEqual((status, body.get("error", {}).get("code")), (401, "token_required"),
                             f"{name} 在没带令牌时被调动了：HTTP {status} {json.dumps(body, ensure_ascii=False)[:120]}")
            status, _h, body = request(self.port, "/api/agent/tool", method="POST", origin="http://evil.test",
                                       token=self.token or "no-guard", body={"tool": name, "input": {}})
            self.assertEqual((status, body.get("error", {}).get("code")), (403, "origin_not_allowed"),
                             f"{name} 被跨源网页调动了：HTTP {status}")

    # -- 4. 合法本机路径 ----------------------------------------------------
    def test_legit_local_call_from_token_file_succeeds(self):
        """与 MCP 桥同样的路径：回环 Host + 令牌文件里的那一串。"""
        status, _h, body = request(self.port, "/api/agent/tool", method="POST", token=self.token,
                                   body={"tool": "ra.status", "input": {}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "ra.status")
        self.assertIn("data_dir", body["data"])
        # 隔离生效：绝不能读到本机真实的 %LOCALAPPDATA%
        self.assertEqual(Path(body["data"]["data_dir"]), Path(os.environ["LOCALAPPDATA"]) / "RecordedAutomation")

    def test_legit_get_needs_no_token(self):
        for path in ("/api/health", "/api/agent/tools", "/api/agent/manifest"):
            status, _h, body = request(self.port, path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body["ok"], path)
        status, _h, body = request(self.port, "/api/health")
        self.assertEqual(body["data"]["auth"], "token")
        self.assertEqual(body["data"]["token_file"], os.environ[localguard.TOKEN_FILE_ENV])
        self.assertNotIn(self.token, json.dumps(body["data"]), "健康检查绝不能把令牌本身吐出来")

    # -- 5. 预检 / CORS -----------------------------------------------------
    def test_no_wildcard_acao_on_mutating_routes(self):
        for kwargs in ({"method": "POST", "body": {"tool": "ra.status", "input": {}}},
                       {"method": "POST", "token": self.token, "body": {"tool": "ra.status", "input": {}}},
                       {"method": "POST", "host": "evil.test"},
                       {"method": "GET"}):
            _status, headers, _body = request(self.port, "/api/agent/tool", **kwargs)
            self.assertNotIn("access-control-allow-origin", headers, f"又发通配 CORS 了：{kwargs}")

    def test_browser_preflight_cannot_open_the_door(self):
        """网页要先过预检才发得出带令牌的跨源请求；预检不给任何 ACAO，请求根本出不了门。"""
        status, headers, _body = request(
            self.port, "/api/agent/tool", method="OPTIONS", origin="http://evil.test",
            host=f"{LOOP}:{self.port}",
            body=None)
        self.assertIn(status, (204, 403))
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertNotIn("access-control-allow-methods", headers)
        self.assertNotIn("access-control-allow-headers", headers)

    def test_denied_requests_are_counted_where_the_ui_can_see_them(self):
        before = self.server.denied
        request(self.port, "/api/agent/tool", method="POST", body={"tool": "ra.status", "input": {}})
        request(self.port, "/api/health", host="evil.test")
        self.assertGreaterEqual(self.server.denied, before + 2)
        self.assertIn("token_file", self.server.info())

    def test_endpoint_file_stays_a_single_url_line(self):
        """personal-agent-hub 的脚本按「整文件 trim 出来就是 URL」读它，令牌绝不能塞进第二行。"""
        raw = Path(os.environ[localguard.ENDPOINT_FILE_ENV]).read_text(encoding="utf-8")
        self.assertEqual(raw.strip(), f"http://{LOOP}:{self.port}")
        self.assertNotIn(self.token, raw)

    # -- 6. 公示的风险档与 confirm（作品集验收器读的就是这份清单） -------------
    def test_the_published_manifest_satisfies_standard_rule4(self):
        """作品集验收器打的是 GET /api/agent/tools 这一份，不是源码里的那张表。

        所以 AGENT_API_STANDARD 第 4 条的判据（exec 必须有 confirm 且缺省拒绝；write 只在名字或
        描述含破坏性动词时强制）就在这条真实 HTTP 返回上跑一遍：红要红在本仓库。
        本轮的由来正是这里 —— `ra.record_stop` 公示的是 exec，可它既不写盘、不起进程也不驱动页面，
        只是把自己那一场录制收尾并把草稿原样交回来，于是验收器点名「exec 工具没有 confirm 入参」。
        改的是这一边的标签（exec → write），不是判据，也不是给不需要的地方补一个 confirm。
        """
        import re
        destructive = re.compile(
            r"\b(delete|remove|purge|clear|reset|overwrite|drop|unlink|erase)\b|删除|清空|覆盖|重置", re.I)
        status, _h, listed = request(self.port, "/api/agent/tools")
        self.assertEqual(status, 200)
        rows = listed["data"]
        self.assertEqual({row["name"] for row in rows}, {tool["name"] for tool in self.server.tools},
                         "公示的工具名与注册表不一致")
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["ra.record_stop"]["risk"], "write",
                         "风险档又回到 exec：按标准那就必须有 confirm，本轮的结论被推翻")
        self.assertNotIn("confirm", by_name["ra.record_stop"]["input_schema"].get("properties") or {},
                         "给可逆小写入挂 confirm，是把调用方训练成无脑传 true 的起点")
        offenders = []
        for row in rows:
            self.assertIn(row.get("risk"), {"read", "write", "exec"}, f"{row['name']} 的 risk 不合法")
            properties = (row.get("input_schema") or {}).get("properties") or {}
            required = (row.get("input_schema") or {}).get("required") or []
            demanded = (row["risk"] == "exec"
                        or (row["risk"] == "write"
                            and bool(destructive.search(f"{row['name']} {row.get('description', '')}"))))
            if demanded and ("confirm" not in properties or "confirm" not in required):
                offenders.append(f"{row['name']}（{row['risk']}）")
        self.assertEqual(offenders, [], f"验收器会点名的工具：{offenders}")
        # 另一个出口必须给出同一份风险档，否则就是第二份会各自漂移的判据
        status, _h, manifest = request(self.port, "/api/agent/manifest")
        self.assertEqual({row["name"]: row["risk"] for row in manifest["data"]["tools"]},
                         {row["name"]: row["risk"] for row in rows})


class StandaloneServerGuardTests(unittest.TestCase):
    """独立进程 agent/server.py：同一个洞的另一半，必须一起补。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ra-guard-standalone-"))
        cls.port = free_port()
        cls.env = dict(os.environ)
        cls.env.update({
            "LOCALAPPDATA": str(cls.tmp / "localappdata"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "AGENT_PORT": str(cls.port),
            localguard.TOKEN_FILE_ENV: str(cls.tmp / "agent-token"),
            localguard.ENDPOINT_FILE_ENV: str(cls.tmp / ".endpoint"),
        })
        cls.env.pop(localguard.TOKEN_ENV, None)
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "agent" / "server.py")],
                                    cwd=str(ROOT), env=cls.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace")
        cls.base_ok = cls._wait_ready()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    @classmethod
    def _wait_ready(cls) -> bool:
        deadline = time.time() + 30
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                return False
            try:
                status, _h, body = request(cls.port, "/api/health")
                if status == 200 and body.get("ok"):
                    return True
            except (OSError, http.client.HTTPException):
                time.sleep(0.3)
            time.sleep(0.3)
        return False

    def test_standalone_server_came_up(self):
        self.assertTrue(self.base_ok, "独立 Agent 服务没起来（后面所有断言都无从谈起）")

    def _token(self) -> str | None:
        path = Path(self.env[localguard.TOKEN_FILE_ENV])
        text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        return text or None          # 没有令牌文件（= 还没有防护）时不带头，让断言自己去红

    def test_standalone_requires_the_same_token(self):
        status, _h, body = request(self.port, "/api/agent/tool", method="POST",
                                   body={"tool": "ra.status", "input": {}})
        self.assertEqual((status, body.get("error", {}).get("code")), (401, "token_required"),
                         f"没带令牌也调得动：HTTP {status} {json.dumps(body, ensure_ascii=False)[:160]}")
        status, _h, body = request(self.port, "/api/agent/tool", method="POST", token=self._token(),
                                   body={"tool": "ra.status", "input": {}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])

    def test_standalone_refuses_forged_host_and_cross_origin(self):
        status, _h, body = request(self.port, "/api/health", host="evil.test")
        self.assertEqual((status, body.get("error", {}).get("code")), (403, "host_not_allowed"),
                         f"伪造 Host 也过了：HTTP {status}")
        status, _h, body = request(self.port, "/api/agent/tool", method="POST", origin="http://evil.test",
                                   token=self._token(), body={"tool": "ra.status", "input": {}})
        self.assertEqual((status, body.get("error", {}).get("code")), (403, "origin_not_allowed"),
                         f"跨源 POST 也过了：HTTP {status}")

    def test_standalone_sends_no_wildcard_cors(self):
        for kwargs in ({}, {"token": self._token()}):
            status, headers, _body = request(self.port, "/api/agent/tool", method="POST",
                                             body={"tool": "ra.status", "input": {}}, **kwargs)
            self.assertNotIn("access-control-allow-origin", headers, f"{status} 还带通配 CORS")


if __name__ == "__main__":
    unittest.main()
