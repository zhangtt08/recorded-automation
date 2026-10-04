"""运行日志的体积上界，以及"删过档要说出来"（评审 MAJOR #3）。

评审原话：`journal.py:32-62` 一路 append 到底，`TAIL_BYTES` 只限制**读取**，不限制文件本身。
于是两件事同时不成立：日志永远在涨；而"界面上的历史就是最近的运行"靠的是悄悄跳过文件头。

这里钉的就是这两条加上那句诚实：

1. 到线就轮转，单份体积有上界（不是"读的时候只看尾部"）。
2. 轮转留下的标记算得出真数：轮转了几次、各份现在多少字节、被挤出去的最旧那份是多少字节。
3. 一次运行的事件不被切成两半：跨轮转的 run_id 仍能整份读回（`read_run` 会翻归档）。
4. 最旧那份真被删掉时，列表少了一条，同时 `stats()` / `Api.list_runs()` / `ra.run_history`
   必须带上那句话 —— 只让列表变短而不解释，才是这条评审意见真正在拦的行为。
5. 没轮转过的时候它要能说"读到的是全部"，否则第 4 条的报告随时可能是在掩饰。

全程只写临时目录，不碰本机真实的 %LOCALAPPDATA%\\RecordedAutomation，也不启动浏览器。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from ra import journal as journal_module
from ra.core import RunEvent
from ra.journal import FileJournal

# 阶段名在旧版里根本不存在。刻意用 getattr 取，不让它把整个模块的 import 打断 ——
# 评审要的"每条都能红"必须是逐条断言失败，而不是夹具里的 ImportError。
ROTATION_PHASE = getattr(journal_module, "ROTATION_PHASE", "journal_rotated")

ROOT = Path(__file__).resolve().parent.parent


def make_journal(path: Path, max_bytes: int, keep: int) -> FileJournal:
    """带上限参数构造。旧版（一路 append 到底）不接受这两个参数 —— 那正是评审点的问题，
    把它写成一句读得懂的断言失败，而不是夹具里的 TypeError。"""
    try:
        return FileJournal(path, max_bytes=max_bytes, keep=keep)
    except TypeError as exc:
        raise AssertionError(f"FileJournal 没有体积上界参数（{exc}）—— 就是「一路 append 不封顶」那一条")


def event(run_id: str, phase: str = "step_started", reason: str = "") -> RunEvent:
    return RunEvent(run_id, "wf_gate", "s1", phase, code="some_code", reason=reason)


def load_tools():
    agent = ROOT / "agent"
    if str(agent) not in sys.path:                   # tools.py 里 `from errors import AgentError`
        sys.path.insert(0, str(agent))               # 要按标准实现的样子从 agent/ 目录解析
    spec = importlib.util.spec_from_file_location("ra_tools_journal_probe", agent / "tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class JournalBoundTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ra-journal-"))
        self.path = self.root / "journal.jsonl"
        self.max_bytes = 4000
        self.keep = 2
        self.journal = make_journal(self.path, self.max_bytes, self.keep)

    def write(self, count: int, prefix: str = "run") -> None:
        for index in range(count):
            self.journal.append(event(f"{prefix}-{index}", phase="step_started"))
            self.journal.append(event(f"{prefix}-{index}", phase="completed", reason="停在 s1 / some_code"))

    def markers(self) -> list[dict]:
        found = []
        for raw in self.journal._recent_lines(tail_only=False):        # noqa: SLF001 - 就是要读原始标记
            row = json.loads(raw)
            if row.get("phase") == ROTATION_PHASE:
                found.append(row)
        return found

    def test_size_is_bounded_after_many_appends(self):
        self.write(400)                                                # 约 800 条，远超 4000 字节
        stats = self.journal.stats()
        self.assertGreater(stats["rotations"], 0, "到线了却没有轮转")
        self.assertLessEqual(self.path.stat().st_size, self.max_bytes + 2000,
                             "当前这份没有上界：还在无限增长")
        self.assertLessEqual(stats["bytes"], self.max_bytes * (self.keep + 1) + 4000,
                             "含归档的总量也没有上界")

    def test_no_rotation_before_the_bound_and_it_says_so(self):
        self.write(2)
        stats = self.journal.stats()
        self.assertFalse(stats["trimmed"], "没到线就轮转了")
        self.assertEqual(stats["rotations"], 0)
        self.assertIn("还没有轮转", stats["note"], "没删过档时它得能证明读到的是全部")
        self.assertEqual(len(self.journal.list_runs(limit=50)), 2)

    def test_rotation_marker_records_real_byte_counts(self):
        self.write(400)
        stats = self.journal.stats()
        self.assertTrue(stats["trimmed"])
        self.assertIn(str(stats["max_bytes"]), stats["note"])
        # 标记里的数字必须是真数出来的：拿磁盘上的实际大小逐份对一遍
        self.assertGreaterEqual(len(stats["files"]), 2)
        for item in stats["files"]:
            self.assertEqual(item["bytes"], (self.root / item["name"]).stat().st_size)
        # 轮转标记本身带 archived_bytes / dropped_bytes，而且不是某一次运行的事件
        marks = self.markers()
        self.assertTrue(marks, "日志里没有一条轮转标记：删过档这件事没有留下痕迹")
        self.assertEqual(marks[0]["run_id"], "", "轮转标记不能冒充一次运行")
        self.assertGreater(marks[0]["archived_bytes"], 0)

    def test_dropped_history_is_announced_not_silent(self):
        """写满到最旧那份归档被挤出去：列表少了一条，同时必须有一句话说清删了多少字节。"""
        journal = make_journal(self.root / "drop.jsonl", 20000, 2)      # 容量 20000×3
        self.journal = journal
        self.write(60, prefix="old")
        before = {row["run_id"] for row in journal.list_runs(limit=1000)}
        self.assertEqual(len(before), 60, "这一步只是搭现场：60 次运行都该还在保留窗口内")
        self.assertEqual(journal.stats()["dropped_bytes"], 0, "还没到删除的时候就不该报丢")
        self.write(400, prefix="new")
        stats = journal.stats()
        after = {row["run_id"] for row in journal.list_runs(limit=5000)}
        self.assertGreater(stats["dropped_bytes"], 0,
                           "最旧那份归档已经没了，却没把删掉的字节数说出来")
        self.assertIn("已删除", stats["note"])
        self.assertTrue(after - before, "新写的运行应当读得到（有界不等于写不进去）")
        lost = before - after
        self.assertTrue(lost, "预期最旧那批运行会被挤出保留窗口")
        self.assertTrue(all(name.startswith("old-") for name in lost), "丢的必须只是最旧那批")

    def test_a_run_survives_rotation_intact(self):
        """一次运行的事件跨过了轮转边界，只要还在保留窗口内就必须整份读回（翻归档也算）。

        这里把 keep 调大到不会真的丢档：测的是「跨份读取」，不是「丢了还说得出」——
        后者由 test_dropped_history_is_announced_not_silent 负责。
        """
        journal = make_journal(self.root / "cross.jsonl", 1000, 5)      # 容量 1000×6，写得满也丢不掉
        self.journal = journal
        journal.append(event("wf-cross", phase="step_started"))         # 这条会随轮转进到归档里
        for index in range(3):
            journal.append(RunEvent("filler", "wf_gate", f"s{index}", "step_started",
                                    code="c", reason="填充用事件 " * 20))
        journal.append(event("wf-cross", phase="action_started"))
        journal.append(event("wf-cross", phase="completed", reason="停在 s1 / some_code"))
        stats = journal.stats()
        self.assertGreater(stats["rotations"], 0, "这一组事件本该触发至少一次轮转")
        self.assertEqual(stats["dropped_bytes"], 0, "这一格不该丢档（keep 足够大）")
        self.assertGreater(len(journal._files()), 1, "轮转之后应留有归档文件，否则测不到翻归档")
        rows = journal.read_run("wf-cross")
        self.assertEqual([row["phase"] for row in rows],
                         ["step_started", "action_started", "completed"],
                         f"跨轮转的运行只读回来 {len(rows)} 条：一次运行的事件被切开了")
        self.assertTrue(all(row["workflow_id"] == "wf_gate" for row in rows))

    def test_keep_zero_still_bounds_the_file(self):
        journal = make_journal(self.root / "keep0.jsonl", self.max_bytes, 0)
        for index in range(400):
            journal.append(event(f"r-{index}"))
        self.assertLessEqual((self.root / "keep0.jsonl").stat().st_size, self.max_bytes + 2000)
        stats = journal.stats()
        self.assertTrue(stats["trimmed"])
        self.assertGreater(stats["dropped_bytes"], 0, "不留归档时更要说清丢了多少")

    def test_stats_and_list_runs_survive_a_missing_journal(self):
        fresh = make_journal(self.root / "nothing" / "journal.jsonl", self.max_bytes, self.keep)
        self.assertEqual(fresh.list_runs(limit=10), [])
        stats = fresh.stats()
        self.assertEqual(stats["bytes"], 0)
        self.assertFalse(stats["trimmed"])

    def test_events_keep_their_shape_and_redaction_boundary(self):
        """轮转不许改变事件本身：reason 仍然截到 2000 字，标识字段一个不少。"""
        long_reason = "停在 s1 / AmbiguousTarget " + ("x" * 3000)
        self.journal.append(event("wf-shape", phase="failed", reason=long_reason))
        rows = self.journal.read_run("wf-shape")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workflow_id"], "wf_gate")
        self.assertEqual(rows[0]["code"], "some_code")
        self.assertLessEqual(len(rows[0]["reason"]), 2000)


class ApiAndToolReportingTests(unittest.TestCase):
    """上限要有出口：界面与 Agent 工具都必须读得到那句"删过档"。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ra-journal-api-"))
        self.previous = os.environ.get("LOCALAPPDATA")
        # LOCALAPPDATA 指到这里，工具侧的 data_root()/journal.jsonl 与界面侧的才是同一份文件
        os.environ["LOCALAPPDATA"] = str(self.root)
        from ra.main import build

        self.api, self.session, _ = build(self.root / "RecordedAutomation")
        self.api.journal.max_bytes = 3000
        self.api.journal.keep = 1

    def tearDown(self):
        self.session.shutdown()
        if self.previous is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self.previous

    def fill(self, count: int) -> None:
        for index in range(count):
            self.api.journal.append(event(f"r-{index}", phase="step_started"))
            self.api.journal.append(event(f"r-{index}", phase="completed", reason="停在 s1 / some_code"))

    def test_api_list_runs_carries_the_journal_notice(self):
        result = self.api.list_runs()
        self.assertIn("journal", result, "界面读不到日志体积与轮转状态，「历史就是全部」这句没有根据")
        self.assertFalse(result["journal"]["trimmed"])
        self.fill(200)
        result = self.api.list_runs()
        self.assertTrue(result["journal"]["trimmed"])
        self.assertIn("已删除", result["journal"]["note"])

    def test_app_info_carries_it_too(self):
        self.fill(200)
        info = self.api.app_info()
        self.assertIn("journal", info)
        self.assertTrue(info["journal"]["trimmed"])

    def test_run_history_and_status_tools_report_the_trim(self):
        tools = {tool["name"]: tool for tool in load_tools().TOOLS}
        self.fill(200)
        history = tools["ra.run_history"]["handler"]({})
        self.assertIn("journal", history, "Agent 查历史时读不到「日志删过档」，它会以为这就是全部")
        self.assertTrue(history["journal"]["trimmed"])
        status = tools["ra.status"]["handler"]({})
        self.assertIn("journal", status)
        self.assertGreater(status["journal"]["rotations"], 0)


if __name__ == "__main__":
    unittest.main()
