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
        self.assertTrue(self.names(), True)
        self.assertTrue(all(name.startswith("ra.") for name in self.names()), self.names())
        self.assertGreaterEqual(len(self.names()), 4)
        self.assertLessEqual(len(self.names()), 8)

    def test_agent_error_class_is_shared_with_the_server(self):
        # importlib 二次加载会让 AgentError 变成两个类对象，server 就捕不到 handler 抛的错。
        self.assertIs(self.tools.AgentError, AgentError)

    def test_risk_labels_are_honest(self):
        risks = {tool["name"]: tool["risk"] for tool in self.tools.TOOLS}
        self.assertEqual(risks["ra.save_workflow"], "write")
        self.assertEqual(risks["ra.run_workflow"], "exec")
        for name in ("ra.status", "ra.list_workflows", "ra.get_workflow", "ra.validate_workflow",
                     "ra.run_history", "ra.list_secret_refs"):
            self.assertEqual(risks[name], "read", name)

    def test_destructive_actions_are_not_exposed(self):
        joined = " ".join(self.names())
        for forbidden in ("delete", "reset", "clear", "shutdown"):
            self.assertNotIn(forbidden, joined.lower())

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
