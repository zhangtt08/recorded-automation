"""候选事件归并为可审阅的工作流草稿，并把草稿落为 Workflow v1 定义。

连续输入合并成一次 fill；同一目标上的重复点击去重；敏感输入只保留 secret_ref。
"""

from __future__ import annotations

import re
import time
from typing import Callable, Iterable

from .recorder import ROLE_WORDS, Captured, Option

MERGE_WINDOW_S = 1.2
ACTION_OF = {"click": "click", "fill": "fill", "hotkey": "hotkey", "merge": "fill"}


def step_key(captured: Captured) -> str:
    """Identity of the element a candidate points at, used for merging."""
    for option in captured.options:
        if option.strategy != "css":
            return f"{option.strategy}:{option.value}:{option.name or ''}"
    for option in captured.options:
        return f"css:{option.value}:"
    return captured.title


def status_of(option: dict | None) -> str:
    if option is None:
        return "missing"
    count = int(option.get("count", -1) or 0)
    if count == 1:
        return "unique"
    if count > 1:
        return "ambiguous"
    if count == 0:
        return "missing"
    return "unchecked"


class Draft:
    """Recording draft: ordered steps plus the origin/start_url contract."""

    def __init__(self, origin: str, start_url: str, default_timeout: float = 10.0) -> None:
        self.origin = origin
        self.start_url = start_url or origin
        self.default_timeout = max(1.0, float(default_timeout or 10.0))
        self.steps: list[dict] = []
        self.started_at = time.time()
        self.counter = 0

    # -- capture pipeline ------------------------------------------------
    def add(self, captured: Captured) -> None:
        key = step_key(captured)
        last = self.steps[-1] if self.steps else None
        if captured.kind == "fill" and last and last["key"] == key and ACTION_OF[last["kind"]] == "fill":
            last["value"] = captured.value
            last["sensitive"] = last["sensitive"] or captured.sensitive
            last["merged"] = int(last.get("merged", 0)) + 1
            last["kind"] = "merge"
            last["options"] = [option.as_dict() for option in captured.options]
            last["selected"] = _best_index(last["options"])
            last["status"] = status_of(_selected_option(last["options"], last["selected"]))
            last["updated_at"] = captured.at
            return
        if captured.kind == "click" and last and last["key"] == key and ACTION_OF[last["kind"]] == "click":
            if captured.at - float(last["updated_at"]) <= MERGE_WINDOW_S:
                last["updated_at"] = captured.at
                return
        self.counter += 1
        options = [option.as_dict() for option in captured.options]
        selected = _best_index(options)
        self.steps.append({
            "id": f"s{self.counter}",
            "key": key,
            "kind": captured.kind,
            "title": captured.title,
            "url": captured.url,
            "frame": captured.frame,
            "options": options,
            "selected": selected,
            "status": status_of(_selected_option(options, selected)),
            "value": captured.value if captured.kind == "fill" else None,
            "sensitive": bool(captured.sensitive),
            "secret_ref": None,
            "hotkey": captured.hotkey,
            "expected": None,
            "timeout_s": self.default_timeout,
            "merged": 0,
            "updated_at": captured.at,
        })
        self._assign_secret_refs()

    def append_manual(self, action: str = "wait_for", title: str = "等待页面出现目标") -> dict:
        """A step the user adds by hand; its locator is filled in during review."""
        self.counter += 1
        step = {
            "id": f"s{self.counter}", "key": f"manual:{self.counter}", "kind": action, "title": title,
            "url": self.start_url, "frame": None, "options": [], "selected": 0, "status": "missing",
            "value": None, "sensitive": False, "secret_ref": None, "hotkey": None, "expected": None,
            "timeout_s": self.default_timeout, "merged": 0, "updated_at": time.time(),
        }
        self.steps.append(step)
        return step

    def refresh(self, evaluate: Callable[[dict], list[dict]]) -> dict:
        """Recount every candidate on the live page; the browser session owns it."""
        changed = 0
        for step in self.steps:
            options = evaluate(step)
            if not options:
                continue
            step["options"] = options
            if step["selected"] >= len(options):
                step["selected"] = 0
            step["status"] = status_of(_selected_option(options, step["selected"]))
            changed += 1
        return {"refreshed": changed, "steps": len(self.steps)}

    # -- UI editing ------------------------------------------------------
    def edit(self, step_id: str, patch: dict) -> dict:
        step = self._find(step_id)
        for field in ("value", "hotkey", "timeout_s", "selected"):
            if field in patch:
                step[field] = patch[field]
        if "sensitive" in patch:
            step["sensitive"] = bool(patch["sensitive"])
            if step["sensitive"]:
                step["value"] = None
            self._assign_secret_refs()
        if "secret_ref" in patch and step.get("sensitive"):
            ref = str(patch["secret_ref"]).strip()
            if ref:
                step["secret_ref"] = ref
        if "expected" in patch:
            step["expected"] = patch["expected"] or None
        if "options" in patch:
            options = [normalize_option(item) for item in patch["options"] if isinstance(item, dict)]
            if options:
                step["options"] = options
                step["selected"] = min(int(step.get("selected", 0)), len(options) - 1)
                step["status"] = status_of(_selected_option(options, step["selected"]))
        return step

    def move(self, step_id: str, offset: int) -> None:
        index = self.ids().index(step_id)
        target = max(0, min(len(self.steps) - 1, index + offset))
        self.steps.insert(target, self.steps.pop(index))
        for position, step in enumerate(self.steps, start=1):
            step["id"] = f"s{position}"

    def remove(self, step_id: str) -> None:
        self.steps = [step for step in self.steps if step["id"] != step_id]
        for position, step in enumerate(self.steps, start=1):
            step["id"] = f"s{position}"
        for step in self.steps:
            if step.get("expected") and step["expected"].get("from_step") not in self.ids():
                step["expected"] = None

    def ids(self) -> list[str]:
        return [step["id"] for step in self.steps]

    def _find(self, step_id: str) -> dict:
        for step in self.steps:
            if step["id"] == step_id:
                return step
        raise KeyError(step_id)

    def _assign_secret_refs(self) -> None:
        host = re.sub(r"[^a-z0-9]+", "_", _host_of(self.origin)).strip("_").lower() or "site"
        index = 0
        for step in self.steps:
            if not step.get("sensitive"):
                continue
            index += 1
            if step.get("secret_ref"):
                continue
            field = re.sub(r"[^\w\u4e00-\u9fff]+", "_", _field_of(step.get("title", ""))).strip("_").lower()
            step["secret_ref"] = f"{host}_{field or 'secret'}_{index}"

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "origin": self.origin,
            "start_url": self.start_url,
            "started_at": self.started_at,
            "steps": [dict(step) for step in self.steps],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Draft":
        draft = cls(payload["origin"], payload.get("start_url", ""))
        draft.started_at = float(payload.get("started_at", time.time()))
        draft.steps = [dict(step) for step in payload.get("steps", [])]
        draft.counter = len(draft.steps)
        return draft

    @classmethod
    def from_workflow(cls, payload: dict) -> "Draft":
        """Load a saved Workflow v1 back into the review editor.

        Locators from a file have no live match count, so they start as 未校验
        and the user can recount them with 重新校验唯一性.
        """
        draft = cls(payload["origin"], payload.get("start_url", ""))
        for position, step in enumerate(payload.get("steps") or [], start=1):
            target = step.get("target") or {}
            options = [{"strategy": locator["strategy"], "value": locator["value"],
                        "name": locator.get("name") or None, "count": -1, "note": ""}
                       for locator in target.get("locators") or []]
            kind = str(step.get("action") or "click")
            draft.steps.append({
                "id": str(step.get("id") or f"s{position}"),
                "key": f"loaded:{step.get('id') or position}",
                "kind": kind,
                "title": step_label(step),
                "url": payload.get("start_url", ""),
                "frame": target.get("frame"),
                "options": options,
                "selected": 0,
                "status": status_of(options[0]) if options else ("unchecked" if kind == "hotkey" else "missing"),
                "value": step.get("text"),
                "sensitive": bool(step.get("secret_ref")),
                "secret_ref": step.get("secret_ref"),
                "hotkey": step.get("hotkey"),
                "expected": {"locators": (step.get("expected") or {}).get("locators") or []}
                            if step.get("expected") else None,
                "timeout_s": float(step.get("timeout_s", 10.0)),
                "merged": 0,
                "updated_at": time.time(),
            })
        draft.counter = len(draft.steps)
        return draft

    def build(self, workflow_id: str = "", name: str = "") -> dict:
        """Build a Workflow v1 payload; every problem is returned as a message."""
        problems: list[str] = []
        if not self.steps:
            problems.append("工作流至少需要一个步骤")
        payload_steps = []
        used_ids: set[str] = set()
        for step in self.steps:
            action = ACTION_OF.get(step["kind"], step["kind"])
            target = build_target(step)
            if target is None and action != "hotkey":
                problems.append(f"步骤 {step['id']} 的定位器不唯一或为空，需人工修复")
                continue
            item: dict = {"id": step["id"], "action": action}
            if target is not None:
                item["target"] = target
            if step["id"] in used_ids:
                problems.append(f"步骤 ID 重复：{step['id']}")
            used_ids.add(step["id"])
            if action == "fill":
                if step.get("sensitive"):
                    if not step.get("secret_ref"):
                        problems.append(f"步骤 {step['id']} 缺少 secret_ref")
                    item["secret_ref"] = step["secret_ref"]
                else:
                    item["text"] = str(step.get("value") or "")
            elif action == "hotkey":
                if not step.get("hotkey"):
                    problems.append(f"步骤 {step['id']} 缺少键盘组合")
                    continue
                item["hotkey"] = step.get("hotkey")
            expected = build_expected(step, self.steps)
            if expected is not None:
                item["expected"] = expected
            item["timeout_s"] = float(step.get("timeout_s", 10.0))
            payload_steps.append(item)
        workflow = {
            "schema_version": 1,
            "id": workflow_id or slug(self.start_url or self.origin) or "recorded_workflow",
            "name": name or "",
            "origin": self.origin,
            "start_url": self.start_url,
            "steps": payload_steps,
        }
        if not payload_steps and "工作流至少需要一个步骤" not in problems:
            problems.append("没有可保存的步骤")
        return {"workflow": workflow, "problems": problems}


def step_label(step: dict) -> str:
    """Human-readable step title for review, timeline and history."""
    verbs = {"click": "点击", "fill": "填写", "hotkey": "按键", "wait_for": "等待", "merge": "填写"}
    action = step.get("action") or step.get("kind") or ""
    verb = verbs.get(action, action)
    if action == "hotkey":
        return f"按下 {step.get('hotkey') or ''}".strip()
    target = step.get("target") or {}
    locators = target.get("locators") or step.get("options") or []
    first = locators[0] if locators else {}
    if first.get("strategy") == "role":
        word = ROLE_WORDS.get(first.get("value"), "元素")
        name = first.get("name") or ""
        return f"{verb} {word}「{name}」" if name else f"{verb} {word}"
    if first.get("strategy") == "label":
        return f"{verb} 输入框「{first.get('value')}」"
    if first.get("strategy") == "text":
        return f"{verb} 文本「{first.get('value')}」"
    if first.get("strategy") == "test_id":
        return f"{verb} 测试标识「{first.get('value')}」"
    if first.get("strategy") == "css":
        return f"{verb} 选择器 {first.get('value')}"
    title = step.get("title") or ""
    return f"{verb} {title}".strip()


def normalize_option(item: dict) -> dict:
    strategy = str(item.get("strategy") or "css")
    if strategy not in {"role", "label", "test_id", "text", "css"}:
        strategy = "css"
    option = {"strategy": strategy, "value": str(item.get("value") or ""), "count": int(item.get("count", -1)),
              "note": str(item.get("note") or "")}
    name = item.get("name")
    if strategy == "role":
        option["name"] = str(name or "")
    elif name:
        option["name"] = str(name)
    return option


def _host_of(url: str) -> str:
    match = re.match(r"^[a-z]+://([^/:]+)", url or "")
    return match.group(1) if match else (url or "")


def _field_of(title: str) -> str:
    """密码框「密码」 -> 密码：引用名用字段可读名，不用 URL 主机重复拼接。"""
    match = re.search(r"「([^」]+)」", title or "")
    return match.group(1) if match else (title or "")


def slug(value: str) -> str:
    host = _host_of(value) or value
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", host).strip("_")
    return cleaned[:40].lower()


def _best_index(options: Iterable[dict]) -> int:
    for position, option in enumerate(options):
        if option.get("count") == 1:
            return position
    return 0


def _selected_option(options: list[dict], index: int) -> dict | None:
    if not options:
        return None
    return options[max(0, min(index, len(options) - 1))]


def build_target(step: dict) -> dict | None:
    """Selected locator first, then any other locator that is already unique."""
    options = step.get("options") or []
    selected = _selected_option(options, step.get("selected", 0))
    if selected is None:
        return None
    ordered: list[dict] = [selected]
    for option in options:
        if option is selected or option.get("count") != 1:
            continue
        if option in ordered:
            continue
        ordered.append(option)
    locators = []
    for option in ordered:
        if option.get("count") not in (1, -1):
            continue          # 多匹配的定位器不进入工作流，避免误点别处
        payload = {"strategy": option["strategy"], "value": option["value"]}
        if option["strategy"] == "role":
            if not option.get("name"):
                return None   # role 定位器必须带可访问名称
            payload["name"] = option["name"]
        elif option.get("name"):
            continue
        if not payload["value"]:
            return None
        locators.append(payload)
    if not locators:
        return None
    target = {"page": "main", "locators": locators}
    if step.get("frame"):
        target["frame"] = step["frame"]
    return target


def build_expected(step: dict, steps: list[dict]) -> dict | None:
    expected = step.get("expected")
    if not expected:
        return None
    if isinstance(expected, dict) and expected.get("from_step"):
        for candidate in steps:
            if candidate["id"] != expected["from_step"]:
                continue
            return build_target(candidate)
        return None
    if isinstance(expected, dict) and expected.get("locators"):
        return {"page": "main", "locators": expected["locators"]}
    return None
