"""录制桥接：页面事件 → Python 候选事件，并在来源复核后生成候选定位器。

网页脚本可以伪造事件，因此这里只产出"候选"，全部结果都要经过人工审阅；
其他 origin 的事件直接丢弃并记录原因。
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Page

from .driver import build_locator, same_origin
from .core import Locator as StepLocator

MAX_MESSAGE_BYTES = 6000
PRIORITY = ("role", "label", "test_id", "text", "css")
BINDING_NAME = "__raEmit"
_ACTIVE: dict[int, "Recorder"] = {}


def _source_frame(source):
    """Python 同步 API 把绑定回调的第一个参数作为 dict 传入。"""
    if isinstance(source, dict):
        return source.get("frame")
    return getattr(source, "frame", None)


def _source_page(source):
    if isinstance(source, dict):
        return source.get("page")
    return getattr(source, "page", None)


def _dispatch(source, payload=None) -> None:
    """Stable binding target: forwards to whichever recorder currently owns the page."""
    try:
        recorder = _ACTIVE.get(id(_source_page(source)))
        if recorder is not None:
            recorder._on_binding(source, payload)
    except Exception:  # 单个事件失败不能影响页面
        pass


def origin_of(url: str) -> str:
    parsed = urlsplit(url or "")
    if not parsed.scheme or not parsed.hostname:
        return ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    default = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname
    netloc = f"{host}:{port}" if port != default else host
    return f"{parsed.scheme}://{netloc}"


@dataclass
class Option:
    strategy: str
    value: str
    name: str | None
    count: int
    note: str = ""

    def as_dict(self) -> dict:
        payload = {"strategy": self.strategy, "value": self.value, "count": self.count, "note": self.note}
        if self.name:
            payload["name"] = self.name
        return payload

    def locator(self) -> StepLocator:
        return StepLocator(self.strategy, self.value, self.name)


@dataclass
class Captured:
    """One operation candidate, ready for human review."""

    kind: str                       # click | fill | hotkey | merge
    at: float
    url: str
    frame: str | None
    title: str
    options: list[Option] = field(default_factory=list)
    value: str | None = None
    sensitive: bool = False
    secret_ref: str | None = None
    hotkey: str | None = None
    merged: int = 0                 # 被合并的输入事件数量

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "at": self.at,
            "url": self.url,
            "frame": self.frame,
            "title": self.title,
            "options": [option.as_dict() for option in self.options],
            "value": self.value,
            "sensitive": self.sensitive,
            "secret_ref": self.secret_ref,
            "hotkey": self.hotkey,
            "merged": self.merged,
        }


ROLE_WORDS = {
    "button": "按钮", "link": "链接", "textbox": "输入框", "searchbox": "搜索框",
    "combobox": "下拉框", "listbox": "列表框", "checkbox": "复选框", "radio": "单选框",
    "spinbutton": "数字框", "slider": "滑块", "heading": "标题", "img": "图片",
}


def describe_target(descriptor: dict) -> str:
    role = (descriptor or {}).get("role") or (descriptor or {}).get("tag") or "元素"
    word = ROLE_WORDS.get(role, "元素")
    if (descriptor or {}).get("type") == "password":
        word = "密码框"
    name = (descriptor or {}).get("name") or (descriptor or {}).get("label") or (descriptor or {}).get("text") or ""
    return f"{word}「{name}」" if name else word


class Recorder:
    """Installs the page bridge and validates every event on the Python side."""

    def __init__(self, page: Page, origin: str, script_path: Path, on_captured: Callable[[Captured], None]) -> None:
        self.page = page
        self.origin = origin
        self.script = Path(script_path).read_text(encoding="utf-8")
        self.on_captured = on_captured
        self.dropped: list[dict] = []
        self.pending: "queue.Queue[dict]" = queue.Queue()
        self.last_url = ""
        self.attached = False

    def attach(self) -> None:
        if self.attached:
            return
        _ACTIVE[id(self.page)] = self
        self.page.add_init_script(self.script)
        try:
            # 绑定名在页面内唯一：第二次录制复用分发函数，而不是重复注册。
            self.page.expose_binding(BINDING_NAME, _dispatch)
        except PlaywrightError as exc:
            if "already registered" not in str(exc):
                raise
        self.page.on("framenavigated", self._on_navigated)
        self.last_url = self.page.url
        self.attached = True

    def detach(self) -> None:
        if not self.attached:
            return
        if _ACTIVE.get(id(self.page)) is self:
            _ACTIVE.pop(id(self.page), None)
        # 注入脚本留在页面里（自带 __raInstalled 去重）：没有活动录制器时分发函数会忽略事件。
        try:
            self.page.remove_listener("framenavigated", self._on_navigated)
        except PlaywrightError:
            pass
        self.attached = False

    # -- bridge callbacks -------------------------------------------------
    def _on_binding(self, source, payload: dict | None = None) -> None:
        """Runs on the Playwright event thread: only cheap checks, then queue."""
        if not self.attached or not isinstance(payload, dict):
            return
        frame = _source_frame(source)
        if frame is None:
            return
        url = getattr(frame, "url", "") or ""
        if len(str(payload)) > MAX_MESSAGE_BYTES:
            self._drop("oversized", "事件过大", url)
            return
        self.pending.put({"payload": payload, "url": url, "frame_name": getattr(frame, "name", "") or "",
                          "is_main": frame is self.page.main_frame})

    def _on_navigated(self, frame: Frame) -> None:
        if frame is not self.page.main_frame:
            return
        url = frame.url or ""
        if not url or url.startswith("about:"):
            return
        if not same_origin(url, self.origin) and origin_of(url):
            self._drop("origin", "导航离开允许来源", url)
        self.last_url = url

    def _drop(self, code: str, reason: str, url: str) -> None:
        self.dropped.append({"code": code, "reason": reason, "url": url, "at": time.time()})

    # -- drain on the browser thread -------------------------------------
    def drain(self) -> list[Captured]:
        """Turn queued page events into candidates with live locator counts."""
        produced: list[Captured] = []
        while True:
            try:
                item = self.pending.get_nowait()
            except queue.Empty:
                break
            captured = self._resolve_event(item)
            if captured is not None:
                produced.append(captured)
        for captured in produced:
            self.on_captured(captured)
        return produced

    def _resolve_event(self, item: dict) -> Captured | None:
        payload = item["payload"]
        url = item["url"] or self.last_url
        if not same_origin(url, self.origin):
            self._drop("origin", "来源不符", url)
            return None
        kind = payload.get("kind")
        if kind == "hotkey":
            hotkey = str(payload.get("hotkey") or "")
            if not hotkey:
                return None
            descriptor = payload.get("element") or {}
            frame = self._frame_for(item)
            options = self._options(descriptor, frame) if descriptor else []
            return Captured("hotkey", float(payload.get("at") or time.time() * 1000) / 1000.0, url,
                            None if item["is_main"] else (item["frame_name"] or url),
                            describe_target(descriptor) if descriptor else "键盘组合",
                            options, hotkey=hotkey)
        descriptor = payload.get("element") or {}
        if not descriptor.get("tag"):
            return None
        frame = self._frame_for(item)
        options = self._options(descriptor, frame)
        if not options:
            self._drop("unlocateable", "无法生成任何候选定位器", url)
            return None
        sensitive = bool(descriptor.get("sensitive"))
        at = float(payload.get("at") or time.time() * 1000) / 1000.0
        value = None if sensitive else descriptor.get("currentValue")
        return Captured(
            "fill" if kind == "input" else "click", at, url,
            None if item["is_main"] else (item["frame_name"] or url),
            describe_target(descriptor), options,
            value=value if kind == "input" else None,
            sensitive=sensitive,
        )

    def _frame_for(self, item: dict) -> Frame:
        if item["is_main"]:
            return self.page.main_frame
        name = item["frame_name"]
        for frame in self.page.frames:
            if frame is self.page.main_frame:
                continue
            if (name and frame.name == name) or same_origin(frame.url or "", item["url"] or self.origin):
                return frame
        return self.page.main_frame

    def _options(self, descriptor: dict, frame: Frame) -> list[Option]:
        """Candidate locators, most reliable first, each with a live match count."""
        candidates: list[StepLocator] = []
        role, name = descriptor.get("role"), descriptor.get("name")
        if role and name:
            candidates.append(StepLocator("role", role, name))
        if descriptor.get("label"):
            candidates.append(StepLocator("label", descriptor["label"]))
        if descriptor.get("testId"):
            candidates.append(StepLocator("test_id", descriptor["testId"]))
        if descriptor.get("text") and descriptor.get("tag") not in {"input", "textarea", "select"}:
            candidates.append(StepLocator("text", descriptor["text"]))
        if descriptor.get("css"):
            candidates.append(StepLocator("css", descriptor["css"]))
        options: list[Option] = []
        for locator in candidates:
            try:
                count = build_locator(frame, locator).count()
            except PlaywrightError:
                count = -1
            if count < 0:
                continue
            options.append(Option(locator.strategy, locator.value, locator.name, count))
        # 高优先级定位器只有在唯一时才排前面；多匹配的仍保留供人工修复。
        options.sort(key=lambda item: (item.count != 1, PRIORITY.index(item.strategy) if item.strategy in PRIORITY else 9))
        return options
