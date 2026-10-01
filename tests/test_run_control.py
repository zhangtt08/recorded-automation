"""运行控制面的回归：缺秘密值要列出全部引用名、从失败步续跑、历史运行按日志终态读、长日志只读尾部。"""

import tempfile
import unittest
from pathlib import Path

from ra.core import RunEvent
from ra.main import build

WF = {
    "schema_version": 1,
    "id": "wf_secrets",
    "name": "两个秘密引用",
    "origin": "https://example.test",
    "start_url": "https://example.test/form",
    "steps": [
        {"id": "s1", "action": "fill",
         "target": {"page": "main", "locators": [{"strategy": "label", "value": "用户"}]}, "secret_ref": "ref_user"},
        {"id": "s2", "action": "fill",
         "target": {"page": "main", "locators": [{"strategy": "label", "value": "密码"}]}, "secret_ref": "ref_pass"},
        {"id": "s3", "action": "click",
         "target": {"page": "main", "locators": [{"strategy": "role", "value": "button", "name": "登录"}]},
         "expected": {"page": "main", "locators": [{"strategy": "text", "value": "欢迎"}]}},
    ],
}


class MissingSecretsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ra-secrets-"))
        self.api, self.session, _ = build(self.root)
        self.session.settings.update({"headless": True})

    def tearDown(self):
        self.session.shutdown()

    def test_names_every_missing_reference_at_once(self):
        self.assertEqual(self.session.missing_secrets(WF), ["ref_pass", "ref_user"])
        result = self.api.run_workflow("does_not_exist")
        self.assertIn("error", result)
        self.api.store.save(WF)
        blocked = self.api.run_workflow("wf_secrets")
        self.assertEqual(blocked.get("missing_secret"), ["ref_pass", "ref_user"])
        self.assertIn("ref_pass", blocked["error"])
        self.assertIn("ref_user", blocked["error"])
        self.assertEqual(self.session.state, "idle")            # 被拦下时没有启动浏览器

    def test_stored_reference_stops_being_reported(self):
        self.session.secrets.set("ref_user", "u")
        self.assertEqual(self.session.missing_secrets(WF), ["ref_pass"])


class ResumeFromStepTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ra-resume-"))
        self.api, self.session, _ = build(self.root)

    def tearDown(self):
        self.session.shutdown()

    def test_from_step_rejects_an_unknown_step_id(self):
        self.api.store.save(WF)
        for step in WF["steps"]:
            if step.get("secret_ref"):
                self.session.secrets.set(step["secret_ref"], "v")
        unknown = self.api.run_workflow("wf_secrets", "s99")
        self.assertIn("步骤 s99", unknown.get("error", ""))

    def test_api_slices_the_steps_and_reports_what_was_skipped(self):
        self.api.store.save(WF)
        for step in WF["steps"]:
            if step.get("secret_ref"):
                self.session.secrets.set(step["secret_ref"], "v")
        captured = {}

        def fake_start_run(payload, run_id, resumed=None):
            captured.update(payload=payload, resumed=resumed)
            return {"run_id": run_id, "steps": len(payload["steps"])}

        self.session.start_run = fake_start_run
        started = self.api.run_workflow("wf_secrets", "s2")
        self.assertEqual(started["steps"], 2)
        self.assertEqual([step["id"] for step in captured["payload"]["steps"]], ["s2", "s3"])
        self.assertEqual(captured["resumed"], {"from_step": "s2", "skipped": 1, "total_steps": 3})
        self.assertEqual(started["from_step"], "s2")
        self.assertEqual(started["skipped"], 1)

    def test_resumed_information_reaches_the_run_detail(self):
        self.api.store.save(WF)
        live = {"run_id": "run_resume", "workflow_id": "wf_secrets", "status": "completed",
                "started_at": 100.0, "ended_at": 102.0, "events": [],
                "resumed": {"from_step": "s2", "skipped": 1, "total_steps": 3}}
        detail = self.api._detail("run_resume", live)
        self.assertEqual(detail["resumed"]["skipped"], 1)
        self.assertEqual(detail["resumed"]["from_step"], "s2")
        self.assertEqual(detail["steps_total"], 3)

    def test_full_run_is_not_marked_resumable(self):
        self.api.store.save(WF)
        live = {"run_id": "run_ok", "workflow_id": "wf_secrets", "status": "completed",
                "started_at": 1.0, "ended_at": 2.0, "events": []}
        self.assertFalse(self.api._detail("run_ok", live)["resumable"])


class JournalDetailTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ra-detail-"))
        self.api, self.session, _ = build(self.root)

    def tearDown(self):
        self.session.shutdown()

    def test_finished_run_is_read_from_the_journal_not_from_memory(self):
        self.api.store.save(WF)
        # 内存里没有这次运行（模拟程序重启后从历史打开），终态只能来自日志。
        for phase, code in (("step_started", ""), ("failed_before_action", "AmbiguousTarget"), ("failed", "AmbiguousTarget")):
            self.session.journal.append(RunEvent("run_old", "wf_secrets", "s1", phase, code))
        detail = self.api._detail("run_old", self.session.run_status("run_old"))
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["status_label"], "失败")
        self.assertEqual(detail["code"], "AmbiguousTarget")
        self.assertEqual(detail["step_id"], "s1")
        self.assertTrue(detail["resumable"])
        self.assertNotEqual(detail["status"], "unknown")

    def test_run_without_terminal_event_is_reported_as_no_record(self):
        self.api.store.save(WF)
        self.session.journal.append(RunEvent("run_half", "wf_secrets", "s1", "step_started", ""))
        detail = self.api._detail("run_half", self.session.run_status("run_half"))
        self.assertEqual(detail["status"], "unknown")
        self.assertEqual(detail["status_label"], "无记录")

    def test_counts_unverified_steps_for_the_explanation(self):
        self.api.store.save(WF)
        for step in ("s1", "s2", "s3"):
            self.session.journal.append(RunEvent("run_unv", "wf_secrets", step, "step_started", ""))
        for step in ("s1", "s2"):
            self.session.journal.append(RunEvent("run_unv", "wf_secrets", step, "executed_unverified", ""))
        self.session.journal.append(RunEvent("run_unv", "wf_secrets", "s3", "verified", ""))
        self.session.journal.append(RunEvent("run_unv", "wf_secrets", "s3", "completed_unverified", ""))
        detail = self.api._detail("run_unv", self.session.run_status("run_unv"))
        self.assertEqual(detail["steps_unverified"], 2)


class LongJournalTests(unittest.TestCase):
    """列表只看日志尾部（快），单条运行的事件仍是全量读（准）。"""

    RUNS = 5000

    def build_journal(self):
        import json
        from ra import journal as journal_module

        path = Path(tempfile.mkdtemp(prefix="ra-journal-")) / "journal.jsonl"
        lines = [json.dumps({"at": 1000 + index, "run_id": f"r{index}", "workflow_id": "wf",
                             "step_id": "s1", "phase": "step_started", "code": ""})
                 for index in range(self.RUNS)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path, journal_module

    def test_list_runs_is_bounded_by_the_tail_window(self):
        path, module = self.build_journal()
        original = module.TAIL_BYTES
        module.TAIL_BYTES = 2000                     # 只看最后两千字节：更早的运行不该被数进来
        try:
            runs = module.FileJournal(path).list_runs(limit=self.RUNS)
        finally:
            module.TAIL_BYTES = original
        self.assertEqual(runs[0]["run_id"], f"r{self.RUNS - 1}")
        self.assertGreater(len(runs), 1)
        self.assertLess(len(runs), self.RUNS)

    def test_read_run_still_scans_whole_file(self):
        from ra import journal as journal_module

        path, module = self.build_journal()
        journal = module.FileJournal(path)
        original = module.TAIL_BYTES
        module.TAIL_BYTES = 2000
        try:
            self.assertEqual(len(journal.read_run("r0")), 1)      # 最早那次也读得到
        finally:
            module.TAIL_BYTES = original
        self.assertEqual(len(journal.list_runs(limit=3)), 3)


if __name__ == "__main__":
    unittest.main()
