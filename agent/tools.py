"""录放台的 Agent API 工具层（本标准里项目唯一需要写的文件）。

契约见 personal-agent-hub/docs/AGENT_API_STANDARD.md。每个工具都调用 ra/ 的真实能力：

| 工具 | 真实落点 |
| --- | --- |
| ra.status | ra.paths（数据目录/资源目录/浏览器缓存解析）、ra.store、ra.journal、ra.secrets |
| ra.list_workflows | ra.store.WorkflowStore.list() |
| ra.get_workflow | ra.store.load() + ra.store.problems()（Schema + 执行语义双层校验） |
| ra.validate_workflow | ra.store.problems()，不落盘 |
| ra.save_workflow | ra.store.save()（阻塞项会被拒绝写入） |
| ra.run_workflow | ra.api.Api.run_workflow → ra.session.Session → ra.engine → ra.core.Runner |
| ra.run_history | ra.journal.FileJournal + ra.api.Api.run_detail（真实步骤时间线） |
| ra.list_secret_refs | ra.secrets.FileSecretStore.preview()（只有引用名与长度） |

安全边界：
1. 秘密值永不返回，只返回引用名、长度与「缺哪些」。
2. 删除类动作（删工作流、删秘密值、清空浏览器登录态）不暴露为工具。
3. 写入与执行必须显式 confirm:true；缺 confirm 直接返回 bad_input。
4. 结果分类沿用 ra/core.py 的 completed / completed_unverified / failed / uncertain / cancelled，
   程序不自动重试，工具也不重跑。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from errors import AgentError            # 与 server.py 共用同一个类对象，别改成别的路径

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ra import __version__               # noqa: E402
from ra.normalizer import step_label     # noqa: E402
from ra.paths import APP_TITLE, data_root, resource_root   # noqa: E402
from ra.secrets import FileSecretStore   # noqa: E402
from ra.store import Settings, WorkflowStore               # noqa: E402

PROJECT = {
    "name": "recorded-automation",
    "version": __version__,
    "summary": "录放台：浏览器操作录制与自动化回放（本机单进程，Playwright 受控浏览器）",
}

MAX_TIMELINE = 300


# --------------------------------------------------------------------------- helpers
def _store_and_vault() -> tuple[WorkflowStore, FileSecretStore, Settings]:
    root = data_root()
    store = WorkflowStore(root / "workflows", resource_root() / "workflow.schema.json")
    return store, FileSecretStore(root / "secrets.json"), Settings(root / "settings.json")


def _journal():
    from ra.journal import FileJournal

    return FileJournal(data_root() / "journal.jsonl")


def _require_confirm(input: dict, action: str) -> None:
    if input.get("confirm") is not True:
        raise AgentError("bad_input", f"这个操作会{action}；请在 input 里显式传 confirm:true（这是给 Agent 的二次确认，不是开关）")


def _missing_refs(store: WorkflowStore, secrets: FileSecretStore, refs) -> list[str]:
    return sorted({ref for ref in (refs or []) if ref and not secrets.has(str(ref))})


def _workflow_refs(payload: dict) -> list[str]:
    return sorted({str(step.get("secret_ref")) for step in (payload.get("steps") or [])
                   if isinstance(step, dict) and step.get("secret_ref")})


def _need_workflow(input: dict) -> dict:
    payload = input.get("workflow")
    if not isinstance(payload, dict):
        raise AgentError("bad_input", "workflow 必须是 Workflow v1 对象（含 schema_version / id / origin / start_url / steps）")
    if not isinstance(payload.get("steps"), list) or not payload["steps"]:
        raise AgentError("bad_input", "workflow.steps 必须是非空数组")
    return payload


def _clip(rows: list[dict], limit: int) -> dict:
    return {"rows": rows[:limit], "total": len(rows), "truncated": len(rows) > limit}


class _RunSettings:
    """只影响这一次运行的临时设置：不改写用户的 settings.json。"""

    def __init__(self, base: Settings, headless: bool) -> None:
        self.base = base
        self.headless = headless

    def all(self) -> dict:
        payload = dict(self.base.all())
        payload["headless"] = self.headless
        payload["keep_browser_open"] = True        # 收尾由本工具负责，别让会话提前关掉浏览器
        return payload

    def update(self, patch: dict) -> dict:
        return self.base.update(patch)


def _launch_hint(error_text: str) -> str:
    text = (error_text or "").lower()
    if any(word in text for word in ("profile", "singleton", "already in use", "启动", "lock")):
        return "受控浏览器没能启动：正在运行的录放台应用窗口可能占用同一个浏览器档案目录，先关闭应用窗口再重试。"
    if "playwright install" in text or "没有可用的浏览器引擎" in (error_text or ""):
        return "没有可用的浏览器引擎：执行 python -m playwright install chromium，或安装 Microsoft Edge。"
    return ""


# --------------------------------------------------------------------------- tools
def _status(input: dict) -> dict:
    from ra.paths import browsers_path_env

    browsers_path_env()
    store, secrets, settings = _store_and_vault()
    root = data_root()
    candidates = [Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""),
                  Path(sys.executable).resolve().parent / "ms-playwright",
                  ROOT / "ms-playwright",
                  root.parent / "ms-playwright"]
    return {
        "app": APP_TITLE,
        "project": PROJECT["name"],
        "version": __version__,
        "data_dir": str(root),
        "resources": str(resource_root()),
        "bundled": bool(getattr(sys, "_MEIPASS", None)),
        "frozen": bool(getattr(sys, "frozen", False)),
        "workflows": len(store.list()),
        "runs": len(_journal().list_runs(limit=500)),
        "secret_refs": [item["ref"] for item in secrets.preview()],
        "settings": settings.all(),
        "browser_cache": {"env": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
                          "available": sorted({str(item) for item in candidates if item.is_dir()}),
                          "note": "这里只报告引擎缓存的解析结果；真正启动浏览器发生在 ra.run_workflow（ra/engine.py 会依次尝试 Chromium 与本机 Edge）"},
    }


def _list_workflows(input: dict) -> dict:
    store, secrets, _ = _store_and_vault()
    limit = max(1, min(int(input.get("limit") or 50), 200))
    rows = []
    for row in store.list():
        rows.append({
            "id": row["id"], "name": row["name"], "origin": row["origin"], "start_url": row["start_url"],
            "steps": row["steps"], "actions": row["actions"], "unverified": row["unverified"],
            "secret_refs": row["secret_refs"], "secret_refs_missing": _missing_refs(store, secrets, row["secret_refs"]),
            "broken": row["broken"], "updated_at": row["updated_at"], "file": row["file"],
        })
    return _clip(rows, limit)


def _get_workflow(input: dict) -> dict:
    workflow_id = str(input.get("workflow_id") or "").strip()
    if not workflow_id:
        raise AgentError("bad_input", "缺少必填参数：workflow_id")
    store, secrets, _ = _store_and_vault()
    try:
        payload = store.load(workflow_id)
    except KeyError:
        known = [row["id"] for row in store.list()]
        raise AgentError("not_found", f"没有 id 为 {workflow_id} 的工作流；本机现有：" + (", ".join(known) or "（还没有保存过工作流）")) from None
    steps = [dict(step, label=step_label(step), index=index)
             for index, step in enumerate(payload.get("steps") or [], start=1)]
    report = store.problems(payload)
    return {
        "workflow": dict(payload, steps=steps),
        "check": {"schema_ok": report["schema_ok"], "blocking": report["blocking"], "warnings": report["warnings"]},
        "secret_refs": _workflow_refs(payload),
        "secret_refs_missing": _missing_refs(store, secrets, _workflow_refs(payload)),
        "locator_strategies": sorted({locator.get("strategy") or "" for step in steps
                                      for locator in ((step.get("target") or {}).get("locators") or [])}),
    }


def _validate_workflow(input: dict) -> dict:
    payload = _need_workflow(input)
    store, secrets, _ = _store_and_vault()
    report = store.problems(payload)
    missing = _missing_refs(store, secrets, _workflow_refs(payload))
    return {"schema_ok": report["schema_ok"], "blocking": report["blocking"], "warnings": report["warnings"],
            "steps": len(payload.get("steps") or []), "secret_refs_missing": missing,
            "saved": False, "note": "只校验，没有写盘。要保存用 ra.save_workflow（需要 confirm:true）"}


def _save_workflow(input: dict) -> dict:
    payload = _need_workflow(input)
    _require_confirm(input, "写入或覆盖本机的工作流文件")
    store, secrets, _ = _store_and_vault()
    try:
        report = store.save(payload)
    except ValueError as exc:                       # 阻塞项：Schema 或执行语义不过
        raise AgentError("bad_input", str(exc)) from None
    except (KeyError, TypeError) as exc:
        raise AgentError("bad_input", f"工作流结构不完整：{exc}") from None
    return {"saved": True, "id": payload["id"], "steps": len(payload.get("steps") or []),
            "check": {"schema_ok": report["schema_ok"], "blocking": report["blocking"], "warnings": report["warnings"]},
            "secret_refs_missing": _missing_refs(store, secrets, _workflow_refs(payload)),
            "file": str(store.root / f"{payload['id']}.workflow.json")}


def _run(input: dict) -> dict:
    workflow_id = str(input.get("workflow_id") or "").strip()
    if not workflow_id:
        raise AgentError("bad_input", "缺少必填参数：workflow_id")
    _require_confirm(input, "在真实浏览器里执行这个工作流（会点击、填写、按键）")
    from_step = str(input.get("from_step") or "")
    headless = bool(input.get("headless", True))
    wait_s = max(5.0, min(float(input.get("wait_s") or 180.0), 900.0))

    from ra.main import build                        # 与界面同一个装配：Api → Session → Driver → Runner

    store, _, _ = _store_and_vault()
    try:
        store.load(workflow_id)
    except KeyError:
        known = [row["id"] for row in store.list()]
        raise AgentError("not_found", f"没有 id 为 {workflow_id} 的工作流；本机现有：" +
                         (", ".join(known) or "（还没有保存过工作流）")) from None

    api, session, _ = build()
    session.settings = _RunSettings(session.settings, headless)
    try:
        started = api.run_workflow(workflow_id, from_step)
        if started.get("error"):
            code = "needs_secret" if started.get("missing_secret") else "run_refused"
            raise AgentError(code, started["error"] + (
                "" if not started.get("missing_secret")
                else "；引用名可用 ra.list_secret_refs 查到，填值只能在界面上做（Agent 不经手秘密值）"))
        run_id = started["run_id"]
        deadline = time.time() + wait_s
        timed_out = False
        while time.time() < deadline:
            if session.run_status(run_id).get("status") != "running":
                break
            time.sleep(0.25)
        else:
            api.cancel_run()                          # 超时按用户已知的取消路径处理，不假装完成
            timed_out = True
            for _ in range(40):
                if session.run_status(run_id).get("status") != "running":
                    break
                time.sleep(0.25)
        detail = (api.run_detail(run_id) or {}).get("detail") or {}
        timeline = detail.get("timeline") or []
        out = {
            "run_id": run_id,
            "workflow_id": detail.get("workflow_id", workflow_id),
            "workflow_name": detail.get("workflow_name", ""),
            "status": detail.get("status", "unknown"),
            "status_label": detail.get("status_label", ""),
            "code": detail.get("code", ""),
            "stopped_at_step": detail.get("step_id", ""),
            "steps_total": detail.get("steps_total", 0),
            "steps_run": detail.get("steps_run", 0),
            "steps_unverified": detail.get("steps_unverified", 0),
            "duration_s": detail.get("duration_s", 0),
            "resumed_from": started.get("from_step", "") or (detail.get("resumed") or {}).get("from_step", ""),
            "skipped_steps": started.get("skipped", 0),
            "headless": headless,
            "timed_out": timed_out,
            "uncertain": detail.get("status") == "uncertain",
            "timeline": timeline[:MAX_TIMELINE],
            "timeline_truncated": len(timeline) > MAX_TIMELINE,
            "events": len(detail.get("events") or []),
            "journal": detail.get("journal_path", ""),
            "error": detail.get("error", ""),
        }
        if out.get("resumed_from"):
            out["resumed_note"] = f"本次从步骤 {out['resumed_from']} 续跑，前面的步骤没有重新执行"
        if timed_out:
            out["note"] = f"等待 {wait_s:.0f} 秒未结束，已按停止请求取消；停在当前步骤，之后不会自动重试"
        elif out["error"]:
            out["note"] = out["error"] + (("\n" + _launch_hint(out["error"])) if _launch_hint(out["error"]) else "")
        if out["status"] == "uncertain":
            out["manual_check"] = "动作已发出且只发一次，无法确认页面是否生效；先人工检查受控浏览器页面，再决定重新运行或从失败步续跑"
        return out
    finally:
        try:
            api.shutdown()                            # 一定关掉本次自己起的受控浏览器
        except Exception:
            pass


def _run_history(input: dict) -> dict:
    run_id = str(input.get("run_id") or "").strip()
    journal = _journal()
    if run_id:
        from ra.main import build

        api, session, _ = build()                     # 只读时间线，不会启动浏览器
        try:
            detail = (api.run_detail(run_id) or {}).get("detail") or {}
        finally:
            session.shutdown()
        if not detail.get("events"):
            raise AgentError("not_found", f"日志里没有 {run_id} 这个阶段事件；最近运行可用不带 run_id 的调用查到")
        timeline = detail.get("timeline") or []
        return {"run": {key: detail.get(key) for key in (
            "run_id", "workflow_id", "workflow_name", "status", "status_label", "code", "step_id",
            "started_at", "duration_s", "steps_total", "steps_run", "steps_unverified", "durable")},
            "timeline": timeline[:MAX_TIMELINE], "timeline_truncated": len(timeline) > MAX_TIMELINE,
            "events": detail.get("events") or [], "journal_path": detail.get("journal_path", ""),
            "resumed": detail.get("resumed") or {},
            "note": "结果分类沿用 ra/core.py：failed 表示动作之前停止，uncertain 表示动作已发出但无法确认"}
    limit = max(1, min(int(input.get("limit") or 30), 200))
    store, _, _ = _store_and_vault()
    names = {row["id"]: row["name"] for row in store.list()}
    rows = []
    for row in journal.list_runs(limit=limit):
        rows.append({
            "run_id": row["run_id"], "workflow_id": row["workflow_id"],
            "workflow_name": names.get(row["workflow_id"], row["workflow_id"]),
            "workflow_deleted": row["workflow_id"] not in names,
            "status": row["status"] or "running_or_unknown", "stopped_at_step": row["step_id"],
            "code": row["code"], "steps": row["steps"], "duration_s": row["duration_s"],
            "started_at": row["started_at"], "ended_at": row["ended_at"],
        })
    return _clip(rows, limit)


def _secret_refs(input: dict) -> dict:
    store, secrets, _ = _store_and_vault()
    used_by: dict[str, list[str]] = {}
    for row in store.list():
        for ref in row.get("secret_refs") or []:
            used_by.setdefault(ref, []).append(row["name"] or row["id"])
    stored = secrets.preview()                        # 只有 ref 与长度，值不在这个函数之外出现过
    return {"stored_refs": [item["ref"] for item in stored], "stored_lengths": stored,
            "referenced_by_workflows": sorted(used_by), "used_by": used_by,
            "missing": sorted({ref for ref in used_by if not secrets.has(ref)}),
            "unused": sorted({item["ref"] for item in stored if item["ref"] not in used_by}),
            "values_returned": False,
            "note": "秘密值只在界面上填写、由 Windows 当前用户账户加密保存，工具永不返回；缺值时运行会被直接拦下并列出引用名"}


TOOLS = [
    {
        "name": "ra.status",
        "description": "看录放台现在的真实状态：版本、数据目录、工作流与运行次数、秘密引用名、设置、浏览器缓存解析。用于决定接下来调用哪个工具。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "risk": "read",
        "handler": _status,
    },
    {
        "name": "ra.list_workflows",
        "description": "列出本机已保存的工作流摘要（id、名称、origin、步数、动作类型、未验证步数、用到的秘密引用名与缺失项）。不返回步骤详情，详情用 ra.get_workflow。",
        "input_schema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "最多返回多少条，默认 50"}},
            "additionalProperties": False},
        "risk": "read",
        "handler": _list_workflows,
    },
    {
        "name": "ra.get_workflow",
        "description": "读取一个工作流的完整 Workflow v1 定义：每一步的编号、可读标签、动作、目标定位器与匹配策略，外加 Schema/执行语义双层校验结果、用到的秘密引用名与缺失项。",
        "input_schema": {"type": "object", "properties": {
            "workflow_id": {"type": "string", "description": "工作流 id，例如 wf_contact"}},
            "required": ["workflow_id"], "additionalProperties": False},
        "risk": "read",
        "handler": _get_workflow,
    },
    {
        "name": "ra.validate_workflow",
        "description": "对一份 Workflow v1 定义跑一遍保存前的双层校验（JSON Schema + 执行语义：步骤 ID 唯一、start_url 与 origin 同源、role 定位器带可访问名称、令牌参数拒绝），只校验不写盘。",
        "input_schema": {"type": "object", "properties": {
            "workflow": {"type": "object", "description": "完整的 Workflow v1 对象"}},
            "required": ["workflow"], "additionalProperties": False},
        "risk": "read",
        "handler": _validate_workflow,
    },
    {
        "name": "ra.save_workflow",
        "description": "保存或覆盖本机的工作流文件（写入 %LOCALAPPDATA%\\RecordedAutomation\\workflows\\<id>.workflow.json）。有阻塞项时会被校验层拒绝并返回原因。必须显式 confirm:true。返回校验结果与还缺哪些秘密引用名。",
        "input_schema": {"type": "object", "properties": {
            "workflow": {"type": "object", "description": "完整的 Workflow v1 对象"},
            "confirm": {"type": "boolean", "description": "必须为 true：确认这是一次写入操作"}},
            "required": ["workflow", "confirm"], "additionalProperties": False},
        "risk": "write",
        "handler": _save_workflow,
    },
    {
        "name": "ra.run_workflow",
        "description": "在受控浏览器里按步骤真实回放一个已保存的工作流，返回真实的步骤时间线、结果分类（completed / completed_unverified / failed / uncertain / cancelled）、错误码与停在的步骤。必须显式 confirm:true。from_step 用于人工指定的「从失败步续跑」。秘密值缺失时会在打开浏览器之前被拦下并列出引用名。",
        "input_schema": {"type": "object", "properties": {
            "workflow_id": {"type": "string", "description": "要运行的工作流 id"},
            "from_step": {"type": "string", "description": "可选：从这一步的 id 开始跑（前序步骤不重跑）"},
            "headless": {"type": "boolean", "description": "默认 true 无界面运行；要看页面传 false"},
            "wait_s": {"type": "number", "minimum": 5, "maximum": 900, "description": "最多等多久，超时按取消处理，默认 180"},
            "confirm": {"type": "boolean", "description": "必须为 true：确认这是一次真实执行"}},
            "required": ["workflow_id", "confirm"], "additionalProperties": False},
        "risk": "exec",
        "handler": _run,
    },
    {
        "name": "ra.run_history",
        "description": "查运行记录：不带 run_id 时返回最近的运行摘要（从 journal.jsonl 数出来的）；带 run_id 时返回那次运行的完整时间线与阶段事件。日志只含 ID、阶段、错误码，不含输入内容或秘密值。",
        "input_schema": {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "某次运行的 id"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "列表模式返回多少条，默认 30"}},
            "additionalProperties": False},
        "risk": "read",
        "handler": _run_history,
    },
    {
        "name": "ra.list_secret_refs",
        "description": "列出秘密库的引用名（以及每个引用被哪些工作流使用、哪些还缺值、哪些没人用）。只返回名字与字符长度，绝不返回秘密值。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "risk": "read",
        "handler": _secret_refs,
    },
]
