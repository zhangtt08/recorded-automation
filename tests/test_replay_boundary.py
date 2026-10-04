"""回放的同源边界不许回退（评审第 4 项点名的 GOOD：钉住，而不是重申一遍）。

| GOOD（判据住在哪） | 这一组里钉住它的断言 |
| --- | --- |
| `ra/core.py::Workflow` 强制 `start_url` ∈ `origin` | `test_out_of_origin_start_url_is_refused_at_every_layer`（构造 / 校验面 / 存盘三层） |
| 同一条判定在 Agent 工具面上也成立 | `test_the_agent_surface_refuses_the_same_workflow`（validate / save / run 三个出口） |
| 绕过 save() 手放进磁盘的定义 | `test_a_hand_placed_out_of_origin_file_is_refused_before_the_browser_opens` |
| origin 是协议 + 主机 + 端口三元组 | `test_same_origin_but_different_port_or_scheme_is_still_refused` |
| `driver.in_scope` 每个动作前重查 | `test_in_scope_is_reevaluated_before_every_action` + `test_run_stops_at_the_first_action_after_the_page_leaves_the_origin` + `test_the_completion_condition_lookup_is_gated_too` |
| 重查读的是「当前那一页」而不是缓存的布尔值 | `test_the_real_adapter_reads_the_live_url_on_every_check`（真 `ra.driver.PlaywrightDriver`） |
| `Runner.scrub()` 把喂进页面的值从原因里抹掉 | `test_scrub_strips_fed_values_from_reasons` |
| 缺秘密引用时在动作之前停下，只报码不报值 | `test_a_missing_secret_reference_is_reported_by_name_only` |
| 递给页面的 JS 只取自仓库里的固定片段 | `test_only_bundled_js_reaches_the_page` |
| `paths.py` / `main.build()` 把浏览器档案钉在数据目录里 | `test_profile_dir_is_pinned_under_the_data_root` |
| 这一组的替身必须像真类 | `test_the_doubles_are_shaped_like_the_real_contracts` |

不启动浏览器：被测的判定全是**真对象** —— `ra.core.Runner`、`ra.driver.PlaywrightDriver`、
`ra.journal.FileJournal`（真的落盘，再从磁盘读回来查）、`ra.secrets.StaticSecrets`、
`agent/tools.py` 里那份真工具表。只有「页面」与「本机数据目录」这两半是假的：页面换成只会
报 URL 的记账对象，数据目录靠临时 `LOCALAPPDATA` 顶掉。替身必须长成 `ra.core.Driver` 协议的
样子：少一个 `act`，Runner 就会在 `driver.act(...)` 上退化成 AttributeError，那一刻被测的
就不再是 Runner 了 —— 这正是本项目记过的那条教训（形状不像真类的替身不算测试），所以它也被
钉成了断言，而不是靠人记得。

测试一律把 `LOCALAPPDATA` 指到临时目录：本机真实的录放台数据（工作流、秘密库、运行日志、
受控浏览器档案里的 Cookie）不读也不写。
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from threading import Event

from ra.core import Action, Driver, Journal, Runner, SecretStore, workflow_from_dict
from ra.journal import FileJournal
from ra.main import build
from ra.paths import data_root, resource_root
from ra.secrets import StaticSecrets, UnknownSecret
from ra.store import WorkflowStore

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = "https://hr.example.test"
SECRET_VALUE = "hunter2"

GOOD = {
    "schema_version": 1,
    "id": "wf_origin",
    "name": "同源的一份",
    "origin": ORIGIN,
    "start_url": f"{ORIGIN}/apply/form",
    "steps": [{"id": "s1", "action": "click",
               "target": {"page": "main", "locators": [{"strategy": "label", "value": "下一步"}]},
               "expected": {"page": "main", "locators": [{"strategy": "text", "value": "感谢申请"}]}}],
}
# 只有 start_url 换到另一个源：其余字段逐字相同，所以失败只能归因于同源判定。
EVIL = dict(GOOD, start_url="https://attacker.test/collect")


def workflow(payload: dict):
    """真的走一次反序列化：Locator / Step / Workflow 都由 ra.core 自己构造，不手搓数据类。"""
    return workflow_from_dict(payload)


class RecordingDriver:
    """假页面，但形状严格按 `ra.core.Driver` 的三个协议方法，行为只到「记账」为止。

    `timeline` 记录 Runner 真实看到的调用顺序，所以「动作之前有没有重查同源」这一条
    量的是发生过的调用，不是某一次调用的返回值。
    """

    def __init__(self, origin: str = ORIGIN) -> None:
        self.allowed = {origin}
        self.timeline: list[str] = []
        self.fed: list[str] = []
        self.echo_argument = False          # 复刻 Playwright 把取值原样回显进报错那条路
        self.leave_after_actions: int | None = None   # 第 N 个动作之后页面就离开同源

    def in_scope(self, origin: str) -> bool:
        self.timeline.append("in_scope")
        if origin not in self.allowed:
            return False
        if self.leave_after_actions is not None:
            return self.timeline.count("act") < self.leave_after_actions
        return True

    def resolve(self, target, timeout_s: float = 10.0):
        self.timeline.append("resolve")
        return ("locator", self.timeline.count("resolve"), timeout_s)

    def act(self, action, element, argument) -> None:
        self.timeline.append("act")
        if action == Action.FILL:
            self.fed.append(str(argument))
        if self.echo_argument:
            raise RuntimeError(f"select_option: 无法选中 {argument}")

    # 供断言用的派生量
    @property
    def acts(self) -> int:
        return self.timeline.count("act")

    @property
    def scope_checks(self) -> int:
        return self.timeline.count("in_scope")


class FakePage:
    """PlaywrightDriver 只向页面要 `url` —— 给一个会变的 url 就够，
    这样「每次重查」这件事可以在不启动浏览器的情况下量出来。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.frames: list = []


def load_agent_tools():
    """按 agent/server.py 与进程内服务同一份路径把工具表读进来（真表，不是抄一份）。"""
    import importlib.util
    import sys

    agent_dir = ROOT / "agent"
    for path in (str(agent_dir), str(ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location("ra_agent_tools_boundary", agent_dir / "tools.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ra_agent_tools_boundary"] = module
    spec.loader.exec_module(module)
    return module


class IsolatedDataMixin:
    """把本机数据目录临时换成临时目录：data_root() 按 env 现读，所以换 env 就够了。"""

    def isolate_data_root(self) -> Path:
        self._previous_localappdata = os.environ.get("LOCALAPPDATA")
        self._previous_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["LOCALAPPDATA"] = str(self.root)
        os.environ["XDG_DATA_HOME"] = str(self.root)
        return Path(data_root())

    def release_data_root(self) -> None:
        if self._previous_localappdata is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._previous_localappdata
        if self._previous_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._previous_xdg


class ReplayBoundaryTests(unittest.TestCase, IsolatedDataMixin):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ra-boundary-"))
        self.store = WorkflowStore(self.root / "workflows", resource_root() / "workflow.schema.json")
        self.journal = FileJournal(self.root / "journal.jsonl")
        self.stop = Event()

    def tearDown(self):
        self.stop.set()

    def runner(self, driver, secrets=None) -> Runner:
        """四个 collaborator 全部按 ra.core.Runner 的真实签名传：driver, secrets, journal, stop。"""
        return Runner(driver, secrets or StaticSecrets(), self.journal, self.stop, poll_s=0.001)

    # -- 边界一：start_url 必须在声明的 origin 之内 -------------------------------
    def test_out_of_origin_start_url_is_refused_at_every_layer(self):
        # 1) 领域对象：构造即拒，理由点名 start_url（不是「某个字段不对」）
        with self.assertRaises(ValueError) as caught:
            workflow(EVIL)
        self.assertIn("start_url", str(caught.exception))
        # 2) 校验面：JSON Schema 放行的东西，执行语义这一层必须挡住（blocking，不是 warning）
        report = self.store.problems(EVIL)
        self.assertTrue(report["schema_ok"], "Schema 这一层现在就能挡住，下面的语义层断言就失去意义了")
        self.assertTrue(any("start_url" in item for item in report["blocking"]), report)
        self.assertEqual(report["warnings"], [], report)
        # 3) 存盘：拒绝写入，并且磁盘上真的没有留下这个文件
        with self.assertRaises(ValueError):
            self.store.save(EVIL)
        self.assertEqual(self.store.list(), [], "越界的定义被写进了本机工作流库")
        self.assertFalse((self.root / "workflows" / "wf_origin.workflow.json").exists())
        # 4) 同源的对照品必须能存下来 —— 否则上面三条可能是「什么都存不进去」
        self.store.save(GOOD)
        self.assertEqual([row["id"] for row in self.store.list()], ["wf_origin"])

    def test_a_hand_placed_out_of_origin_file_is_refused_before_the_browser_opens(self):
        """绕过 save() 直接把文件放进磁盘（手改、同步盘、Agent 写错目录都算）：
        加载它去运行时要被同一个判据拦下，而且是在打开浏览器之前。"""
        path = self.root / "workflows" / "wf_evil.workflow.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(EVIL, ensure_ascii=False), encoding="utf-8")
        api, session, _ = build(self.root)
        try:
            result = api.run_workflow("wf_evil")
            self.assertIn("error", result, f"越界的 start_url 竟然开始运行：{result}")
            self.assertIn("start_url", result["error"])
            self.assertEqual(session.state, "idle", "被拦下时不该已经把会话推进到 running")
            self.assertFalse(session.snapshot()["browser_open"], "越界的定义不该打开受控浏览器")
            self.assertEqual(session.runs, {}, "被拦下的运行不许留下运行记录")
            # 同一个文件在校验面上也是阻塞项（界面显示「有问题」而不是「可以跑」）
            self.assertTrue(any("start_url" in item
                                for item in self.store.problems(json.loads(path.read_text(encoding="utf-8")))["blocking"]))
        finally:
            session.shutdown()

    def test_same_origin_but_different_port_or_scheme_is_still_refused(self):
        """origin 是三元组：换个端口、换个协议、加个子域都不算同源。"""
        for url in ("http://hr.example.test/apply", "https://hr.example.test:8443/apply",
                    "https://apply.hr.example.test/apply", "https://hr.example.test.evil/apply"):
            with self.assertRaises(ValueError, msg=url):
                workflow(dict(GOOD, start_url=url))

    # -- 边界二：每个动作之前重新查一遍同源 ------------------------------------
    def test_in_scope_is_reevaluated_before_every_action(self):
        """不是「开局查一次就信任到底」：每个动作之前都重新问一次 driver.in_scope。"""
        driver = RecordingDriver()
        steps = 3
        payload = dict(GOOD, id="wf_scope",
                       steps=[{"id": f"s{index}", "action": "click",
                               "target": {"page": "main",
                                          "locators": [{"strategy": "label", "value": "下一步"}]}}
                              for index in range(1, steps + 1)])
        result = self.runner(driver).run(workflow(payload))
        # 这三步都没写完成条件（expected），所以程序如实记「完成 · 未验证」而不是「完成」。
        self.assertEqual(result.status.value, "completed_unverified")
        # 每步的真实顺序：开局闸门 → 定位前闸门 → 定位 → 动作前闸门 → 动作
        self.assertEqual(driver.timeline, ["in_scope", "in_scope", "resolve", "in_scope", "act"] * steps,
                         f"调用顺序变了：{driver.timeline}")
        self.assertEqual(driver.scope_checks, steps * 3,
                         f"{steps} 个动作只查了 {driver.scope_checks} 次同源，闸门没有在动作前重查")
        # 判据落在「紧邻动作之前」这件事上，而不是落在总次数上：
        # 每一次 act 前面一句必须是 in_scope —— 少一次重查就是这条断言变红。
        for index, item in enumerate(driver.timeline):
            if item == "act":
                self.assertEqual(driver.timeline[index - 1], "in_scope",
                                 f"第 {index} 个调用是 act，但它前面不是同源检查（{driver.timeline}）")

    def test_run_stops_at_the_first_action_after_the_page_leaves_the_origin(self):
        """页面漂到别的源之后，动作发不出去，而且结果按程序既有的分类说（不是「完成」）。"""
        driver = RecordingDriver()
        driver.leave_after_actions = 1          # 第一个动作之后页面就不在同源了
        payload = dict(GOOD, id="wf_drift",
                       steps=[{"id": f"s{index}", "action": "click",
                               "target": {"page": "main",
                                          "locators": [{"strategy": "label", "value": "下一步"}]}}
                              for index in (1, 2)])
        result = self.runner(driver).run(workflow(payload))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.code, "PageOutOfScope")
        self.assertEqual(driver.acts, 1, f"越界之后还发出了 {driver.acts - 1} 个动作")
        self.assertIn("来源", result.reason, result.reason)
        # 这条分类不是只在内存里：日志落盘后仍然答得出「动作之前停的」以及停在哪个码上。
        # （终态那一条 completed/failed 是 Session 结算时补的，Runner 自己只写到阶段事件为止。）
        events = self.journal.read_run(result.run_id)
        phases = [event["phase"] for event in events]
        self.assertEqual(phases[-1], "failed_before_action", phases)
        self.assertEqual(events[-1]["code"], "PageOutOfScope", events[-1])
        self.assertEqual(phases.count("action_started"), 1, phases)

    # -- 边界三：喂进页面的值不写进原因 ----------------------------------------
    def test_scrub_strips_fed_values_from_reasons(self):
        """`scrub()` 把交给过页面的值从任何要落盘的原因里换掉（真日志、真结果文本）。"""
        driver = RecordingDriver()
        driver.echo_argument = True             # 驱动把取值回显进报错 —— 泄漏就发生在这里
        payload = dict(GOOD, id="wf_scrub", steps=[
            {"id": "s1", "action": "fill",
             "target": {"page": "main", "locators": [{"strategy": "label", "value": "密码"}]},
             "secret_ref": "pw"}])
        runner = self.runner(driver, StaticSecrets({"pw": SECRET_VALUE}))
        result = runner.run(workflow(payload))
        self.assertEqual(driver.fed, [SECRET_VALUE], "这一格没有真的喂值，scrub 测不到东西")
        self.assertEqual(result.status.value, "uncertain", "动作已发出且驱动报错 —— 只能是不确定")
        self.assertNotIn(SECRET_VALUE, result.reason, f"秘密值出现在了结果文本里：{result.reason}")
        self.assertIn("***", result.reason)
        # 落盘那一份才是关键：日志文件里逐字节查一遍
        on_disk = (self.root / "journal.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(SECRET_VALUE, on_disk, "运行日志把喂进页面的值记下来了")
        # 直接问 scrub 本身，两种形状都要换掉：明文值与 key=value 形式
        self.assertEqual(runner.scrub(f"失败：字段里是 {SECRET_VALUE} 这串值"), "失败：字段里是 *** 这串值")
        self.assertEqual(runner.scrub("password=abc123 被拒绝"), "password=*** 被拒绝")

    def test_a_missing_secret_reference_is_reported_by_name_only(self):
        """引用名读不到值时：动作一个都不发，原因里只有中文句子 + 机器可读的码。

        名字本身在哪读？`UnknownSecret` 的 args 里（`ra/secrets.py::get` 抛的就是它），
        以及运行之前的 API 拦截面 `Session.missing_secrets` 上（那里逐个列出，见
        tests/test_run_control.py）—— `friendly_reason` 对已知码只给固定句子，
        所以这里断言的是「码 + 不外泄值」这两件，而不是把名字硬塞进 reason。
        """
        driver = RecordingDriver()
        payload = dict(GOOD, id="wf_missing", steps=[
            {"id": "s1", "action": "fill",
             "target": {"page": "main", "locators": [{"strategy": "label", "value": "密码"}]},
             "secret_ref": "not_stored"}])
        result = self.runner(driver, StaticSecrets({})).run(workflow(payload))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.code, "UnknownSecret")
        self.assertEqual(driver.acts, 0, "读不到值的时候不许喂任何东西给页面")
        self.assertEqual(driver.fed, [])
        self.assertIn("秘密库", result.reason)
        self.assertEqual([event["code"] for event in self.journal.read_run(result.run_id)
                          if event["code"]], ["UnknownSecret"])
        # 名字跟着异常走，绝不跟着值走
        with self.assertRaises(UnknownSecret) as caught:
            StaticSecrets({}).get("not_stored")
        self.assertEqual(caught.exception.args[0], "not_stored")

    # -- 边界四：递给页面的 JS 只来自仓库里的固定片段 ---------------------------
    def test_only_bundled_js_reaches_the_page(self):
        """回放/录制链路里，任何递给页面的 JS 都必须是仓库里的字面量，或那份随程序分发的
        录制脚本；不许出现「把调用方给的东西拼进脚本再发出去」的形状。"""
        scanned = ("ra/core.py", "ra/driver.py", "ra/session.py", "ra/recorder.py")
        allowed_names = {"ra/recorder.py": {"self.script"}}   # 唯一非字面量：随程序分发的那份脚本
        found = []
        for relative in scanned:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in {"evaluate", "add_init_script", "run_js", "dispatch_event"}:
                    continue
                self.assertTrue(node.args, f"{relative}:{node.lineno} 递给页面的调用没有参数")
                argument = node.args[0]
                rendered = ast.unparse(argument)
                allowed = allowed_names.get(relative, set())
                if isinstance(argument, ast.Constant):
                    self.assertIsInstance(argument.value, str, f"{relative}:{node.lineno} JS 参数不是字符串")
                    found.append((relative, node.lineno, "字面量"))
                    continue
                if rendered in allowed:
                    found.append((relative, node.lineno, rendered))
                    continue
                self.fail(f"{relative}:{node.lineno} 递给页面的脚本不是固定片段（{rendered[:120]}）："
                          "f-string 或拼接一旦进到这里，工作流里的文本就成了在页面上执行的代码")
        self.assertEqual(len(found), 3, f"应该数到驱动里那两条判定 + 录制脚本注入，实际：{found}")
        # 那条「非字面量」的豁免必须真是从磁盘读进来的仓库文件，否则豁免本身是个洞
        recorder = (ROOT / "ra" / "recorder.py").read_text(encoding="utf-8")
        self.assertRegex(recorder, r"self\.script\s*=\s*Path\(script_path\)\.read_text")
        self.assertRegex(recorder, r"BINDING_NAME\s*=\s*[\"'][A-Za-z_]+[\"']")
        # 装配处必须把它接到随程序分发的那个文件上（不是临时目录、不是调用方给的路径）
        main = (ROOT / "ra" / "main.py").read_text(encoding="utf-8")
        self.assertIn('script_path=resources / "ui" / "record_script.js"', main)
        self.assertTrue((resource_root() / "ui" / "record_script.js").is_file())

    # -- 边界五：Agent 那一面上同一条判定也得成立 --------------------------------
    def test_the_agent_surface_refuses_the_same_workflow(self):
        """同源判定必须在 Agent 的工具面上也成立：界面挡住了、工具面放行，等于门还开着。

        69f8541 那道本机闸门管的是「谁能调」；这一条管的是「调得动也不许把越界的定义
        写进本机、更不许拿去开浏览器跑」。三个出口都走 agent/tools.py 里那份真表。
        """
        resolved = self.isolate_data_root()
        tools = load_agent_tools()
        by_name = {tool["name"]: tool for tool in tools.TOOLS}
        workflows = Path(resolved) / "workflows"
        try:
            # 1) 校验面：ra.validate_workflow 只回原因，并且把这一条列为阻塞项
            report = by_name["ra.validate_workflow"]["handler"]({"workflow": EVIL})
            self.assertTrue(report["schema_ok"], "JSON Schema 这一层就挡住了，下面的语义层断言没意义")
            self.assertTrue(any("start_url" in item for item in report["blocking"]), report)
            self.assertFalse(report["saved"], report)
            self.assertFalse(workflows.exists() and list(workflows.glob("*.workflow.json")),
                             "validate 不该写盘")
            # 2) 写盘面：带着 confirm:true 也照样被拒，本机工作流库里没有留下这个文件
            with self.assertRaises(tools.AgentError) as saved:
                by_name["ra.save_workflow"]["handler"]({"workflow": EVIL, "confirm": True})
            self.assertEqual(saved.exception.code, "bad_input")
            self.assertIn("start_url", str(saved.exception))
            self.assertFalse(workflows.exists() and list(workflows.glob("*.workflow.json")),
                             "越界的定义被 Agent 写进了本机工作流库")
            # 3) 运行面：手放一个越界定义（绕过工具的写入路径），运行必须在开浏览器之前被拒
            workflows.mkdir(parents=True, exist_ok=True)
            (workflows / "wf_evil.workflow.json").write_text(json.dumps(EVIL, ensure_ascii=False),
                                                              encoding="utf-8")
            with self.assertRaises(tools.AgentError) as run:
                by_name["ra.run_workflow"]["handler"]({"workflow_id": "wf_evil", "confirm": True})
            self.assertEqual(run.exception.code, "run_refused")
            self.assertIn("start_url", str(run.exception))
            # 4) 对照品：只差 start_url 同源的那一份必须写得进去 —— 否则上面三条只是「什么都拒」
            saved_ok = by_name["ra.save_workflow"]["handler"]({"workflow": GOOD, "confirm": True})
            self.assertTrue(saved_ok["saved"], saved_ok)
            # 5) 本机列表面怎么交代那份手放的文件：列得出来，但标成损坏，并且跑不起来
            rows = {row["id"]: row for row in
                    WorkflowStore(workflows, resource_root() / "workflow.schema.json").list()}
            self.assertEqual(sorted(rows), ["wf_evil.workflow", "wf_origin"], rows)
            self.assertIn("start_url", rows["wf_evil.workflow"]["broken"], rows)
            self.assertEqual(rows["wf_origin"]["broken"], "", rows)
        finally:
            self.release_data_root()

    # -- 边界六：适配器那一道查的是「当前那一页」 --------------------------------
    def test_the_real_adapter_reads_the_live_url_on_every_check(self):
        """`in_scope` 之所以算「每次动作前重查」，是因为它每次都重新读当前页面的 URL，
        不是开局算好一个布尔值存下来。这里用的是真的 `ra.driver.PlaywrightDriver`。"""
        from ra.driver import OutOfScope, PlaywrightDriver, Resolved, same_origin

        page = FakePage(f"{ORIGIN}/apply/form")
        driver = PlaywrightDriver(page, ORIGIN)
        self.assertTrue(driver.in_scope(ORIGIN))
        page.url = "https://attacker.test/collect"          # 页面自己跳走了
        self.assertFalse(driver.in_scope(ORIGIN), "in_scope 把开局那一次的结果缓存下来了")
        page.url = f"{ORIGIN}/apply/form"
        self.assertTrue(driver.in_scope(ORIGIN), "回到同源之后仍要认：判定不是单向的")
        page.url = "about:blank"
        self.assertFalse(driver.in_scope(ORIGIN), "空白页不许算作同源（那还没有任何页面）")
        # 动作那一步适配器自己还有一道：就算策略层漏查了，越界的页面也发不出动作
        page.url = "https://attacker.test/collect"
        with self.assertRaises(OutOfScope):
            driver.act(Action.CLICK, Resolved(locator=object(), timeout_ms=1000.0), None)
        # 与 Workflow 那条判定同一把尺：协议或端口不同就不算同源
        self.assertTrue(same_origin("https://hr.example.test/apply", ORIGIN))
        self.assertFalse(same_origin("https://hr.example.test:8443/apply", ORIGIN))
        self.assertFalse(same_origin("http://hr.example.test/apply", ORIGIN))

    # -- 边界七：完成条件那一次定位也过一次闸门 ----------------------------------
    def test_the_completion_condition_lookup_is_gated_too(self):
        """动作发出去之后核对完成条件，同样是「先查同源再读页面」；等待步一个动作都不发。"""
        driver = RecordingDriver()
        payload = dict(GOOD, id="wf_gated", steps=GOOD["steps"] + [
            {"id": "s2", "action": "wait_for",
             "target": {"page": "main", "locators": [{"strategy": "text", "value": "受理编号"}]}}])
        result = self.runner(driver).run(workflow(payload))
        self.assertEqual(result.status.value, "completed", result.reason)
        self.assertEqual(driver.timeline,
                         ["in_scope",                          # 第一步开局闸门
                          "in_scope", "resolve",               # 定位目标之前
                          "in_scope", "act",                   # 发动作之前
                          "in_scope", "resolve",               # 核对完成条件之前
                          "in_scope", "in_scope", "resolve"],  # 等待步：两次闸门 + 定位，没有 act
                         f"调用顺序变了：{driver.timeline}")
        self.assertEqual(driver.acts, 1, "wait_for 那一步不该发出任何动作")

    # -- 边界八：受控浏览器档案钉在数据目录里 -----------------------------------
    def test_profile_dir_is_pinned_under_the_data_root(self):
        """`paths.py` + `main.build()` 把受控浏览器档案钉在数据目录里，而且位置是确定的一个：
        不许每次装配换一个地方（那等于登录态留不下来），也不许指回用户自己浏览器的
        profile（那里面是真实登录态）或仓库里那份遗留档案目录。"""
        resolved = self.isolate_data_root()
        try:
            self.assertEqual(Path(resolved).name, "RecordedAutomation")
            self.assertEqual(Path(resolved), Path(os.environ["LOCALAPPDATA"]) / "RecordedAutomation")
            _, first, base = build()                    # 不给目录：走默认解析
        except Exception:
            self.release_data_root()
            raise
        second = None
        try:
            _, second, _ = build()
            profile = Path(first.profile_dir)
            self.assertEqual(profile, base / "profile")
            self.assertEqual(profile.parent, resolved)
            self.assertEqual(Path(second.profile_dir), profile, "两次装配指向两个档案目录")
            self.assertNotIn("User Data", str(profile), "指向了用户真实浏览器档案")
            self.assertNotEqual(profile.parent, ROOT / ".ra-tmp", "用到了仓库里那份遗留浏览器档案")
        finally:
            if second is not None:
                second.shutdown()
            first.shutdown()
            self.release_data_root()

    # -- 这一组自己的守卫：替身必须像真类 ---------------------------------------
    def test_the_doubles_are_shaped_like_the_real_contracts(self):
        """Runner 只通过三个协议跟外部打交道。替身少一个方法，被测对象就会在半路
        AttributeError 上退化 —— 那条测试测的已经不是 Runner 了。所以协议成员逐个查。"""
        def members(contract) -> set[str]:
            return {name for name in dir(contract) if not name.startswith("_")}
        driver = RecordingDriver()
        for contract, instance in ((Driver, driver), (SecretStore, StaticSecrets()), (Journal, self.journal)):
            required = members(contract)
            self.assertTrue(required, f"{contract.__name__} 协议里一个方法都没有，这条断言已经失效")
            missing = sorted(name for name in required if not callable(getattr(instance, name, None)))
            self.assertEqual(missing, [], f"{type(instance).__name__} 不像 {contract.__name__}：缺 {missing}")
        # 签名本身也钉住：stop 是必需的位置参数，且顺序不许悄悄改（少了它构造就 TypeError）
        import inspect
        params = list(inspect.signature(Runner).parameters.values())
        required = [item.name for item in params if item.default is inspect.Parameter.empty]
        self.assertEqual(required, ["driver", "secrets", "journal", "stop"],
                         f"Runner 的必需协作者变成了 {required}：测试与真实装配点要一起改")
        self.assertEqual(params[0].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        # 界面那条真实装配点用同样的位置顺序构造，替身不许自成一套
        session_source = (ROOT / "ra" / "session.py").read_text(encoding="utf-8")
        self.assertRegex(session_source,
                         r"Runner\(driver,\s*self\.secrets,\s*journal,\s*self\._stop",
                         "ra/session.py 里的装配点不再按真实签名构造 Runner")


if __name__ == "__main__":
    unittest.main()
