"""Playwright adapter implementing the platform-neutral Driver contract.

Locating is retried by the Runner; an action is issued exactly once, and the page
origin plus locator uniqueness are rechecked immediately before it is issued.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page

from .core import Action, Locator as StepLocator, Target


class AmbiguousTarget(RuntimeError):
    """More than one element matched — the step must be fixed by the user."""


class MissingPage(RuntimeError):
    pass


class MissingFrame(RuntimeError):
    pass


class MissingTarget(RuntimeError):
    pass


class OutOfScope(RuntimeError):
    pass


@dataclass
class Resolved:
    """A lazily re-resolvable locator plus the budget to spend on acting."""

    locator: Locator
    timeout_ms: float


def same_origin(left: str, right: str) -> bool:
    def parts(url: str):
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname, port

    try:
        return bool(urlsplit(left).hostname) and parts(left) == parts(right)
    except ValueError:
        return False


Scope = Page | Frame


def build_locator(scope: Scope, locator: StepLocator) -> Locator:
    strategy = locator.strategy
    if strategy == "role":
        return scope.get_by_role(locator.value, name=locator.name, exact=True)
    if strategy == "label":
        return scope.get_by_label(locator.value, exact=True)
    if strategy == "test_id":
        return scope.get_by_test_id(locator.value)
    if strategy == "text":
        return scope.get_by_text(locator.value, exact=True)
    return scope.locator(locator.value)


class PlaywrightDriver:
    """Driver bound to the single controlled tab of the session."""

    def __init__(self, page: Page, origin: str) -> None:
        self.page = page
        self.origin = origin

    def in_scope(self, origin: str) -> bool:
        url = self.page.url or ""
        if not urlsplit(url).hostname:  # about:blank or an empty tab
            return False
        return same_origin(url, origin)

    def _scope(self, target: Target) -> Scope:
        if target.page != "main":
            raise MissingPage(target.page)
        if not target.frame:
            return self.page
        for frame in self.page.frames:
            if frame.name == target.frame or (frame.url and target.frame in frame.url):
                return frame
        raise MissingFrame(target.frame)

    def _unique(self, scope: Scope, locator: StepLocator) -> Locator | None:
        built = build_locator(scope, locator)
        count = built.count()
        if count == 0:
            return None
        if count > 1:
            raise AmbiguousTarget(f"{locator.strategy}:{locator.value} 匹配 {count} 个元素")
        if not built.first.is_visible():
            return None
        return built.first

    def resolve(self, target: Target, timeout_s: float = 10.0) -> Resolved | None:
        scope = self._scope(target)
        for locator in target.locators:
            found = self._unique(scope, locator)
            if found is not None:
                return Resolved(found, max(1000.0, timeout_s * 1000.0))
        return None

    def act(self, action: Action, element: Resolved | None, argument: str | None) -> None:
        if not same_origin(self.page.url or "", self.origin):
            raise OutOfScope()
        if action == Action.HOTKEY and element is None:
            # 录制时焦点不在任何元素上：直接按键盘，不需要目标。
            self.page.keyboard.press(str(argument))
            return
        if element is None:
            raise MissingTarget()
        count = element.locator.count()
        if count != 1:
            raise AmbiguousTarget(f"执行前目标数量变为 {count}")
        timeout = element.timeout_ms
        if action == Action.CLICK:
            element.locator.click(timeout=timeout)
        elif action == Action.FILL:
            value = "" if argument is None else str(argument)
            if _is_select(element.locator):
                # fill() rejects <select>; choosing the option is the equivalent action.
                element.locator.select_option(value=value, timeout=timeout)
            else:
                element.locator.fill(value, timeout=timeout)
        elif action == Action.HOTKEY:
            if _is_editable(element.locator):
                element.locator.press(str(argument), timeout=timeout)
            else:
                self.page.keyboard.press(str(argument))
        else:  # wait_for never reaches act()
            raise ValueError("wait_for 不需要执行动作")


def _is_editable(locator: Locator) -> bool:
    try:
        return bool(locator.evaluate(
            "el => el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el.isContentEditable"
        ))
    except PlaywrightError:
        return False


def _is_select(locator: Locator) -> bool:
    try:
        return bool(locator.evaluate("el => el instanceof HTMLSelectElement"))
    except PlaywrightError:
        return False
