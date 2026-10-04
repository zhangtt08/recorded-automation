"""Agent API 层的回归：工具清单、秘密值不外泄、写入/执行必须 confirm、AgentError 类身份一致。

用临时 LOCALAPPDATA 起服务侧的工具层，不碰本机真实的录放台数据目录，也不启动浏览器。
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent"
sys.path.insert(0, str(AGENT))

from errors import AgentError                                  # noqa: E402  与 server.py 同一份


def load_tools():
    spec = importlib.util.spec_from_file_location("agent_tools_probe", AGENT / "tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToolRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous = os.environ.get("LOCALAPPDATA")
        cls.root = Path(tempfile.mkdtemp(prefix="ra-agent-"))
        os.environ["LOCALAPPDATA"] = str(cls.root)
        from ra.paths import data_root

        cls.data = Path(data_root())            # 工具读的是这个隔离目录，不是本机真实的录放台数据
        cls.tools = load_tools()

    @classmethod
    def tearDownClass(cls):
        if cls.previous is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = cls.previous

    def names(self):
        return [tool["name"] for tool in self.tools.TOOLS]

    def by_name(self, name):
        return next(tool for tool in self.tools.TOOLS if tool["name"] == name)

    def test_project_identity_and_prefix(self):
        self.assertEqual(self.tools.PROJECT["name"], "recorded-automation")
        self.assertTrue(all(name.startswith("ra.") for name in self.names()), self.names())
        self.assertGreaterEqual(len(self.names()), 20, "交付版要覆盖界面能做的每一件事")
        self.assertEqual(len(self.names()), len(set(self.names())), "工具名不能重复")

    def test_every_tool_is_documented_enough_for_an_agent_to_call_blind(self):
        for tool in self.tools.TOOLS:
            self.assertTrue(len(tool["description"]) >= 20, tool["name"])
            self.assertIn(tool.get("risk"), {"read", "write", "exec"}, tool["name"])
            schema = tool.get("input_schema")
            self.assertIsInstance(schema, dict, tool["name"])
            if schema.get("required"):
                for key in schema["required"]:
                    self.assertIn(key, schema.get("properties", {}), f"{tool['name']} 要求 {key} 却没写它的说明")

    def test_agent_error_class_is_shared_with_the_server(self):
        # importlib 二次加载会让 AgentError 变成两个类对象，server 就捕不到 handler 抛的错。
        self.assertIs(self.tools.AgentError, AgentError)

    def test_risk_labels_are_honest(self):
        risks = {tool["name"]: tool["risk"] for tool in self.tools.TOOLS}
        self.assertEqual(risks["ra.save_workflow"], "write")
        self.assertEqual(risks["ra.run_workflow"], "exec")
        self.assertEqual(risks["ra.record_start"], "exec")
        # ra.record_stop 不是 exec：按标准的判据（真的跑活 / 真的写盘 / 真的起进程）它三样都不做，
        # 它只是把自己那一场录制收尾并把草稿原样交回来。标成 exec 就是标签说谎，
        # 逼着一个不需要的 confirm 出现在调用里（见 test_confirm_demanded_exactly_where_rule4_says）。
        self.assertEqual(risks["ra.record_stop"], "write")
        self.assertEqual(risks["ra.cancel_run"], "exec")
        self.assertEqual(risks["ra.delete_workflow"], "write")
        self.assertEqual(risks["ra.set_secret"], "write")
        for name in ("ra.status", "ra.list_workflows", "ra.get_workflow", "ra.validate_workflow",
                     "ra.run_history", "ra.list_secret_refs", "ra.workflow_schema", "ra.app_state",
                     "ra.draft", "ra.run_status"):
            self.assertEqual(risks[name], "read", name)

    def test_confirm_demanded_exactly_where_rule4_says(self):
        """作品集验收器（personal-agent-hub/scripts/verify-agent-apis.mjs）打的是这份表。

        判据照 AGENT_API_STANDARD 第 4 条原样实现，一条都不放宽：exec 必须有 confirm 且缺省拒绝
        （光有个可选键等于没有闸门）；write 只在名字或描述含破坏性动词时强制。
        红要红在本仓库里，而不是等验收器报「exec/破坏性 write 工具没有 confirm 入参」。
        """
        import re
        destructive = re.compile(
            r"\b(delete|remove|purge|clear|reset|overwrite|drop|unlink|erase)\b|删除|清空|覆盖|重置", re.I)
        flagged = []
        for tool in self.tools.TOOLS:
            risk = tool.get("risk")
            self.assertIn(risk, {"read", "write", "exec"}, f"{tool['name']} 的 risk 不在标准允许的三档里")
            schema = tool.get("input_schema", {})
            properties = schema.get("properties") or {}
            required = schema.get("required") or []
            needs = (risk == "exec"
                     or (risk == "write" and bool(destructive.search(f"{tool['name']} {tool.get('description', '')}"))))
            if needs:
                flagged.append(tool["name"])
                self.assertIn("confirm", properties, f"{tool['name']}（{risk}）按第 4 条必须有 confirm 入参")
                self.assertIn("confirm", required,
                              f"{tool['name']} 的 confirm 只是可选键：缺省不拒绝就等于闸门形同没有")
        # 反向也要成立，否则这条判据是可以被「全都加 confirm」糊过去的：
        # record_stop 是这一轮改档的那一个，它必须既不要 confirm 也不装作要。
        stop = self.by_name("ra.record_stop")
        self.assertNotIn("confirm", stop["input_schema"].get("properties") or {},
                         "给可逆小写入挂 confirm，就是把调用方训练成无脑传 true")
        self.assertNotIn("ra.record_stop", flagged,
                         "record_stop 又被当成 exec / 破坏性写入了：先改标签，别改判据")

    def test_record_stop_is_not_a_confirm_gate_and_writes_nothing(self):
        """两个方向都量：exec 那一面缺 confirm 真的被拒；record_stop 这一面失败只可能来自状态。"""
        with self.assertRaises(AgentError) as started:
            self.by_name("ra.record_start")["handler"]({"url": "https://example.test"})
        self.assertEqual(started.exception.code, "bad_input")
        self.assertIn("confirm", str(started.exception))

        with self.assertRaises(AgentError) as stopped:
            self.by_name("ra.record_stop")["handler"]({})       # 不带 confirm 也照样走到真实能力那一步
        self.assertEqual(stopped.exception.code, "stop_failed", str(stopped.exception))
        self.assertIn("没有进行中的录制", str(stopped.exception))

    def test_stop_recording_touches_no_disk_and_opens_no_browser(self):
        """「不写盘、不起进程」写在 docstring 里不算数，前后各数一遍目录。"""
        from ra.main import build

        def listing(root: Path) -> list[tuple[str, int, int]]:
            return sorted((str(item.relative_to(root)), item.stat().st_size, item.stat().st_mtime_ns)
                          for item in root.rglob("*") if item.is_file())

        root = Path(tempfile.mkdtemp(prefix="ra-record-stop-"))
        api, session, _ = build(root)
        try:
            before = listing(root)
            result = api.stop_recording()
            self.assertTrue(result.get("error"), f"没有录制时不该假装停止成功：{result}")
            self.assertIn("录制", result["error"])
            self.assertFalse(session.snapshot()["browser_open"], "收尾一场录制不需要把受控浏览器开起来")
            self.assertEqual(listing(root), before, "ra.record_stop 这条路上写了本机文件")
        finally:
            session.shutdown()

    def test_destructive_tools_exist_but_each_one_demands_confirm(self):
        """交付版把界面能做的都开放给 Agent：删除类也在内，但每一个都必须显式 confirm:true。"""
        for name in ("ra.delete_workflow", "ra.set_secret", "ra.delete_secret", "ra.cancel_run",
                     "ra.save_workflow", "ra.run_workflow", "ra.record_start", "ra.edit_step",
                     "ra.move_step", "ra.remove_step", "ra.add_step", "ra.save_draft"):
            tool = self.by_name(name)
            self.assertIn("confirm", tool["input_schema"].get("required", []), name)

    def test_write_and_exec_need_explicit_confirm(self):
        payload = {"schema_version": 1, "id": "wf_probe", "origin": "https://example.test",
                   "start_url": "https://example.test/form",
                   "steps": [{"id": "s1", "action": "hotkey", "hotkey": "Control+Enter"}]}
        with self.assertRaises(AgentError) as saved:
            self.by_name("ra.save_workflow")["handler"]({"workflow": payload})
        self.assertEqual(saved.exception.code, "bad_input")
        with self.assertRaises(AgentError) as run:
            self.by_name("ra.run_workflow")["handler"]({"workflow_id": "wf_probe"})
        self.assertEqual(run.exception.code, "bad_input")

    def test_missing_required_input_is_bad_input(self):
        with self.assertRaises(AgentError) as missing:
            self.by_name("ra.get_workflow")["handler"]({})
        self.assertEqual(missing.exception.code, "bad_input")

    def test_secret_tool_never_returns_values(self):
        from ra.secrets import FileSecretStore

        FileSecretStore(self.data / "secrets.json").set("probe_ref", "super-secret-value")
        text = json.dumps(self.by_name("ra.list_secret_refs")["handler"]({}), ensure_ascii=False)
        self.assertIn("probe_ref", text)
        self.assertNotIn("super-secret-value", text)

    def test_status_and_history_read_the_isolated_data_dir(self):
        status = self.by_name("ra.status")["handler"]({})
        self.assertEqual(Path(status["data_dir"]), self.data)
        self.assertEqual(status["workflows"], 0)
        history = self.by_name("ra.run_history")["handler"]({"limit": 5})
        self.assertEqual(history["rows"], [])
        with self.assertRaises(AgentError) as gone:
            self.by_name("ra.get_workflow")["handler"]({"workflow_id": "nope"})
        self.assertEqual(gone.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main()
