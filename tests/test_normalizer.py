import json
import unittest

from ra.normalizer import Draft, build_target, status_of, step_label
from ra.recorder import Captured, Option, describe_target, origin_of


def option(strategy, value, count, name=None):
    return Option(strategy, value, name, count)


def captured(kind, title, options, value=None, sensitive=False, hotkey=None, at=1000.0):
    return Captured(kind, at, "https://example.test/form", None, title, options, value=value,
                    sensitive=sensitive, hotkey=hotkey)


class OriginTests(unittest.TestCase):
    def test_default_ports_are_stripped(self):
        self.assertEqual(origin_of("https://example.test/page"), "https://example.test")
        self.assertEqual(origin_of("http://example.test:80/x"), "http://example.test")
        self.assertEqual(origin_of("http://example.test:8080/x"), "http://example.test:8080")
        self.assertEqual(origin_of("not a url"), "")

    def test_status_vocabulary(self):
        self.assertEqual(status_of({"count": 1}), "unique")
        self.assertEqual(status_of({"count": 3}), "ambiguous")
        self.assertEqual(status_of({"count": 0}), "missing")
        self.assertEqual(status_of({"count": -1}), "unchecked")
        self.assertEqual(status_of(None), "missing")


class DescribeTests(unittest.TestCase):
    def test_role_and_name(self):
        self.assertEqual(describe_target({"role": "button", "name": "发送"}), "按钮「发送」")
        self.assertEqual(describe_target({"role": "textbox", "type": "password", "label": "密码"}), "密码框「密码」")
        self.assertEqual(describe_target({"role": "unknown"}), "元素")


class DraftTests(unittest.TestCase):
    def make(self):
        return Draft("https://example.test", "https://example.test/form")

    def test_consecutive_input_events_merge_into_one_fill(self):
        draft = self.make()
        opts = [option("label", "Email", 1), option("css", "#email", 1)]
        draft.add(captured("fill", "输入框「Email」", opts, value="u@"))
        draft.add(captured("fill", "输入框「Email」", opts, value="user@example.test"))
        self.assertEqual(len(draft.steps), 1)
        step = draft.steps[0]
        self.assertEqual(step["kind"], "merge")
        self.assertEqual(step["value"], "user@example.test")
        self.assertEqual(step["merged"], 1)
        self.assertEqual(step["status"], "unique")

    def test_duplicate_click_on_same_target_is_deduped(self):
        draft = self.make()
        opts = [option("role", "button", 1, name="发送")]
        draft.add(captured("click", "按钮「发送」", opts, at=1000.0))
        draft.add(captured("click", "按钮「发送」", opts, at=1000.4))
        self.assertEqual(len(draft.steps), 1)
        draft.add(captured("click", "按钮「发送」", opts, at=1005.0))
        self.assertEqual(len(draft.steps), 2)

    def test_interleaved_target_breaks_the_merge(self):
        draft = self.make()
        email = [option("label", "Email", 1)]
        name = [option("label", "姓名", 1)]
        draft.add(captured("fill", "输入框「Email」", email, value="a"))
        draft.add(captured("fill", "输入框「姓名」", name, value="b"))
        draft.add(captured("fill", "输入框「Email」", email, value="c"))
        self.assertEqual([step["value"] for step in draft.steps], ["a", "b", "c"])

    def test_sensitive_input_never_keeps_value(self):
        draft = self.make()
        draft.add(captured("fill", "密码框「密码」", [option("label", "密码", 1)], value=None, sensitive=True))
        step = draft.steps[0]
        self.assertTrue(step["sensitive"])
        self.assertIsNone(step["value"])
        self.assertEqual(step["secret_ref"], "example_test_密码_1")
        text = json.dumps(draft.build("wf_login", "登录")["workflow"], ensure_ascii=False)
        self.assertIn("secret_ref", text)
        self.assertNotIn('"text"', text)

    def test_ambiguous_selected_locator_blocks_saving(self):
        draft = self.make()
        draft.add(captured("click", "按钮「复制」", [option("role", "button", 2, name="复制"),
                                                  option("css", "section#dupes button.copy", 2)]))
        step = draft.steps[0]
        self.assertEqual(step["status"], "ambiguous")
        built = draft.build("wf_ambiguous", "含多匹配")
        self.assertTrue(built["problems"])
        self.assertIn("s1", built["problems"][0])

    def test_unique_css_is_selected_when_role_is_ambiguous(self):
        draft = self.make()
        draft.add(captured("click", "按钮「复制」", [option("role", "button", 2, name="复制"),
                                                  option("css", "#dupes button:nth-of-type(1)", 1)]))
        step = draft.steps[0]
        self.assertEqual(step["status"], "unique")
        self.assertEqual(step["selected"], 1)
        self.assertEqual(step["options"][step["selected"]]["strategy"], "css")
        built = draft.build("wf_dupes", "复制第一个")
        self.assertEqual(built["problems"], [])
        self.assertEqual(built["workflow"]["steps"][0]["target"]["locators"],
                         [{"strategy": "css", "value": "#dupes button:nth-of-type(1)"}])

    def test_refresh_recounts_from_the_live_page(self):
        draft = self.make()
        draft.add(captured("click", "按钮「发送」", [option("role", "button", 1, name="发送")]))
        seen = []

        def evaluate(step):
            seen.append(step["id"])
            return [{"strategy": "role", "value": "button", "name": "发送", "count": 3, "note": ""}]

        summary = draft.refresh(evaluate)
        self.assertEqual(summary["refreshed"], 1)
        self.assertEqual(draft.steps[0]["status"], "ambiguous")
        self.assertEqual(seen, ["s1"])

    def test_reorder_and_remove_renumber_ids(self):
        draft = self.make()
        draft.add(captured("click", "A", [option("css", "#a", 1)]))
        draft.add(captured("click", "B", [option("css", "#b", 1)]))
        draft.add(captured("fill", "C", [option("label", "Email", 1)], value="x"))
        draft.move("s3", -2)
        self.assertEqual([step["title"] for step in draft.steps], ["C", "A", "B"])
        draft.remove("s2")
        self.assertEqual(draft.ids(), ["s1", "s2"])
        self.assertEqual([step["title"] for step in draft.steps], ["C", "B"])

    def test_expected_can_reference_another_step(self):
        draft = self.make()
        draft.add(captured("click", "打开", [option("css", "#open", 1)]))
        draft.add(captured("fill", "Email", [option("label", "Email", 1)], value="a@b.test"))
        draft.edit("s1", {"expected": {"from_step": "s2"}})
        built = draft.build("wf_exp", "含完成条件")
        self.assertEqual(built["problems"], [])
        self.assertEqual(built["workflow"]["steps"][0]["expected"]["locators"][0]["strategy"], "label")

    def test_hotkey_step_keeps_combination(self):
        draft = self.make()
        draft.add(captured("hotkey", "键盘组合", [], at=1.0, hotkey="Control+Enter"))
        built = draft.build("wf_key", "提交快捷键")
        self.assertEqual(built["problems"], [])
        self.assertEqual(built["workflow"]["steps"][0],
                         {"id": "s1", "action": "hotkey", "hotkey": "Control+Enter", "timeout_s": 10.0})

    def test_build_strips_locators_that_are_not_unique(self):
        draft = self.make()
        draft.add(captured("click", "按钮", [option("role", "button", 4, name="复制"),
                                           option("css", "#dupes .copy", 4)]))
        self.assertIsNone(build_target(draft.steps[0]))

    def test_role_locator_without_name_is_rejected(self):
        draft = self.make()
        draft.add(captured("click", "按钮", [option("role", "button", 1, name=None)]))
        draft.edit("s1", {"options": [{"strategy": "role", "value": "button", "count": 1}]})
        self.assertIsNone(build_target(draft.steps[0]))

    def test_manual_step_needs_a_locator_before_saving(self):
        draft = self.make()
        step = draft.append_manual("wait_for")
        self.assertEqual(step["status"], "missing")
        self.assertTrue(draft.build("wf_manual")["problems"])
        draft.edit("s1", {"options": [{"strategy": "text", "value": "留言已收到", "count": 1}]})
        self.assertEqual(draft.build("wf_manual")["problems"], [])

    def test_loaded_workflow_starts_unchecked_but_savable(self):
        payload = {
            "schema_version": 1, "id": "wf_back", "name": "回读", "origin": "https://example.test",
            "start_url": "https://example.test/form",
            "steps": [{"id": "s1", "action": "click",
                       "target": {"page": "main", "locators": [{"strategy": "label", "value": "Email"}]}}],
        }
        draft = Draft.from_workflow(payload)
        self.assertEqual(draft.steps[0]["status"], "unchecked")
        self.assertEqual(draft.steps[0]["title"], "点击 输入框「Email」")
        built = draft.build("wf_back", "回读")
        self.assertEqual(built["problems"], [])
        self.assertEqual(built["workflow"]["steps"][0]["target"]["locators"],
                         [{"strategy": "label", "value": "Email"}])

    def test_step_label_reads_naturally(self):
        self.assertEqual(step_label({"action": "click", "target": {"locators": [{"strategy": "role", "value": "button", "name": "发送"}]}}), "点击 按钮「发送」")
        self.assertEqual(step_label({"action": "fill", "target": {"locators": [{"strategy": "label", "value": "Email"}]}}), "填写 输入框「Email」")
        self.assertEqual(step_label({"action": "hotkey", "hotkey": "Enter"}), "按下 Enter")


if __name__ == "__main__":
    unittest.main()
