import json
import tempfile
import unittest
from pathlib import Path

from ra.paths import resource_root
from ra.secrets import FileSecretStore, UnknownSecret
from ra.store import Settings, WorkflowStore

SCHEMA = resource_root() / "workflow.schema.json"

GOOD = {
    "schema_version": 1,
    "id": "wf_contact",
    "name": "客户留言表提交",
    "origin": "https://example.test",
    "start_url": "https://example.test/contact",
    "steps": [
        {"id": "s1", "action": "click",
         "target": {"page": "main", "locators": [{"strategy": "role", "value": "button", "name": "打开留言表"}]},
         "expected": {"page": "main", "locators": [{"strategy": "label", "value": "Email"}]}},
        {"id": "s2", "action": "fill",
         "target": {"page": "main", "locators": [{"strategy": "label", "value": "Email"}]},
         "text": "user@example.test"},
        {"id": "s3", "action": "fill",
         "target": {"page": "main", "locators": [{"strategy": "label", "value": "密码"}]},
         "secret_ref": "example_test_密码_1"},
    ],
}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = WorkflowStore(Path(self.tmp) / "workflows", SCHEMA)

    def payload(self, **patch):
        return {**GOOD, **patch}

    def test_good_workflow_passes_both_validation_layers(self):
        report = self.store.problems(GOOD)
        self.assertTrue(report["schema_ok"], report)
        self.assertEqual(report["blocking"], [])
        self.assertEqual(report["warnings"], [])

    def test_role_locator_needs_accessible_name(self):
        payload = self.payload(steps=[{"id": "s1", "action": "click",
                                       "target": {"page": "main", "locators": [{"strategy": "role", "value": "button"}]}}])
        self.assertTrue(self.store.problems(payload)["blocking"])

    def test_off_origin_start_url_is_rejected(self):
        report = self.store.problems(self.payload(start_url="https://other.test/contact"))
        self.assertTrue(report["blocking"])

    def test_duplicate_step_ids_are_rejected(self):
        payload = self.payload(steps=[GOOD["steps"][0], GOOD["steps"][0]])
        blocking = self.store.problems(payload)["blocking"]
        self.assertTrue(any("unique" in item for item in blocking), blocking)

    def test_token_in_start_url_blocks_saving(self):
        blocking = self.store.problems(self.payload(start_url="https://example.test/contact?access_key=abc"))["blocking"]
        self.assertTrue(any("令牌" in item for item in blocking), blocking)

    def test_fill_needs_text_or_secret_ref_not_both(self):
        bad = self.payload(steps=[{"id": "s1", "action": "fill",
                                   "target": {"page": "main", "locators": [{"strategy": "label", "value": "Email"}]},
                                   "text": "a", "secret_ref": "b"}])
        self.assertTrue(self.store.problems(bad)["blocking"])

    def test_click_may_warn_about_missing_completion_condition(self):
        payload = self.payload(steps=[{"id": "s1", "action": "click",
                                       "target": {"page": "main",
                                                  "locators": [{"strategy": "role", "value": "button", "name": "发送"}]}}])
        report = self.store.problems(payload)
        self.assertEqual(report["blocking"], [])
        self.assertTrue(any("完成条件" in item for item in report["warnings"]), report)

    def test_save_load_list_delete(self):
        self.store.save(GOOD)
        path = Path(self.tmp) / "workflows" / "wf_contact.workflow.json"
        self.assertTrue(path.exists())
        self.assertEqual(self.store.load("wf_contact")["name"], "客户留言表提交")
        rows = self.store.list()
        self.assertEqual(rows[0]["id"], "wf_contact")
        self.assertEqual(rows[0]["steps"], 3)
        self.assertEqual(rows[0]["secret_refs"], ["example_test_密码_1"])
        self.assertEqual(rows[0]["unverified"], 2)
        self.store.delete("wf_contact")
        with self.assertRaises(KeyError):
            self.store.load("wf_contact")

    def test_save_strips_ui_only_labels(self):
        payload = self.payload(steps=[dict(step, label="点击 按钮「打开留言表」") for step in GOOD["steps"]])
        self.store.save(payload)
        saved = json.loads((Path(self.tmp) / "workflows" / "wf_contact.workflow.json").read_text(encoding="utf-8"))
        self.assertNotIn("label", saved["steps"][0])

    def test_save_refuses_blocked_payload(self):
        with self.assertRaises(ValueError):
            self.store.save(self.payload(id="bad id!!"))

    def test_broken_file_is_reported_not_fatal(self):
        broken = Path(self.tmp) / "workflows" / "broken.workflow.json"
        broken.write_text("{not json", encoding="utf-8")
        rows = self.store.list()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["broken"])
        self.assertIn("updated_at", rows[0])


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "secrets.json"
        self.store = FileSecretStore(self.path)

    def test_value_round_trip_and_never_plaintext_on_disk(self):
        self.store.set("site_pwd", "s3cr3t-typed")
        self.assertEqual(self.store.get("site_pwd"), "s3cr3t-typed")
        self.assertNotIn("s3cr3t-typed", self.path.read_text(encoding="utf-8"))

    def test_preview_exposes_only_names(self):
        self.store.set("a", "long-secret-value")
        preview = self.store.preview()
        self.assertEqual(preview, [{"ref": "a", "length": len("long-secret-value")}])
        self.assertNotIn("long-secret-value", json.dumps(preview, ensure_ascii=False))

    def test_reload_from_disk(self):
        self.store.set("b", "value-b")
        self.assertEqual(FileSecretStore(self.path).get("b"), "value-b")

    def test_missing_reference_raises(self):
        with self.assertRaises(UnknownSecret):
            self.store.get("nope")
        self.store.set("c", "x")
        self.store.delete("c")
        self.assertFalse(self.store.has("c"))


class SettingsTests(unittest.TestCase):
    def test_defaults_and_persistence(self):
        tmp = Path(tempfile.mkdtemp()) / "settings.json"
        settings = Settings(tmp)
        self.assertEqual(settings.all()["default_timeout_s"], 10.0)
        self.assertFalse(settings.all()["headless"])
        settings.update({"headless": True, "unknown_key": 1})
        self.assertTrue(Settings(tmp).all()["headless"])
        self.assertNotIn("unknown_key", Settings(tmp).all())


if __name__ == "__main__":
    unittest.main()
