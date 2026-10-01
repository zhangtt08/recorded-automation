"""Platform-neutral contracts and replay policy for recorded UI workflows.

Platform adapters implement Driver, SecretStore, and Journal.  This module never
imports a UI toolkit, OS automation library, or persistence framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Event
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4


class Action(str, Enum):
    CLICK = "click"
    FILL = "fill"
    HOTKEY = "hotkey"
    WAIT_FOR = "wait_for"


class Status(str, Enum):
    COMPLETED = "completed"
    COMPLETED_UNVERIFIED = "completed_unverified"
    FAILED = "failed"             # No action was attempted for the failed step.
    UNCERTAIN = "uncertain"       # Action may have happened; never auto-resume.
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Locator:
    strategy: str                  # role, label, test_id, text, css
    value: str                     # For role, this is the role (e.g. button).
    name: str | None = None        # Accessible name when strategy is role.

    def __post_init__(self) -> None:
        if self.strategy not in {"role", "label", "test_id", "text", "css"} or not self.value:
            raise ValueError("Unsupported or empty locator")
        if self.strategy == "role" and not self.name:
            raise ValueError("role locator needs an accessible name")
        if self.strategy != "role" and self.name is not None:
            raise ValueError("Only role locators may have a name")


@dataclass(frozen=True)
class Target:
    page: str                      # Logical page key, e.g. main or a named popup.
    locators: tuple[Locator, ...]  # Ordered, most reliable first.
    frame: str | None = None       # Explicit frame selector; None means main frame.

    def __post_init__(self) -> None:
        if not self.page or not self.locators:
            raise ValueError("Target needs a page and at least one locator")


@dataclass(frozen=True)
class Step:
    id: str
    action: Action
    target: Target | None = None
    text: str | None = None
    secret_ref: str | None = None
    hotkey: str | None = None
    expected: Target | None = None
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if not self.id or self.timeout_s <= 0:
            raise ValueError("Step needs an id and positive timeout")
        if self.action in (Action.CLICK, Action.FILL, Action.WAIT_FOR) and self.target is None:
            raise ValueError(f"{self.action.value} needs a target")
        if self.action == Action.FILL and (self.text is None) == (self.secret_ref is None):
            raise ValueError("fill needs exactly one of text or secret_ref")
        if self.action == Action.HOTKEY and not self.hotkey:
            raise ValueError("hotkey needs a key combination")
        if self.action != Action.FILL and (self.text is not None or self.secret_ref is not None):
            raise ValueError("only fill may have input text")
        if self.action != Action.HOTKEY and self.hotkey is not None:
            raise ValueError("only hotkey may have a key combination")


@dataclass(frozen=True)
class Workflow:
    id: str
    origin: str                    # Allowed browser origin, e.g. https://example.com.
    start_url: str                 # URL the session coordinator opens before replay.
    steps: tuple[Step, ...]
    name: str = ""                 # Display label; defaults to the id.
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.id or not self.origin or not self.start_url or not self.steps:
            raise ValueError("Unsupported or incomplete workflow")
        parsed = urlsplit(self.origin)
        start = urlsplit(self.start_url)
        try:
            origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            start_port = start.port or (443 if start.scheme == "https" else 80)
        except ValueError as exc:
            raise ValueError("Invalid URL port") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("Workflow origin must be a web origin without a path")
        if start.username or start.password or (start.scheme, start.hostname, start_port) != (parsed.scheme, parsed.hostname, origin_port):
            raise ValueError("start_url must belong to workflow origin")
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Step ids must be unique")


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    workflow_id: str
    step_id: str
    phase: str                    # step_started/action_started/action_returned/verified/...
    code: str = ""                # Stable error code, never user input or secret data.


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: Status
    step_id: str | None
    code: str = ""


class Driver(Protocol):
    def in_scope(self, origin: str) -> bool:
        """Confirm the active browser page still belongs to the allowed origin."""

    def resolve(self, target: Target, timeout_s: float = 10.0) -> object | None:
        """Return a unique visible locator, None if absent, or raise if ambiguous.

        timeout_s bounds the later action so no single browser call can hang.
        """

    def act(self, action: Action, element: object | None, argument: str | None) -> None:
        """Perform once. Recheck focus/element validity immediately before acting."""


class SecretStore(Protocol):
    def get(self, reference: str) -> str:
        """Resolve a secret at execution time; workflow and journal store only its name."""


class Journal(Protocol):
    def append(self, event: RunEvent) -> None:
        """Durably append before returning, especially for action_started."""


class RunCancelled(Exception):
    pass


class PageOutOfScope(Exception):
    pass


class TargetTimeout(Exception):
    pass


class Runner:
    """Sequential replay. Only locating/waiting is retried; an action is never retried."""

    def __init__(
        self,
        driver: Driver,
        secrets: SecretStore,
        journal: Journal,
        stop: Event,
        poll_s: float = 0.1,
    ) -> None:
        if poll_s <= 0:
            raise ValueError("poll_s must be positive")
        self.driver = driver
        self.secrets = secrets
        self.journal = journal
        self.stop = stop
        self.poll_s = poll_s

    def run(self, workflow: Workflow, run_id: str | None = None) -> RunResult:
        run_id = run_id or uuid4().hex
        unverified = False
        for step in workflow.steps:
            try:
                self._emit(run_id, workflow, step, "step_started")
                self._guard(workflow.origin)
                element = self._wait_for(workflow.origin, step.target, step.timeout_s)
                if step.action == Action.WAIT_FOR:
                    self._emit(run_id, workflow, step, "verified")
                    continue
                argument = self._argument(step)
                self._guard(workflow.origin)
                # The durable marker is written BEFORE a possible side effect.
                self._emit(run_id, workflow, step, "action_started")
            except RunCancelled:
                self._try_emit(run_id, workflow, step, "cancelled")
                return RunResult(run_id, Status.CANCELLED, step.id)
            except Exception as exc:
                code = type(exc).__name__
                self._try_emit(run_id, workflow, step, "failed_before_action", code)
                return RunResult(run_id, Status.FAILED, step.id, code)

            try:
                # The adapter must use bounded calls. A blocked OS call cannot be
                # safely cancelled by this policy layer.
                self.driver.act(step.action, element, argument)
                self._emit(run_id, workflow, step, "action_returned")
                if step.expected is not None:
                    self._wait_for(workflow.origin, step.expected, step.timeout_s)
                    self._emit(run_id, workflow, step, "verified")
                else:
                    unverified = True
                    self._emit(run_id, workflow, step, "executed_unverified")
            except Exception as exc:
                # An adapter error, postcondition timeout, or stop request after
                # action_started cannot prove whether the UI side effect happened.
                code = type(exc).__name__
                self._try_emit(run_id, workflow, step, "uncertain", code)
                return RunResult(run_id, Status.UNCERTAIN, step.id, code)

        return RunResult(
            run_id,
            Status.COMPLETED_UNVERIFIED if unverified else Status.COMPLETED,
            workflow.steps[-1].id,
        )

    def _argument(self, step: Step) -> str | None:
        if step.action == Action.FILL:
            return self.secrets.get(step.secret_ref) if step.secret_ref else step.text
        if step.action == Action.HOTKEY:
            return step.hotkey
        return None

    def _wait_for(self, origin: str, target: Target | None, timeout_s: float) -> object | None:
        if target is None:
            return None
        deadline = monotonic() + timeout_s
        while True:
            self._guard(origin)
            element = self.driver.resolve(target, timeout_s)
            if element is not None:
                return element
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TargetTimeout()
            self.stop.wait(min(self.poll_s, remaining))

    def _guard(self, origin: str) -> None:
        if self.stop.is_set():
            raise RunCancelled()
        if not self.driver.in_scope(origin):
            raise PageOutOfScope()

    def _emit(self, run_id: str, workflow: Workflow, step: Step, phase: str, code: str = "") -> None:
        self.journal.append(RunEvent(run_id, workflow.id, step.id, phase, code))

    def _try_emit(self, run_id: str, workflow: Workflow, step: Step, phase: str, code: str = "") -> None:
        try:
            self._emit(run_id, workflow, step, phase, code)
        except Exception:
            # The result still reports the failure. Never attempt another UI action.
            pass


def locator_to_dict(locator: Locator) -> dict:
    payload = {"strategy": locator.strategy, "value": locator.value}
    if locator.name is not None:
        payload["name"] = locator.name
    return payload


def target_to_dict(target: Target) -> dict:
    payload = {"page": target.page, "locators": [locator_to_dict(item) for item in target.locators]}
    if target.frame:
        payload["frame"] = target.frame
    return payload


def step_to_dict(step: Step) -> dict:
    payload: dict = {"id": step.id, "action": step.action.value}
    if step.target is not None:
        payload["target"] = target_to_dict(step.target)
    if step.text is not None:
        payload["text"] = step.text
    if step.secret_ref is not None:
        payload["secret_ref"] = step.secret_ref
    if step.hotkey is not None:
        payload["hotkey"] = step.hotkey
    if step.expected is not None:
        payload["expected"] = target_to_dict(step.expected)
    if step.timeout_s != 10.0:
        payload["timeout_s"] = step.timeout_s
    return payload


def workflow_to_dict(workflow: Workflow) -> dict:
    return {
        "schema_version": workflow.schema_version,
        "id": workflow.id,
        "name": workflow.name or workflow.id,
        "origin": workflow.origin,
        "start_url": workflow.start_url,
        "steps": [step_to_dict(step) for step in workflow.steps],
    }


def target_from_dict(payload: dict) -> Target:
    locators = tuple(
        Locator(item["strategy"], item["value"], item.get("name"))
        for item in payload.get("locators") or []
    )
    return Target(payload.get("page") or "main", locators, payload.get("frame"))


def step_from_dict(payload: dict) -> Step:
    target = payload.get("target")
    expected = payload.get("expected")
    return Step(
        id=payload["id"],
        action=Action(payload["action"]),
        target=target_from_dict(target) if target else None,
        text=payload.get("text"),
        secret_ref=payload.get("secret_ref"),
        hotkey=payload.get("hotkey"),
        expected=target_from_dict(expected) if expected else None,
        timeout_s=float(payload.get("timeout_s", 10.0)),
    )


def workflow_from_dict(payload: dict) -> Workflow:
    return Workflow(
        id=payload["id"],
        origin=payload["origin"],
        start_url=payload["start_url"],
        steps=tuple(step_from_dict(item) for item in payload.get("steps") or []),
        name=payload.get("name", ""),
        schema_version=int(payload.get("schema_version", 1)),
    )
