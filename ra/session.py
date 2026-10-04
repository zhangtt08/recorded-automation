"""会话协调器：受控浏览器、录制与回放互斥、运行状态与取消。

所有 Playwright 调用都在同一个工作线程上执行；UI 线程只提交任务并读取内存状态。
"""

from __future__ import annotations

import base64
import re
import shutil
import threading
import time
from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from .core import (Journal, Locator as StepLocator, RunEvent, RunResult, Runner, friendly_reason,
                   step_to_dict, workflow_from_dict)
from .driver import PlaywrightDriver, build_locator
from .engine import launch_persistent
from .journal import FileJournal
from .normalizer import Draft, step_label
from .paths import browsers_path_env
from .recorder import Recorder


class BusyError(RuntimeError):
    """录制与回放互斥，或浏览器正忙。"""


def valid_start_url(url: str) -> str:
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("请输入要录制的页面地址")
    if any(character.isspace() for character in candidate):
        raise ValueError("地址不能包含空格")
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只支持 http/https 地址，例如 https://site.test/page")
    if not re.fullmatch(r"[A-Za-z0-9.\-]+", parsed.hostname):
        raise ValueError("地址的主机名不合法")
    return candidate


def origin_of(url: str) -> str:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    default = 443 if parsed.scheme == "https" else 80
    netloc = f"{parsed.hostname}:{port}" if port != default else parsed.hostname
    return f"{parsed.scheme}://{netloc}"


class _Worker:
    """Single thread that owns the Playwright connection."""

    def __init__(self) -> None:
        self._queue: list[tuple] = []
        self._condition = threading.Condition()
        self._thread = threading.Thread(target=self._loop, name="ra-browser", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, fn: Callable[[], object]) -> Future:
        future: Future = Future()
        with self._condition:
            self._queue.append((fn, future))
            self._condition.notify()
        return future

    def fire(self, fn: Callable[[], object]) -> None:
        with self._condition:
            self._queue.append((fn, None))
            self._condition.notify()

    def shutdown(self) -> None:
        with self._condition:
            self._queue.append((None, None))
            self._condition.notify()

    def _loop(self) -> None:
        while True:
            with self._condition:
                while not self._queue:
                    self._condition.wait()
                fn, future = self._queue.pop(0)
            if fn is None:
                return
            if future is None:
                try:
                    fn()
                except Exception:
                    pass
                continue
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - 转发给调用方
                future.set_exception(exc)


class TeeJournal:
    """Durable journal plus live notifications for the UI."""

    def __init__(self, sink: Journal, on_event: Callable[[dict], None] | None = None) -> None:
        self.sink = sink
        self.on_event = on_event

    def append(self, event: RunEvent) -> None:
        self.sink.append(event)
        if self.on_event is not None:
            try:
                self.on_event(asdict(event))
            except Exception:
                pass


class Session:
    def __init__(self, *, journal: FileJournal, secrets, settings, script_path: Path,
                 profile_dir: Path, on_event: Callable[[str, dict], None] | None = None) -> None:
        self.journal = journal
        self.secrets = secrets
        self.settings = settings
        self.script_path = Path(script_path)
        self.profile_dir = Path(profile_dir)
        self.notify = on_event or (lambda kind, payload: None)
        self._worker = _Worker()
        self._worker.start()
        self.state = "idle"                       # idle | recording | review | running
        self.draft: Draft | None = None
        self.recorder: Recorder | None = None
        self._pw = None
        self._context = None
        self._page = None
        self._stop = threading.Event()
        self._drain_stop = threading.Event()
        self._drain_thread: threading.Thread | None = None
        self._run_future: Future | None = None
        self.runs: dict[str, dict] = {}
        self.engine = ""
        self.last_error = ""
        # 可诊断日志（真实原因，脱敏后落盘）；由 main.build 接上，测试里保持 None 也能跑。
        self.diagnostics = None

    # -- plumbing --------------------------------------------------------
    def _call(self, fn: Callable[[], object], timeout: float = 60.0):
        return self._worker.submit(fn).result(timeout=timeout)

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "busy": self.state in {"recording", "running"},
            "browser_open": self._pw is not None,
            "engine": self.engine,
            "url": self.recorder.last_url if self.recorder else "",
            "draft_steps": len(self.draft.steps) if self.draft else 0,
            "dropped": len(self.recorder.dropped) if self.recorder else 0,
            "error": self.last_error,
        }

    def _ensure_browser(self):
        if self._page is not None:
            return self._page
        browsers_path_env()
        options = self.settings.all()
        self._pw = sync_playwright().start()
        try:
            self._context, self.engine = launch_persistent(
                self._pw, self.profile_dir,
                headless=bool(options.get("headless")),
                proxy=str(options.get("proxy_server") or "").strip(),
                viewport={"width": 1280, "height": 760})
        except RuntimeError as exc:
            self._pw.stop()
            self._pw = None
            self.last_error = str(exc)
            raise
        self._context.set_default_timeout(30_000)
        pages = list(self._context.pages)
        self._page = pages[0] if pages else self._context.new_page()
        self._page.set_viewport_size({"width": 1280, "height": 760})
        return self._page

    # -- navigation ------------------------------------------------------
    def open_url(self, url: str) -> dict:
        target = valid_start_url(url)
        self._call(lambda: self._open(target))
        return {"url": target, "origin": origin_of(target)}

    def _open(self, url: str) -> str:
        page = self._ensure_browser()
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        return page.url

    def page_url(self) -> str:
        try:
            return str(self._call(lambda: self._page.url if self._page else "", timeout=5))
        except Exception:
            return ""

    def bring_to_front(self) -> None:
        try:
            self._call(lambda: self._page.bring_to_front() if self._page else None, timeout=10)
        except Exception:
            pass

    def page_action(self, fn: Callable[[object], object], timeout: float = 60.0):
        """Run fn(page) on the browser thread — used by the built-in self-check."""
        def work():
            if self._page is None:
                raise BusyError("受控浏览器尚未打开")
            return fn(self._page)

        return self._call(work, timeout=timeout)

    def preview_png(self) -> str:
        """Base64 screenshot of the controlled page — the live preview panel."""
        def grab():
            if self._page is None:
                return ""
            try:
                return base64.b64encode(self._page.screenshot(type="png", timeout=3000)).decode("ascii")
            except PlaywrightError:
                return ""
        try:
            return str(self._call(grab, timeout=12))
        except Exception:
            return ""

    # -- recording -------------------------------------------------------
    def start_recording(self, url: str) -> dict:
        if self.state == "running":
            raise BusyError("回放进行中，先停止或等待当前运行结束")
        target = valid_start_url(url)
        origin = origin_of(target)
        default_timeout = float(self.settings.all().get("default_timeout_s") or 10.0)

        def work():
            page = self._ensure_browser()
            self.draft = Draft(origin, target, default_timeout)
            self.recorder = Recorder(page, origin, self.script_path, self.draft.add)
            self.recorder.attach()
            page.goto(target, wait_until="domcontentloaded", timeout=45_000)
            self.recorder.last_url = page.url
            self._drain_stop.clear()
            self._start_drain()
            return True

        self._call(work)
        self.state = "recording"
        self.notify("state", self.snapshot())
        return {"origin": origin, "url": target}

    def _start_drain(self) -> None:
        def loop():
            while not self._drain_stop.wait(0.3):
                recorder = self.recorder
                if recorder is not None:
                    self._worker.fire(recorder.drain)

        self._drain_thread = threading.Thread(target=loop, name="ra-drain", daemon=True)
        self._drain_thread.start()

    def stop_recording(self) -> dict:
        if self.state != "recording":
            raise BusyError("当前没有进行中的录制")
        self._drain_stop.set()
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=3)
            self._drain_thread = None

        def work():
            if self.recorder is not None:
                self.recorder.drain()
                self.recorder.detach()
            return True

        self._call(work)
        self.recorder = None
        self.state = "review" if (self.draft and self.draft.steps) else "idle"
        self.notify("state", self.snapshot())
        return self.draft_dict() or {"steps": []}

    def recording_snapshot(self) -> dict:
        if self.state == "recording" and self.recorder is not None:
            try:
                self._call(lambda: self.recorder.drain(), timeout=8)
            except Exception:
                pass
        steps = []
        if self.draft:
            steps = [self._public_step(step) for step in self.draft.steps]
        dropped = list(self.recorder.dropped) if self.recorder else []
        return {
            "state": self.state,
            "origin": self.draft.origin if self.draft else "",
            "url": self.recorder.last_url if self.recorder else (self.draft.start_url if self.draft else ""),
            "elapsed": round(time.time() - self.draft.started_at, 1) if self.draft else 0,
            "steps": steps,
            "dropped": dropped,
            "count": len(steps),
            "discarded": len(dropped),
        }

    def _public_step(self, step: dict) -> dict:
        return {key: value for key, value in step.items() if key != "key"}

    # -- draft editing ---------------------------------------------------
    def draft_dict(self) -> dict | None:
        if self.draft is None:
            return None
        return {"origin": self.draft.origin, "start_url": self.draft.start_url,
                "started_at": self.draft.started_at, "steps": [self._public_step(step) for step in self.draft.steps]}

    def edit_draft(self, step_id: str, patch: dict) -> dict:
        if self.draft is None:
            raise BusyError("没有可编辑的草稿")
        step = self.draft.edit(step_id, patch)
        return self._public_step(step)

    def move_draft(self, step_id: str, offset: int) -> dict:
        if self.draft is None:
            raise BusyError("没有可编辑的草稿")
        self.draft.move(step_id, int(offset))
        return {"steps": [self._public_step(step) for step in self.draft.steps]}

    def remove_draft(self, step_id: str) -> dict:
        if self.draft is None:
            raise BusyError("没有可编辑的草稿")
        self.draft.remove(step_id)
        self.state = "review" if self.draft.steps else "idle"
        return {"steps": [self._public_step(step) for step in self.draft.steps]}

    def add_wait_step(self) -> dict:
        if self.draft is None:
            raise BusyError("没有可编辑的草稿，请先录制一段操作")
        step = self.draft.append_manual()
        self.state = "review"
        return self._public_step(step)

    def load_draft(self, payload: dict) -> dict:
        """Open a saved workflow in the review editor (counts start as 未校验)."""
        self.draft = Draft.from_workflow(payload)
        self.recorder = None
        self.state = "review"
        return {"draft": self.draft_dict(),
                "workflow": {"id": payload.get("id"), "name": payload.get("name") or payload.get("id")}}

    def refresh_draft(self) -> dict:
        """Recount every candidate locator against the live page."""
        if self.draft is None:
            raise BusyError("没有可校验的草稿")
        if self._page is None:
            raise BusyError("受控浏览器已关闭，无法重新校验唯一性")

        def evaluate(step: dict) -> list[dict]:
            return self._worker.submit(lambda: self._count_options(step)).result(timeout=30)

        summary = self.draft.refresh(evaluate)
        summary["steps"] = [self._public_step(step) for step in self.draft.steps]
        summary["url"] = self.draft.start_url
        return summary

    def _count_options(self, step: dict) -> list[dict]:
        page = self._page
        if page is None:
            return []
        frame = page.main_frame
        if step.get("frame"):
            wanted = str(step["frame"])
            for candidate in page.frames:
                if candidate is page.main_frame:
                    continue
                if candidate.name == wanted or wanted in (candidate.url or ""):
                    frame = candidate
                    break
        out: list[dict] = []
        for option in step.get("options") or []:
            strategy = str(option.get("strategy") or "css")
            name = option.get("name") if strategy == "role" else None
            enriched = dict(option)
            try:
                locator = build_locator(frame, StepLocator(strategy, str(option.get("value") or ""), name or None))
                enriched["count"] = int(locator.count())
            except Exception:
                enriched["count"] = -1
            out.append(enriched)
        return out

    def build_draft(self, workflow_id: str = "", name: str = "") -> dict:
        if self.draft is None:
            raise BusyError("没有可生成的草稿")
        return self.draft.build(workflow_id, name)

    # -- replay ----------------------------------------------------------
    def missing_secrets(self, payload: dict) -> list[str]:
        """秘密库里还缺的引用名（只给名字，绝不给值）。"""
        return sorted({step.secret_ref for step in workflow_from_dict(payload).steps
                       if step.secret_ref and not self.secrets.has(step.secret_ref)})

    def start_run(self, payload: dict, run_id: str, resumed: dict | None = None) -> dict:
        if self.state in {"recording", "running"}:
            raise BusyError("录制或回放正在进行，两者互斥")
        workflow = workflow_from_dict(payload)
        missing = sorted({step.secret_ref for step in workflow.steps
                          if step.secret_ref and not self.secrets.has(step.secret_ref)})
        if missing:
            # 缺值时在打开浏览器之前就被拦下，并把缺的引用名全部说清楚。
            raise BusyError("秘密库缺少 " + "、".join(missing) + "，请先在秘密库中填写")
        self._stop.clear()
        self.state = "running"
        self.runs[run_id] = {"run_id": run_id, "workflow_id": workflow.id,
                             "workflow_name": workflow.name or workflow.id,
                             "status": "running", "step_id": "", "code": "", "error": "", "reason": "",
                             "events": [], "steps": [step_to_dict(step) for step in workflow.steps],
                             "started_at": time.time(), "ended_at": 0,
                             "resumed": dict(resumed or {})}
        self._diagnostic_launch(run_id, workflow, resumed)
        self.notify("state", self.snapshot())
        self._run_future = self._worker.submit(lambda: self._execute(run_id, payload))
        self._run_future.add_done_callback(lambda future: self._run_done(run_id, future))
        return {"run_id": run_id, "steps": len(workflow.steps)}

    def _diagnostic_launch(self, run_id: str, workflow, resumed: dict | None) -> None:
        """先落一条「这次运行开始了」：没有它就无法区分「取消了一次运行」和「压根没跑起来」。"""
        if getattr(self, "diagnostics", None) is None:
            return
        try:
            self.diagnostics.launch(run_id, workflow.id, workflow.name or workflow.id, len(workflow.steps),
                                    str((resumed or {}).get("from_step", "")))
        except Exception:
            pass

    def _execute(self, run_id: str, payload: dict) -> RunResult:
        workflow = workflow_from_dict(payload)
        journal = TeeJournal(self.journal, lambda event: self._observe(run_id, event))

        def inner():
            page = self._ensure_browser()
            page.goto(workflow.start_url, wait_until="domcontentloaded", timeout=45_000)
            driver = PlaywrightDriver(page, workflow.origin)
            runner = Runner(driver, self.secrets, journal, self._stop, poll_s=0.05)
            return runner.run(workflow, run_id)

        return inner()

    def _observe(self, run_id: str, event: dict) -> None:
        record = self.runs.get(run_id)
        if record is None:
            return
        record["events"].append({**event, "at": round(time.time(), 3)})
        record["step_id"] = event.get("step_id", record["step_id"])
        self.notify("run", {"run_id": run_id, "event": event})

    def _run_done(self, run_id: str, future: Future) -> None:
        record = self.runs.get(run_id, {})
        try:
            result = future.result()
            status, step_id, code = result.status.value, result.step_id or "", result.code
            record["reason"] = str(getattr(result, "reason", "") or "")
        except Exception as exc:  # 驱动或启动失败：没进过 Runner，所以这里自己补一条阶段事件
            status, step_id, code = "failed", "", type(exc).__name__
            message = str(exc)
            record["error"] = message
            record["reason"] = friendly_reason(code, message)
            workflow_id = str(record.get("workflow_id", ""))
            try:
                self.journal.append(RunEvent(run_id, workflow_id, "", "failed_before_action", code,
                                             record["reason"]))
            except Exception:
                pass
        self._record_diagnostic(run_id, record, status, step_id, code)
        record.update({"status": status, "step_id": step_id, "code": code, "ended_at": time.time()})
        try:
            self.journal.append(RunEvent(run_id, record.get("workflow_id", ""), step_id, status, code,
                                         str(record.get("reason", ""))))
        except Exception:
            pass
        self.state = "idle"
        self.notify("run_done", dict(record))
        self.notify("state", self.snapshot())
        if not self.settings.all().get("keep_browser_open", True):
            self.close_browser()

    def _record_diagnostic(self, run_id: str, record: dict, status: str, step_id: str, code: str) -> None:
        """失败/未确认/取消都留下可诊断的一行：第几步、在做什么、真实原因、秘密值处理了几次。"""
        if getattr(self, "diagnostics", None) is None:
            return
        try:
            steps = record.get("steps") or []
            position = next((index for index, item in enumerate(steps, start=1) if item.get("id") == step_id), 0)
            step = steps[position - 1] if position else {}
            secret_refs = sorted({str(item.get("secret_ref")) for item in steps if item.get("secret_ref")})
            # 「秘密值被取用了几次」按事件数出来：值本身一个字节都不进诊断文件。
            secret_steps = {str(item.get("id")) for item in steps if item.get("secret_ref")}
            used = sum(1 for event in record.get("events", [])
                       if event.get("phase") == "action_returned" and str(event.get("step_id")) in secret_steps)
            self.diagnostics.record(
                run_id=run_id, workflow_id=str(record.get("workflow_id", "")),
                workflow_name=str(record.get("workflow_name", "")), status=status,
                step_id=step_id, index=position, total=len(steps), action=str(step.get("action", "")),
                label=step_label(step) if step else "", code=code,
                reason=str(record.get("reason", "")), error=str(record.get("error", "")),
                events=[event.get("phase", "") for event in record.get("events", [])],
                secret_refs=secret_refs, secret_values_used=used)
        except Exception as exc:  # 诊断本身绝不能把运行结果带跑
            record["diagnostic_error"] = f"{type(exc).__name__}: {exc}"

    def run_status(self, run_id: str) -> dict:
        record = self.runs.get(run_id)
        if record is None:
            return {"run_id": run_id, "status": "unknown", "events": []}
        payload = dict(record)
        payload["duration_s"] = round((payload.get("ended_at") or time.time()) - payload.get("started_at", time.time()), 1)
        return payload

    def cancel_run(self) -> dict:
        if self.state != "running":
            return {"cancelled": False, "note": "当前没有进行中的运行"}
        self._stop.set()
        running = [row for row in self.runs.values() if row.get("status") == "running"]
        note = "将在当前动作结束后停止，不会自动重试"
        if running and not running[0].get("events"):
            note = ("这次运行还没有执行到任何一步（正在打开页面或刚起跑）：会立刻停下，"
                    "任何动作都没有发出过，可以从第一步重跑")
        return {"cancelled": True, "note": note, "run_id": running[0]["run_id"] if running else ""}

    # -- lifecycle -------------------------------------------------------
    def close_browser(self) -> None:
        def work():
            self._stop.set()
            if self._drain_thread is not None:
                self._drain_stop.set()
                self._drain_thread.join(timeout=2)
                self._drain_thread = None
            for closer in (self._context.close if self._context else None,
                           self._pw.stop if self._pw else None):
                try:
                    if closer is not None:
                        closer()
                except Exception:
                    pass
            self._context = None
            self._page = None
            self._pw = None

        try:
            self._call(work, timeout=20)
        except Exception:
            pass
        self.recorder = None
        self.state = "idle"

    def reset_profile(self) -> dict:
        self.close_browser()
        try:
            if self.profile_dir.exists():
                shutil.rmtree(self.profile_dir, ignore_errors=True)
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            return {"cleared": True}
        except OSError as exc:
            return {"cleared": False, "note": str(exc)}

    def shutdown(self) -> None:
        self.close_browser()
        self._worker.shutdown()
