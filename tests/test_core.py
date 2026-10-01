import tempfile
import unittest
from pathlib import Path
from threading import Event

from ra.core import (Action, Locator, RunEvent, Runner, Status, Step, Target, Workflow,
                     workflow_from_dict, workflow_to_dict)
from ra.journal import FileJournal

TARGET = Target("main", (Locator("role", "button", "Submit"),))
AFTER = Target("main", (Locator("text", "Done"),))


class FakeDriver:
    def __init__(self) -> None:
        self.scope_ok = True
        self.act_count = 0
        self.resolve_count = 0
        self.fail_after_action = False
        self.show_after = True
        self.ambiguous = False
        self.last_argument = None
        self.last_timeout = None

    def in_scope(self, origin: str) -> bool:
        return self.scope_ok

    def resolve(self, target: Target, timeout_s: float = 10.0) -> object | None:
        self.resolve_count += 1
        self.last_timeout = timeout_s
        if self.ambiguous:
            raise ValueError("ambiguous target")
        if target == AFTER and not self.show_after:
            return None
        return object()

    def act(self, action: Action, element: object | None, argument: str | None) -> None:
        self.act_count += 1
        self.last_argument = argument
        if self.fail_after_action:
            raise RuntimeError("driver could not confirm the action")


class FakeSecrets:
    def get(self, reference: str) -> str:
        return "private-value"

    def has(self, reference: str) -> bool:
        return reference == "login_password"


class MemoryJournal:
    def __init__(self) -> None:
        self.events = []

    def append(self, event) -> None:
        self.events.append(event)


class RunnerTests(unittest.TestCase):
    def make_runner(self):
        driver = FakeDriver()
        journal = MemoryJournal()
        return Runner(driver, FakeSecrets(), journal, Event(), poll_s=0.001), driver, journal

    def workflow(self, *steps):
        return Workflow("w", "https://example.test", "https://example.test/form", steps)

    def test_action_error_is_uncertain_and_never_retried(self):
        runner, driver, journal = self.make_runner()
        driver.fail_after_action = True

        result = runner.run(self.workflow(Step("s", Action.CLICK, TARGET, expected=AFTER)))

        self.assertEqual(result.status, Status.UNCERTAIN)
        self.assertEqual(driver.act_count, 1)
        self.assertEqual([e.phase for e in journal.events][-1], "uncertain")

    def test_scope_change_prevents_action(self):
        runner, driver, _ = self.make_runner()
        driver.scope_ok = False

        result = runner.run(self.workflow(Step("s", Action.CLICK, TARGET)))

        self.assertEqual(result.status, Status.FAILED)
        self.assertEqual(driver.act_count, 0)

    def test_ambiguous_target_prevents_action(self):
        runner, driver, _ = self.make_runner()
        driver.ambiguous = True

        result = runner.run(self.workflow(Step("s", Action.CLICK, TARGET)))

        self.assertEqual(result.status, Status.FAILED)
        self.assertEqual(driver.act_count, 0)

    def test_missing_postcondition_is_uncertain_without_repeating_action(self):
        runner, driver, _ = self.make_runner()
        driver.show_after = False
        step = Step("s", Action.CLICK, TARGET, expected=AFTER, timeout_s=0.01)

        result = runner.run(self.workflow(step))

        self.assertEqual(result.status, Status.UNCERTAIN)
        self.assertEqual(driver.act_count, 1)

    def test_action_budget_is_passed_to_the_adapter(self):
        runner, driver, _ = self.make_runner()
        runner.run(self.workflow(Step("s", Action.CLICK, TARGET, timeout_s=3.5)))
        self.assertEqual(driver.last_timeout, 3.5)

    def test_locating_is_polled_until_appears(self):
        driver = FakeDriver()
        journal = MemoryJournal()
        calls = []

        def flaky(target, timeout_s=10.0):
            calls.append(target)
            return object() if len(calls) > 2 else None

        driver.resolve = flaky  # type: ignore[assignment]
        runner = Runner(driver, FakeSecrets(), journal, Event(), poll_s=0.001)
        result = runner.run(self.workflow(Step("s", Action.CLICK, TARGET, timeout_s=1.0)))
        self.assertEqual(result.status, Status.COMPLETED_UNVERIFIED)
        self.assertGreater(len(calls), 2)

    def test_cancel_stops_before_next_action(self):
        driver = FakeDriver()
        journal = MemoryJournal()
        stop = Event()
        stop.set()
        runner = Runner(driver, FakeSecrets(), journal, stop, poll_s=0.001)
        result = runner.run(self.workflow(Step("s", Action.CLICK, TARGET)))
        self.assertEqual(result.status, Status.CANCELLED)
        self.assertEqual(driver.act_count, 0)

    def test_secret_is_resolved_at_run_time_and_not_journaled(self):
        runner, driver, journal = self.make_runner()
        result = runner.run(self.workflow(Step("s", Action.FILL, TARGET, secret_ref="login_password")))
        self.assertEqual(result.status, Status.COMPLETED_UNVERIFIED)
        self.assertEqual(driver.last_argument, "private-value")
        self.assertNotIn("private-value", repr(journal.events))

    def test_start_url_must_stay_in_allowed_origin(self):
        with self.assertRaises(ValueError):
            Workflow("w", "https://example.test", "https://other.test/form", (Step("s", Action.CLICK, TARGET),))

    def test_wait_for_step_never_acts(self):
        runner, driver, journal = self.make_runner()
        result = runner.run(self.workflow(Step("s", Action.WAIT_FOR, AFTER)))
        self.assertEqual(result.status, Status.COMPLETED)
        self.assertEqual(driver.act_count, 0)
        self.assertEqual([event.phase for event in journal.events], ["step_started", "verified"])


class ContractTests(unittest.TestCase):
    def test_locator_rules(self):
        with self.assertRaises(ValueError):
            Locator("role", "button")
        with self.assertRaises(ValueError):
            Locator("css", "#a", "extra")
        with self.assertRaises(ValueError):
            Locator("xpath", "//a")

    def test_step_rules(self):
        with self.assertRaises(ValueError):
            Step("s", Action.CLICK)
        with self.assertRaises(ValueError):
            Step("s", Action.FILL, TARGET, text="a", secret_ref="b")
        with self.assertRaises(ValueError):
            Step("s", Action.FILL, TARGET)
        with self.assertRaises(ValueError):
            Step("s", Action.HOTKEY, hotkey=None)
        with self.assertRaises(ValueError):
            Step("s", Action.CLICK, TARGET, text="nope")

    def test_serialisation_round_trip(self):
        workflow = Workflow("wf_x", "https://example.test", "https://example.test/form",
                            (Step("s1", Action.FILL, TARGET, secret_ref="pw"),
                             Step("s2", Action.HOTKEY, None, hotkey="Control+Enter"),
                             Step("s3", Action.WAIT_FOR, AFTER, timeout_s=4.0)), name="示例流程")
        payload = workflow_to_dict(workflow)
        self.assertEqual(payload["name"], "示例流程")
        self.assertNotIn("text", payload["steps"][0])
        self.assertEqual(payload["steps"][1]["hotkey"], "Control+Enter")
        self.assertEqual(payload["steps"][2]["timeout_s"], 4.0)
        self.assertEqual(workflow_from_dict(payload), workflow)


class FileJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.journal = FileJournal(Path(self.tmp) / "journal.jsonl")

    def test_events_are_durable_and_summarised(self):
        self.journal.append(RunEvent("r1", "wf", "s1", "step_started"))
        self.journal.append(RunEvent("r1", "wf", "s1", "action_started"))
        self.journal.append(RunEvent("r1", "wf", "s1", "verified"))
        self.journal.append(RunEvent("r1", "wf", "s1", "completed"))
        self.journal.append(RunEvent("r2", "wf", "s1", "uncertain", "TargetTimeout"))
        events = self.journal.read_run("r1")
        self.assertEqual([item["phase"] for item in events][-1], "completed")
        runs = {row["run_id"]: row for row in self.journal.list_runs()}
        self.assertEqual(runs["r1"]["status"], "completed")
        self.assertEqual(runs["r1"]["steps"], 1)
        self.assertEqual(runs["r2"]["code"], "TargetTimeout")
        self.assertLessEqual(runs["r1"]["started_at"], runs["r1"]["ended_at"])

    def test_journal_has_no_user_input_fields(self):
        self.journal.append(RunEvent("r3", "wf", "s1", "action_started"))
        text = (Path(self.tmp) / "journal.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("text", text)
        self.assertNotIn("value", text)


if __name__ == "__main__":
    unittest.main()
