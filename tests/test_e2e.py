"""端到端测试：直接调用程序自带的自检（真实 Chromium + 本地站点）。"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from ra.main import build
from ra.selfcheck import run
from ra.session import BusyError


class SelfCheckIntegration(unittest.TestCase):
    def test_every_integration_case_passes(self):
        root = Path(tempfile.mkdtemp(prefix="ra-e2e-"))
        result = run(root)
        failed = [item for item in result["results"] if not item["ok"]]
        self.assertEqual(failed, [], json.dumps(failed, ensure_ascii=False))
        self.assertGreaterEqual(len(result["results"]), 10)


class SessionGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="ra-session-"))
        api, session, _ = build(cls.root)
        api.settings.update({"headless": True})
        cls.api, cls.session = api, session

    @classmethod
    def tearDownClass(cls):
        cls.session.shutdown()

    def setUp(self):
        # 同一浏览器实例在各测试间复用，但草稿与状态从干净开始。
        self.session.draft = None
        self.session.recorder = None
        self.session.state = "idle"

    def test_run_before_recording_is_rejected_and_draft_is_empty(self):
        self.assertIsNone(self.session.draft)
        self.assertEqual(self.session.state, "idle")
        with self.assertRaises(BusyError):
            self.session.edit_draft("s1", {"value": "x"})
        with self.assertRaises(BusyError):
            self.session.refresh_draft()
        with self.assertRaises(BusyError):
            self.session.build_draft("wf_x")

    def test_invalid_urls_are_rejected_before_the_browser_is_used(self):
        for bad in ("", "ftp://example.test", "not a url at all"):
            with self.assertRaises(ValueError):
                self.session.start_recording(bad)

    def test_run_needs_secret_values_before_starting(self):
        payload = {
            "schema_version": 1, "id": "wf_needs", "origin": "https://example.test",
            "start_url": "https://example.test/form",
            "steps": [{"id": "s1", "action": "fill", "secret_ref": "not_in_vault",
                       "target": {"page": "main", "locators": [{"strategy": "label", "value": "密码"}]}}],
        }
        with self.assertRaises(BusyError):
            self.session.start_run(payload, "needs1")
        self.assertEqual(self.session.state, "idle")

    def test_recording_editor_and_run_detail(self):
        from ra.fixture import FixtureSite

        site = FixtureSite().start()
        try:
            self.session.start_recording(site.url)
            self.session.page_action(lambda page: page.locator("#open-form").click())
            time.sleep(0.6)
            draft = self.session.stop_recording()
            self.assertEqual(len(draft["steps"]), 1)
            built = self.session.build_draft("wf_detail", "详情检查")
            self.assertEqual(built["problems"], [])
            saved = self.api.save_workflow(built["workflow"])
            self.assertNotIn("error", saved)

            started = self.session.start_run(built["workflow"], "detail1")
            deadline = time.time() + 40
            while self.session.run_status(started["run_id"])["status"] == "running" and time.time() < deadline:
                time.sleep(0.2)
            status = self.session.run_status(started["run_id"])
            detail = self.api._detail(started["run_id"], status)
            self.assertEqual(detail["steps_total"], 1)
            self.assertEqual(detail["timeline"][0]["action"], "click")
            self.assertEqual(detail["timeline"][0]["state_label"], "未验证 · 无完成条件")
            self.assertIn(status["status"], {"completed", "completed_unverified"})

            opened = self.api.open_in_editor("wf_detail")
            self.assertNotIn("error", opened, opened)
            self.assertEqual(opened["draft"]["steps"][0]["status"], "unchecked")
            self.assertEqual(opened["workflow"]["name"], "详情检查")
        finally:
            site.stop()


if __name__ == "__main__":
    unittest.main()
