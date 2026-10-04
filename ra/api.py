"""UI 门面：把会话协调器、存储与秘密库暴露给一体化窗口内的前端。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from . import __version__
from .core import friendly_reason
from .journal import TERMINAL as TERMINAL_PHASES
from .normalizer import step_label
from .paths import APP_TITLE, data_root, resource_root, version_stamp
from .secrets import FileSecretStore
from .session import BusyError, Session
from .store import Settings, WorkflowStore

PHASE_ORDER = ["step_started", "action_started", "action_returned", "verified", "executed_unverified",
               "failed_before_action", "uncertain", "cancelled"]
TERMINAL_PHASES = {"completed", "completed_unverified", "failed", "uncertain", "cancelled"}
STEP_STATE = {
    "verified": ("ok", "已验证"),
    "executed_unverified": ("info", "未验证 · 无完成条件"),
    "uncertain": ("warn", "未确认"),
    "failed_before_action": ("bad", "失败"),
    "cancelled": ("off", "已取消"),
    "step_started": ("off", "未执行"),
    "running": ("rec", "执行中"),
}
RUN_STATE = {
    "completed": ("ok", "完成"),
    "completed_unverified": ("info", "完成 · 未验证"),
    "failed": ("bad", "失败"),
    "uncertain": ("warn", "未确认"),
    "cancelled": ("off", "已取消"),
    "running": ("rec", "运行中"),
    "unknown": ("off", "无记录"),
}


def _run_id() -> str:
    """MMDD + HHMMSS + 毫秒：日期进了 id，界面上才答得出「这条历史运行是哪天跑的」。"""
    return time.strftime("%m%d") + (time.strftime("%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}")[:12]


class Api:
    def __init__(self, session: Session, store: WorkflowStore, secrets: FileSecretStore,
                 settings: Settings, journal) -> None:
        self.session = session
        self.store = store
        self.secrets = secrets
        self.settings = settings
        self.journal = journal
        self.window = None
        self.agent_server = None
        self.root = data_root()

    # -- infrastructure --------------------------------------------------
    def attach(self, window) -> None:
        self.window = window

    def version_info(self) -> dict:
        """给界面标题条用的一行版本信息（vX · 代码/资源新鲜度 · 源码或封装）。"""
        stamp = version_stamp()
        kind = "封装版" if stamp["frozen"] else "源码"
        note = "资源与代码同期"
        if stamp["ui_newer_than_source"]:
            note = "资源比代码新（重启或重新打包才会用上）"
        elif stamp["frozen"]:
            note = "资源打包于 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp["ui_mtime"]))
        return {"version": stamp["version"], "kind": kind, "note": note,
                "stamp": f"v{stamp['version']} · {kind} · {note}"}

    def push(self, kind: str, payload: dict) -> None:
        """Push backend state into the in-page UI (no separate window or page)."""
        if self.window is None:
            return
        script = "window.__raPush && window.__raPush({kind}, {data});".format(
            kind=json.dumps(kind), data=json.dumps(payload, ensure_ascii=False, default=str))
        try:
            self.window.evaluate_js(script)
        except Exception:
            pass

    def _guard(self, fn: Callable[[], object]) -> dict:
        try:
            result = fn()
            return result if isinstance(result, dict) else {"ok": True, "value": result}
        except BusyError as exc:
            return {"error": str(exc), "busy": True}
        except (ValueError, KeyError) as exc:
            return {"error": str(exc) or type(exc).__name__}
        except Exception as exc:  # noqa: BLE001 - UI 需要可读的失败信息
            message = str(exc).splitlines()[0] if str(exc) else ""
            return {"error": f"{type(exc).__name__}: {message}" if message else type(exc).__name__}

    # -- overview --------------------------------------------------------
    def app_info(self) -> dict:
        stamp = version_stamp()
        return self._guard(lambda: {
            "title": APP_TITLE,
            "version": stamp["version"],
            "version_kind": "封装版" if stamp["frozen"] else "源码",
            "version_stamp": stamp,
            "state": self.session.snapshot(),
            "data_dir": str(self.root),
            "resources": str(resource_root()),
            "workflows": self.store.list(),
            "runs": self._runs_with_names(),
            "secrets": self.secrets.preview(),
            "settings": self.settings.all(),
            "bundled": bool(getattr(sys, "_MEIPASS", None)),
            "frozen": bool(getattr(sys, "frozen", False)),
            "agent": (self.agent_server.info() if self.agent_server is not None
                      else {"ok": False, "tools": 0, "url": "", "error": "未启用"}),
        })

    def state(self) -> dict:
        return self._guard(self.session.snapshot)

    def open_url(self, url: str) -> dict:
        return self._guard(lambda: self.session.open_url(url))

    def preview(self) -> dict:
        return self._guard(lambda: {"png": self.session.preview_png(), "at": round(time.time(), 2)})

    def bring_to_front(self) -> dict:
        return self._guard(lambda: self.session.bring_to_front() or {"ok": True})

    # -- recording -------------------------------------------------------
    def start_recording(self, url: str) -> dict:
        return self._guard(lambda: self.session.start_recording(url))

    def stop_recording(self) -> dict:
        return self._guard(self.session.stop_recording)

    def recording(self) -> dict:
        return self._guard(self.session.recording_snapshot)

    # -- draft review ----------------------------------------------------
    def draft(self) -> dict:
        return self._guard(lambda: {"draft": self.session.draft_dict()})

    def edit_step(self, step_id: str, patch: dict) -> dict:
        return self._guard(lambda: self.session.edit_draft(step_id, patch or {}))

    def move_step(self, step_id: str, offset: int) -> dict:
        return self._guard(lambda: self.session.move_draft(step_id, offset))

    def remove_step(self, step_id: str) -> dict:
        return self._guard(lambda: self.session.remove_draft(step_id))

    def add_wait_step(self) -> dict:
        return self._guard(self.session.add_wait_step)

    def open_in_editor(self, workflow_id: str) -> dict:
        return self._guard(lambda: self.session.load_draft(self.store.load(workflow_id)))

    def refresh_draft(self) -> dict:
        return self._guard(self.session.refresh_draft)

    def preview_workflow(self, workflow_id: str = "", name: str = "") -> dict:
        def work():
            built = self.session.build_draft(workflow_id, name)
            report = self.store.problems(built["workflow"])
            decorated = [dict(step, label=step_label(step)) for step in built["workflow"]["steps"]]
            workflow = dict(built["workflow"], steps=decorated)
            return {"workflow": workflow, "problems": built["problems"], "check": report,
                    "warnings": report["warnings"], "blocking": report["blocking"]}
        return self._guard(work)

    # -- workflow store --------------------------------------------------
    def validate_workflow(self, payload: dict) -> dict:
        return self._guard(lambda: self.store.problems(payload or {}))

    def save_workflow(self, payload: dict) -> dict:
        def work():
            report = self.store.save(payload)
            missing = sorted({step.get("secret_ref") for step in payload.get("steps", [])
                              if step.get("secret_ref") and not self.secrets.has(step["secret_ref"])})
            return {"ok": True, "id": payload["id"], "check": report,
                    "needs_secret": [item for item in missing if item]}
        return self._guard(work)

    def list_workflows(self) -> dict:
        return self._guard(lambda: {"workflows": self.store.list()})

    def get_workflow(self, workflow_id: str) -> dict:
        def work():
            payload = self.store.load(workflow_id)
            steps = [dict(step, label=step_label(step)) for step in payload.get("steps", [])]
            return {"workflow": dict(payload, steps=steps)}
        return self._guard(work)

    def delete_workflow(self, workflow_id: str) -> dict:
        return self._guard(lambda: self.store.delete(workflow_id) or {"ok": True})

    def export_workflow(self, workflow_id: str) -> dict:
        def work():
            payload = self.store.load(workflow_id)
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            return {"path": self._save_dialog(f"{workflow_id}.workflow.json", text)}
        return self._guard(work)

    def _save_dialog(self, filename: str, text: str) -> str:
        """导出写进本机 exports 目录（窗口内不弹系统保存框）。"""
        target = self.root / "exports" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return str(target)

    # -- replay ----------------------------------------------------------
    def _blocked_by_secrets(self, payload: dict) -> dict | None:
        """缺秘密值时在打开浏览器之前返回结构化原因（只有引用名）。"""
        missing = self.session.missing_secrets(payload)
        if not missing:
            return None
        return {"error": "秘密库缺少 " + "、".join(missing) + "，请先在秘密库中填写",
                "missing_secret": missing}

    def run_workflow(self, workflow_id: str, from_step: str = "") -> dict:
        """运行工作流；from_step 指定「从这一步续跑」（人工点下来的，不是自动重试）。"""
        def work():
            payload = self.store.load(workflow_id)
            steps = payload.get("steps") or []
            resumed: dict = {}
            if from_step:
                index = next((position for position, step in enumerate(steps)
                              if step.get("id") == from_step), -1)
                if index < 0:
                    raise ValueError(f"工作流里没有步骤 {from_step}")
                if index:
                    payload = dict(payload, steps=steps[index:])
                    resumed = {"from_step": from_step, "skipped": index, "total_steps": len(steps)}
            blocked = self._blocked_by_secrets(payload)
            if blocked:
                return blocked
            run_id = _run_id()
            started = self.session.start_run(payload, run_id, resumed=resumed)
            return {"run_id": started["run_id"], "steps": started["steps"],
                    "steps_total": len(steps), "resumed_from": resumed.get("from_step", ""),
                    "name": payload.get("name") or workflow_id, **{k: v for k, v in resumed.items()}}
        return self._guard(work)

    def run_draft_now(self) -> dict:
        """运行草稿（校验通过后）——用于审阅页的即时试跑。"""
        def work():
            built = self.session.build_draft()
            if built["problems"]:
                raise ValueError("；".join(built["problems"][:3]))
            payload = built["workflow"]
            blocked = self._blocked_by_secrets(payload)
            if blocked:
                return blocked
            run_id = _run_id()
            started = self.session.start_run(payload, run_id)
            return {"run_id": started["run_id"], "steps": started["steps"],
                    "steps_total": len(payload.get("steps") or []), "resumed_from": "",
                    "name": payload.get("name") or payload["id"], "unsaved_draft": True}
        return self._guard(work)

    def run_status(self, run_id: str) -> dict:
        def work():
            status = self.session.run_status(run_id)
            return {"run": status, "detail": self._detail(run_id, status)}
        return self._guard(work)

    def cancel_run(self) -> dict:
        return self._guard(self.session.cancel_run)

    def list_runs(self) -> dict:
        return self._guard(lambda: {"runs": self._runs_with_names()})

    def run_detail(self, run_id: str) -> dict:
        def work():
            return {"detail": self._detail(run_id, self.session.run_status(run_id))}
        return self._guard(work)

    def export_run(self, run_id: str) -> dict:
        def work():
            events = self.journal.read_run(run_id)
            text = json.dumps({"run_id": run_id, "events": events}, ensure_ascii=False, indent=2)
            return {"path": self._save_dialog(f"run_{run_id}.json", text)}
        return self._guard(work)

    def _runs_with_names(self) -> list[dict]:
        names = {}
        for row in self.store.list():
            names[row["id"]] = row.get("name") or row["id"]
        runs = list(self.journal.list_runs(limit=80))
        for row in runs:
            row["name"] = names.get(row["workflow_id"], row["workflow_id"])
            row["missing_workflow"] = row["workflow_id"] not in names
        return runs

    def _detail(self, run_id: str, live: dict) -> dict:
        events = live.get("events") or self.journal.read_run(run_id)
        workflow_id = live.get("workflow_id", "")
        if not workflow_id and events:
            workflow_id = events[0].get("workflow_id", "")
        found = True
        try:
            payload = self.store.load(workflow_id)
        except Exception:
            payload = {"steps": []}
            found = False
        by_step: dict[str, list[dict]] = {}
        for event in events:
            by_step.setdefault(event.get("step_id", ""), []).append(event)
        timeline = []
        running_index = 0
        if live.get("status") == "running":
            touched = [position for position, step in enumerate(payload.get("steps", []), start=1)
                       if by_step.get(step.get("id", ""))]
            running_index = touched[-1] if touched else 0
        for position, step in enumerate(payload.get("steps", []), start=1):
            step_events = by_step.get(step.get("id", ""), [])
            phases = [item.get("phase") for item in step_events]
            state = "step_started"
            for candidate in PHASE_ORDER[::-1]:
                if candidate in phases:
                    state = candidate
                    break
            if position == running_index and state in {"step_started", "action_started"}:
                state = "running"
            times = [float(item.get("at") or 0) for item in step_events if item.get("at")]
            modifier, label = STEP_STATE.get(state, ("off", state))
            timeline.append({
                "index": position,
                "step_id": step.get("id", ""),
                "action": step.get("action", ""),
                "label": step_label(step),
                "state": modifier,
                "state_label": label,
                "phases": phases,
                "duration_s": round(max(times) - min(times), 1) if len(times) > 1 else 0.0,
                "code": next((item.get("code") for item in step_events if item.get("code")), ""),
            })
        # 已经不在内存里的历史运行：结果按日志里最后那条终态阶段来读，读不到就如实写「无记录」。
        terminal = next((item for item in reversed(events) if item.get("phase") in TERMINAL_PHASES), {})
        status = live.get("status") or "unknown"
        if status == "unknown" and terminal.get("phase"):
            status = str(terminal["phase"])
        modifier, label = RUN_STATE.get(status, ("off", status))
        stamp = [float(item.get("at") or 0) for item in events if item.get("at")]
        started = live.get("started_at") or (min(stamp, default=0))
        ended = live.get("ended_at") or (max(stamp, default=0))
        duration = round(ended - started, 1) if ended and started else 0.0
        step_id = live.get("step_id", "") or str(terminal.get("step_id", "") or "")
        resumed = dict(live.get("resumed") or {})
        steps = payload.get("steps", [])
        on_step = bool(step_id) and any(step.get("id") == step_id for step in steps)
        # 「失败」与「已取消」都停在这一步的动作之前（未确认不算），所以可以直接从该步续跑。
        resumable = status in {"failed", "cancelled"} and on_step
        # 「未确认」的动作已经发出去过一次：续跑只能由人做完人工检查后自己点，程序不替他决定。
        manual_resume = status == "uncertain" and on_step
        resume_index = next((row["index"] for row in timeline if row["step_id"] == step_id), 0)
        # 「执行了几步」按有没有阶段事件算（含被停下的那一步）；再单独给「验证通过几步」，
        # 免得界面把「跑到第 2 步就失败」显示成「跑了 1 步」，也把「跑了」说成「验证过了」。
        steps_run = len([item for item in timeline if item["phases"]])
        steps_verified = len([item for item in timeline if item["state"] == "ok"])
        reason = str(live.get("reason") or "") or next(
            (str(item.get("reason") or "") for item in reversed(events)
             if item.get("phase") in TERMINAL_PHASES and item.get("reason")), "")
        code = live.get("code", "") or str(terminal.get("code", "") or "")
        if not reason and (status in {"failed", "uncertain"} or code):
            reason = friendly_reason(code, live.get("error", ""))
        diagnostic = ""
        if status in {"failed", "uncertain", "cancelled"} and getattr(self.session, "diagnostics", None):
            diagnostic = str(self.session.diagnostics.message_for(run_id) or "")
        if not reason:
            reason = diagnostic
        # 「已经跑过几次」只在运行结束后算：运行中每 0.7 秒刷一次没必要，读日志尾部也不是免费的。
        prior = [] if status == "running" else [
            row for row in self.journal.list_runs(limit=120)
            if row.get("workflow_id") == workflow_id and row.get("status") in
            {"completed", "completed_unverified", "uncertain"}]
        return {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "workflow_name": live.get("workflow_name") or payload.get("name") or workflow_id,
            "status": status,
            "status_label": label,
            "status_class": modifier,
            "step_id": step_id,
            "code": code,
            "reason": reason,
            "diagnostic": diagnostic or "",
            "started_at": started or 0,
            "duration_s": max(duration, 0.0),
            "steps_total": len(steps),
            "steps_run": steps_run,
            "steps_verified": steps_verified,
            "workflow_found": found,
            "steps_unverified": len([item for item in timeline if item["state"] == "info"]),
            "timeline": timeline,
            "events": events,
            "uncertain": status == "uncertain",
            "resumable": resumable,
            "manual_resume": manual_resume,
            "resume_index": resume_index,
            "resumed": resumed,
            "prior_runs": len(prior),
            "prior_completed": len([row for row in prior if row.get("status") != "uncertain"]),
            "error": live.get("error", ""),
            "durable": status != "running",
            "journal_path": str(self.journal.path),
        }

    # -- secrets & settings ---------------------------------------------
    def secret_list(self) -> dict:
        def work():
            used: dict[str, list[str]] = {}
            for row in self.store.list():
                for ref in row.get("secret_refs") or []:
                    used.setdefault(ref, []).append(row.get("name") or row["id"])
            return {"secrets": self.secrets.preview(), "refs_needed": sorted(used), "used_by": used}
        return self._guard(work)

    def _refs_needed(self) -> list[str]:
        needed: set[str] = set()
        for row in self.store.list():
            needed.update(row.get("secret_refs") or [])
        return sorted(needed)

    def secret_set(self, ref: str, value: str) -> dict:
        return self._guard(lambda: self.secrets.set(str(ref), str(value)) or {"ref": ref, "stored": True})

    def secret_delete(self, ref: str) -> dict:
        return self._guard(lambda: self.secrets.delete(str(ref)) or {"ref": ref, "deleted": True})

    def settings_get(self) -> dict:
        return self._guard(lambda: {"settings": self.settings.all(), "data_dir": str(self.root),
                                    "profile_dir": str(self.root / "profile"),
                                    "engine": self.session.engine or "未启动"})

    def settings_set(self, patch: dict) -> dict:
        def work():
            cleaned = {}
            for key, value in (patch or {}).items():
                if key == "default_timeout_s":
                    cleaned[key] = max(1.0, min(120.0, float(value)))
                elif key in {"headless", "keep_browser_open", "onboarded", "agent_api"}:
                    cleaned[key] = bool(value)
                elif key == "proxy_server":
                    cleaned[key] = str(value or "").strip()
                elif key == "window_mode":
                    wanted = str(value or "").strip().lower()
                    if wanted not in {"docked", "free", "fullscreen"}:
                        raise ValueError("窗口模式只能是 docked / free / fullscreen")
                    cleaned[key] = wanted
                elif key == "window_geom":
                    cleaned[key] = dict(value or {}) if isinstance(value, dict) else {}
                elif key == "agent_port":
                    cleaned[key] = max(1024, min(65535, int(value)))
            return {"settings": self.settings.update(cleaned)}
        return self._guard(work)

    def reset_profile(self) -> dict:
        return self._guard(self.session.reset_profile)

    def close_browser(self) -> dict:
        return self._guard(lambda: self.session.close_browser() or {"ok": True})

    def reveal_data_dir(self) -> dict:
        def work():
            if sys.platform == "win32":
                os.startfile(str(self.root))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.root)])
            else:
                subprocess.Popen(["xdg-open", str(self.root)])
            return {"path": str(self.root)}
        return self._guard(work)

    def selfcheck(self) -> dict:
        if self.session.state in {"recording", "running"}:
            return {"error": "录制或回放进行中，先结束后再自检", "busy": True}

        def work():
            from .selfcheck import run
            return run(self.root / "selfcheck")
        return self._guard(work)

    def shutdown(self) -> dict:
        return self._guard(lambda: self.session.shutdown() or {"ok": True})

    # -- Agent 接口：程序本身就是那个「给 Agent 用」的后端 ------------------
    def agent_info(self) -> dict:
        def work():
            server = self.agent_server
            if server is None:
                return {"ok": False, "enabled": bool(self.settings.all().get("agent_api")),
                        "error": "Agent 接口没有运行（设置里可开关，改完重启程序生效）", "tool_docs": []}
            info = server.info()
            info["enabled"] = bool(self.settings.all().get("agent_api"))
            info["port_pref"] = int(self.settings.all().get("agent_port") or 8795)
            info["tool_docs"] = [{
                "name": tool["name"], "description": tool["description"], "risk": tool.get("risk", "read"),
                "required": list((tool.get("input_schema") or {}).get("required", [])),
                "params": sorted((tool.get("input_schema") or {}).get("properties", {})),
            } for tool in server.tools]
            base = info.get("url") or ""
            info["curl"] = (f"curl -s {base}/api/agent/tools\n"
                            f"curl -s -X POST {base}/api/agent/tool "
                            f"-H 'content-type: application/json' "
                            f"-d '{{\"tool\":\"ra.status\",\"input\":{{}}}}'")
            root = str(resource_root().parent)
            info["mcp"] = json.dumps({"mcpServers": {"recorded-automation": {
                "command": "node",
                "args": [str(Path(root) / "agent" / "mcp-server.mjs")],
                "env": {"AGENT_BASE_URL": base}}}}, ensure_ascii=False, indent=2)
            return info
        return self._guard(work)

    def agent_probe(self) -> dict:
        """真的对自己发一次 HTTP 调用：证明端点活着，而不是只报告「我以为起来了」。"""
        def work():
            server = self.agent_server
            if server is None or not server.port:
                raise ValueError("Agent 接口没有运行")
            import urllib.request

            url = f"http://127.0.0.1:{server.port}/api/agent/tool"
            request = urllib.request.Request(
                url, data=json.dumps({"tool": "ra.status", "input": {}}).encode("utf-8"),
                headers={"content-type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return {"ok": bool(payload.get("ok")), "http": response.status,
                    "version": (payload.get("data") or {}).get("version", ""),
                    "workflows": (payload.get("data") or {}).get("workflows", 0),
                    "in_process": (payload.get("data") or {}).get("serving", False),
                    "ms": payload.get("ms", 0)}
        return self._guard(work)

    def agent_restart(self, port: int = 0) -> dict:
        def work():
            from .main import start_agent_server

            chosen = int(port or self.settings.all().get("agent_port") or 8795)
            if self.agent_server is not None:
                try:
                    self.agent_server.stop()
                except Exception:  # noqa: BLE001
                    pass
            self.settings.update({"agent_port": chosen, "agent_api": True})
            self.agent_server = start_agent_server(self, self.session, chosen)
            if self.agent_server.error:
                return {"ok": False, "error": self.agent_server.error}
            return self.agent_server.info()
        return self._guard(work)

    # -- 窗口控制：标题条由界面自己画，移动/缩放/最小化/最大化都走这里 ----
    def _win(self, name: str, default: dict | None = None, *args) -> dict:
        handler = getattr(self.window, name, None)
        if handler is None:
            return default if default is not None else {"ok": False, "note": "当前平台不支持窗口控制"}
        return self._guard(lambda: handler(*args))

    def win_state(self) -> dict:
        return self._win("win_state", {"frameless": False, "titlebar_hidden": False, "fullscreen": False,
                                       "caption": True, "supported": False, "mode": "free", "strip": 0,
                                       "content": [0, 0, 0, 0], "work": [0, 0, 1280, 720]})

    def win_move(self, left: int = 0, top: int = 0) -> dict:
        return self._win("win_move", {"ok": False}, int(left), int(top))

    def win_resize(self, content=None) -> dict:
        if not content or len(list(content)) != 4:
            return {"ok": False, "error": "win_resize 需要 [x, y, w, h]"}
        return self._win("win_resize", {"ok": False}, list(content))

    def win_gesture_end(self) -> dict:
        return self._win("win_gesture_end", {"ok": False})

    def win_remeasure(self) -> dict:
        return self._win("win_remeasure", {"ok": False, "error": "当前窗口不支持重新测量"})

    def win_minimize(self) -> dict:
        return self._win("win_minimize", {"ok": False})

    def win_maximize(self) -> dict:
        return self._win("win_maximize", {"ok": False})

    def win_mode(self, mode: str = "") -> dict:
        wanted = str(mode or "").strip().lower()
        if wanted not in {"docked", "free", "fullscreen"}:
            return {"ok": False, "error": "未知窗口模式：" + wanted, "modes": ["docked", "free", "fullscreen"]}
        result = self._win("win_mode", {"ok": False, "error": "当前窗口不支持切换模式"}, wanted)
        if isinstance(result, dict) and result.get("mode"):
            self.settings.update({"window_mode": result["mode"]})
        return result

    def win_fullscreen(self, on: bool = True) -> dict:
        return self.win_mode("fullscreen" if bool(on) else "docked")

    def win_close(self) -> dict:
        return self._win("win_close")
