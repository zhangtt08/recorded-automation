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

安全边界（与 personal-agent-hub/docs/AGENT_API_STANDARD.md 第 4 条同一条判据）：
1. 秘密值永不返回，只返回引用名、长度与「缺哪些」。
2. 删工作流与删秘密引用都开放给 Agent，但都要显式 confirm:true；
   清空浏览器登录态仍然只在界面里做（那是带确认框的人工动作）。
3. confirm 的判据按风险档走，不看意图：`exec`（真的跑活、真的写盘、真的起进程）必须有、缺省拒绝；
   `write` 只有名字或描述含破坏性动词（删除 / 清空 / 覆盖 / 重置）时才强制；
   对自己那一场录制的可逆收尾（`ra.record_stop`：不写盘、不起进程、不驱动页面）不强制 ——
   标准点名的真退化恰恰是「把 confirm 塞给每一个写入」，那只会把调用方训练成无脑传 true。
   这条判据由 tests/test_agent_api.py 逐条扫全表，红在这里，而不是等作品集的验收器报出来。
4. 结果分类沿用 ra/core.py 的 completed / completed_unverified / failed / uncertain / cancelled，
   程序不自动重试，工具也不重跑。
"""
from __future__ import annotations

import json
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

# 进程内服务（ra/agentapi.py）会 bind 界面那套会话；独立跑 agent/server.py 时才自己装配。
RUNTIME: dict[str, object] = {"api": None, "session": None}


def bind(api, session) -> None:
    """让工具复用界面正在用的 Session：Agent 的录制与运行会实时反映在界面上，
    也不会和界面抢同一个浏览器档案目录。"""
    RUNTIME["api"] = api
    RUNTIME["session"] = session


def bound() -> bool:
    return RUNTIME.get("api") is not None


def _runtime():
    """返回 (api, session, owned)。owned=True 表示这套是本次临时装的，用完必须关掉。"""
    if RUNTIME.get("api") is not None:
        return RUNTIME["api"], RUNTIME["session"], False
    from ra.main import build

    api, session, _ = build()
    return api, session, True


def _headless_for(session, headless: bool):
    """复用界面会话时临时改这一次运行的 headless，用完原样还回去。"""
    if not bound():
        session.settings = _RunSettings(session.settings, headless)
        return None
    original = session.settings
    session.settings = _RunSettings(original, headless)
    return original




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
        "journal": _journal().stats(),
        "secret_refs": [item["ref"] for item in secrets.preview()],
        "settings": settings.all(),
        "serving": {"in_process": bound(),
                    "state": (RUNTIME["session"].snapshot() if bound() and RUNTIME.get("session") else {}),
                    "note": "in_process=true 表示这个接口跑在桌面程序同一个进程里，界面能实时看到 Agent 的录制与运行"},
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
    raw_wait = input.get("wait_s")
    wait_s = 0.0 if raw_wait == 0 else max(5.0, min(float(raw_wait if raw_wait is not None else 180.0), 900.0))

    store, _, _ = _store_and_vault()
    try:
        store.load(workflow_id)
    except KeyError:
        known = [row["id"] for row in store.list()]
        raise AgentError("not_found", f"没有 id 为 {workflow_id} 的工作流；本机现有：" +
                         (", ".join(known) or "（还没有保存过工作流）")) from None

    api, session, owned = _runtime()                 # 与界面同一个装配：Api → Session → Driver → Runner
    original_settings = _headless_for(session, headless)
    try:
        started = api.run_workflow(workflow_id, from_step)
        if started.get("error"):
            code = "needs_secret" if started.get("missing_secret") else "run_refused"
            raise AgentError(code, started["error"] + (
                "" if not started.get("missing_secret")
                else "；引用名可用 ra.list_secret_refs 查到，填值只能在界面上做（Agent 不经手秘密值）"))
        run_id = started["run_id"]
        if wait_s == 0:                              # 只发起不等待：长流程交给 Agent 自己轮询
            return {"run_id": run_id, "workflow_id": workflow_id, "status": "running", "async": True,
                    "steps": started.get("steps", []), "headless": headless,
                    "note": "已发起、未等待。用 ra.run_status 轮询，用 ra.cancel_run 停止；"
                            "程序不会自动重试，也不会替你判断结果"}
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
        if original_settings is not None:
            session.settings = original_settings
        if owned:
            try:
                api.shutdown()                        # 只关本次自己起的受控浏览器，别把界面的关掉
            except Exception:
                pass


def _run_history(input: dict) -> dict:
    run_id = str(input.get("run_id") or "").strip()
    journal = _journal()
    if run_id:
        api, session, owned = _runtime()              # 只读时间线，不会启动浏览器
        try:
            detail = (api.run_detail(run_id) or {}).get("detail") or {}
        finally:
            if owned:
                session.shutdown()
        if not detail.get("events"):
            stats = journal.stats()
            extra = f"（{stats['note']}）" if stats["trimmed"] else ""
            raise AgentError("not_found",
                             f"日志里没有 {run_id} 这个阶段事件{extra}；最近运行可用不带 run_id 的调用查到")
        timeline = detail.get("timeline") or []
        return {"run": {key: detail.get(key) for key in (
            "run_id", "workflow_id", "workflow_name", "status", "status_label", "code", "step_id",
            "started_at", "duration_s", "steps_total", "steps_run", "steps_unverified", "durable")},
            "timeline": timeline[:MAX_TIMELINE], "timeline_truncated": len(timeline) > MAX_TIMELINE,
            "events": detail.get("events") or [], "journal_path": detail.get("journal_path", ""),
            "journal": journal.stats(),
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
    result = _clip(rows, limit)
    stats = journal.stats()
    if stats["trimmed"]:          # 轮转过的话，这份"最近运行"不是全部 —— 必须随结果一起说出来
        result["journal"] = stats
    return result


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


# ------------------------------------------------------------------ 交付版新增能力
def _schema(input: dict) -> dict:
    """把「怎么写一份能跑的工作流」的全部约束一次交给 Agent，不用它去读源码。"""
    schema_path = resource_root() / "workflow.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.is_file() else {}
    example_path = ROOT / "example.workflow.json"
    example = json.loads(example_path.read_text(encoding="utf-8")) if example_path.is_file() else {}
    return {
        "schema": schema, "schema_path": str(schema_path), "example": example,
        "actions": ["click", "fill", "hotkey", "wait_for"],
        "locator_strategies": ["role", "label", "test_id", "text", "css"],
        "locator_shapes": {
            "每一个定位器都是 {strategy, value}，additionalProperties:false": "strategy 只能是那五种",
            "role": {"strategy": "role", "value": "button|textbox|checkbox|...", "name": "可访问名称（role 必填）"},
            "label": {"strategy": "label", "value": "表单 label 文本"},
            "test_id": {"strategy": "test_id", "value": "data-testid 值"},
            "text": {"strategy": "text", "value": "页面上可见的文本"},
            "css": {"strategy": "css", "value": "CSS 选择器"},
            "step_shapes": {
                "click / wait_for": "需要 target；可选 expected、timeout_s",
                "fill": "需要 target，并且 text 与 secret_ref 二选一（同时给会被拒）",
                "hotkey": "需要 hotkey 字段，不需要 target",
                "expected": "和 target 同形 {page, locators}，是动作之后的可观察完成条件；不写则结果为 completed_unverified",
                "不允许的字段": "additionalProperties:false —— 例如把 fill 的内容写成 value 会被直接拒绝",
            },
            "注意": "role 之外不要带 name，带了会被 Schema 直接拒绝；target 形如 "
                   "{\"page\": \"main\", \"locators\": [ ... ]}，steps 里 expected 可以省略",
        },
        "rules": [
            "schema_version 固定为 1；id 满足 ^[A-Za-z0-9_-]{1,64}$，同时作为文件名",
            "steps 非空且 step.id 在工作流内唯一",
            "start_url 与 origin 必须同源，且 URL 里不能带 token/password 这类令牌参数",
            "role 定位器必须带 name（可访问名称），否则保存被拒",
            "target.locators 按 role → label → test_id → text → css 排序；多匹配会停在动作之前，需要人工选定",
            "expected 是执行后的可观察完成条件（ref 指向另一步骤的目标，或按页面文本判断）；"
            "不设则结果为 completed_unverified",
            "秘密值只写 secret_ref 引用名，值由人在界面或 ra.set_secret 填进本机加密库；工作流与日志里永不出现明文",
        ],
        "result_states": {
            "completed": "所有步骤执行完，且设了完成条件的都被验证过",
            "completed_unverified": "步骤执行完，但有动作没设完成条件，程序没有证据",
            "uncertain": "动作已发出且只发出一次，之后超时/异常/取消，无法判断页面是否生效；不自动重试",
            "failed": "在动作发生之前就停了（目标缺失、多匹配、来源变化、缺秘密值）",
            "cancelled": "人工点了停止",
        },
        "note": "这份返回就是保存前双层校验所依据的约束；改之前可先跑 ra.validate_workflow（不落盘）",
    }


def _cleanup(api, owned: bool) -> None:
    if owned:
        try:
            api.shutdown()                    # 只关本次临时装的浏览器，别把界面那套关掉
        except Exception:
            pass


def _delete_workflow(input: dict) -> dict:
    workflow_id = str(input.get("workflow_id") or "").strip()
    if not workflow_id:
        raise AgentError("bad_input", "缺少必填参数：workflow_id")
    _require_confirm(input, f"删除本机工作流 {workflow_id}（运行日志会保留）")
    api, session, owned = _runtime()
    try:
        result = api.delete_workflow(workflow_id)
        if isinstance(result, dict) and result.get("error"):
            raise AgentError("bad_input", result["error"])
        return {"deleted": True, "id": workflow_id,
                "note": "只删定义文件；journal 里已经发生的事实不会被删掉，历史运行会标出「工作流已删除」"}
    finally:
        _cleanup(api, owned)


def _set_secret(input: dict) -> dict:
    ref = str(input.get("ref") or "").strip()
    value = input.get("value")
    if not ref:
        raise AgentError("bad_input", "缺少必填参数：ref（秘密引用名）")
    if not isinstance(value, str) or value == "":
        raise AgentError("bad_input", "value 必须是非空字符串；要删除用 ra.delete_secret")
    _require_confirm(input, f"写入秘密引用 {ref} 的值（用当前 Windows 账户加密保存在本机）")
    _, secrets, _ = _store_and_vault()
    secrets.set(ref, value)
    return {"ref": ref, "stored": True, "length": len(value), "value_returned": False,
            "note": "值只写进本机加密库，任何工具的返回里都不会再出现它"}


def _delete_secret(input: dict) -> dict:
    ref = str(input.get("ref") or "").strip()
    if not ref:
        raise AgentError("bad_input", "缺少必填参数：ref")
    _require_confirm(input, f"删除秘密引用 {ref}；用到它的工作流会在打开浏览器之前被拦下")
    _, secrets, _ = _store_and_vault()
    secrets.delete(ref)                                  # delete() 不返回东西，只能回读确认
    return {"ref": ref, "deleted": not secrets.has(ref)}


def _app_state(input: dict) -> dict:
    api, session, owned = _runtime()
    try:
        snapshot = session.snapshot() if session is not None else {}
        draft = (api.draft() or {}).get("draft") or {}
        window = (api.win_state() or {}) if getattr(api, "window", None) is not None else {}
        return {"state": snapshot,
                "draft_steps": len(draft.get("steps") or []),
                "draft_problems": draft.get("problems") or [],
                "window": {"mode": window.get("mode"), "frameless": window.get("frameless"),
                           "titlebar_hidden": window.get("titlebar_hidden"),
                           "strip": window.get("strip"), "content": window.get("content")},
                "in_process": bound(),
                "note": "in_process=true 时这里读到的就是界面正在显示的状态；录制与回放互斥"}
    finally:
        _cleanup(api, owned)


def _record_start(input: dict) -> dict:
    url = str(input.get("url") or "").strip()
    if not url:
        raise AgentError("bad_input", "缺少必填参数：url（要录制的页面地址，录制范围就是这个来源）")
    _require_confirm(input, "打开受控浏览器并开始录制操作")
    api, session, owned = _runtime()
    try:
        result = api.start_recording(url) or {}
        if result.get("error"):
            hint = _launch_hint(result["error"])
            raise AgentError("record_refused", result["error"] + (("\n" + hint) if hint else ""))
        return {"recording": True, "url": url, "state": (api.state() or {}),
                "note": "在受控浏览器里操作即产生候选事件；ra.draft 读、ra.record_stop 结束。"
                        "候选事件必须经审阅（人工或 ra.edit_step）才会保存为工作流"}
    finally:
        _cleanup(api, owned)


def _record_stop(input: dict) -> dict:
    api, session, owned = _runtime()
    try:
        result = api.stop_recording() or {}
        if result.get("error"):
            raise AgentError("stop_failed", result["error"])
        draft = (api.draft() or {}).get("draft") or {}
        return {"recording": False, "steps": len(draft.get("steps") or []),
                "dropped": draft.get("dropped", 0), "draft": draft,
                "note": "已停止录制；草稿用 ra.edit_step / ra.move_step / ra.remove_step 剪辑，ra.save_draft 保存"}
    finally:
        _cleanup(api, owned)


def _draft(input: dict) -> dict:
    api, session, owned = _runtime()
    try:
        draft = (api.draft() or {}).get("draft") or {}
        steps = [dict(step, label=step_label(step), index=index)
                 for index, step in enumerate(draft.get("steps") or [], start=1)]
        return {"steps": steps, "problems": draft.get("problems") or [],
                "start_url": draft.get("start_url", ""), "origin": draft.get("origin", ""),
                "dropped": draft.get("dropped", 0),
                "note": "这是当前草稿（未保存）"}
    finally:
        _cleanup(api, owned)


def _edit_step(input: dict) -> dict:
    step_id = str(input.get("step_id") or "").strip()
    patch = input.get("patch")
    if not step_id:
        raise AgentError("bad_input", "缺少必填参数：step_id（草稿里的步骤 id）")
    if not isinstance(patch, dict) or not patch:
        raise AgentError("bad_input",
                         "patch 必须是非空对象，例如 {\"target\": {\"locators\": [...]}} 或 {\"expected\": {...}}")
    _require_confirm(input, "修改当前草稿里的一个步骤")
    api, session, owned = _runtime()
    try:
        result = api.edit_step(step_id, patch) or {}
        if result.get("error"):
            raise AgentError("bad_input", result["error"])
        draft = (api.draft() or {}).get("draft") or {}
        return {"edited": step_id, "steps": len(draft.get("steps") or []), "draft": draft,
                "note": "改的是草稿，不是已保存的工作流；保存用 ra.save_draft"}
    finally:
        _cleanup(api, owned)


def _move_step(input: dict) -> dict:
    step_id = str(input.get("step_id") or "").strip()
    if not step_id:
        raise AgentError("bad_input", "缺少必填参数：step_id")
    _require_confirm(input, "调整草稿里步骤的顺序")
    api, session, owned = _runtime()
    try:
        draft = (api.draft() or {}).get("draft") or {}
        current = [step.get("id") for step in draft.get("steps") or []]
        if step_id not in current:
            raise AgentError("not_found", f"草稿里没有步骤 {step_id}；现有：" + ", ".join(current))
        if input.get("to_index") is not None:
            offset = int(input["to_index"]) - current.index(step_id)
        else:
            offset = int(input.get("offset") if input.get("offset") is not None else -1)
        result = api.move_step(step_id, offset) or {}
        if isinstance(result, dict) and result.get("error"):
            raise AgentError("bad_input", result["error"])
        draft = (api.draft() or {}).get("draft") or {}
        return {"moved": step_id, "order": [step.get("id") for step in draft.get("steps") or []]}
    finally:
        _cleanup(api, owned)


def _remove_step(input: dict) -> dict:
    step_id = str(input.get("step_id") or "").strip()
    if not step_id:
        raise AgentError("bad_input", "缺少必填参数：step_id")
    _require_confirm(input, f"从草稿里删掉步骤 {step_id}")
    api, session, owned = _runtime()
    try:
        result = api.remove_step(step_id) or {}
        if isinstance(result, dict) and result.get("error"):
            raise AgentError("bad_input", result["error"])
        draft = (api.draft() or {}).get("draft") or {}
        return {"removed": step_id, "steps": len(draft.get("steps") or []),
                "order": [step.get("id") for step in draft.get("steps") or []]}
    finally:
        _cleanup(api, owned)


def _add_step(input: dict) -> dict:
    _require_confirm(input, "往草稿里追加一个步骤")
    api, session, owned = _runtime()
    try:
        action = str(input.get("action") or "wait_for")
        draft = (api.draft() or {}).get("draft") or {}
        if action == "wait_for":
            result = api.add_wait_step() or {}
        else:
            built = (api.draft() or {}).get("draft") or {}
            step = {"id": str(input.get("id") or f"s{len(built.get('steps') or []) + 1}"),
                    "action": action, "target": input.get("target") or {},
                    "timeout_s": float(input.get("timeout_s") or 10.0)}
            text = input.get("text") if input.get("text") is not None else input.get("value")
            if text is not None:
                step["text"] = str(text)
            if input.get("secret_ref"):
                step["secret_ref"] = input["secret_ref"]
            if input.get("expected"):
                step["expected"] = input["expected"]
            appended = dict(built, steps=list(built.get("steps") or []) + [step])
            if hasattr(api.session, "load_draft"):
                result = api.session.load_draft(appended)
            else:
                result = {"error": "当前会话不支持追加任意动作：改用 ra.save_workflow 写完整定义"}
        if isinstance(result, dict) and result.get("error"):
            raise AgentError("bad_input", result["error"])
        built = (api.draft() or {}).get("draft") or {}
        return {"added": True, "action": action, "steps": len(built.get("steps") or []),
                "order": [step.get("id") for step in built.get("steps") or []]}
    finally:
        _cleanup(api, owned)


def _save_draft(input: dict) -> dict:
    _require_confirm(input, "把当前草稿校验后保存为本机工作流")
    api, session, owned = _runtime()
    try:
        workflow_id = str(input.get("workflow_id") or "")
        name = str(input.get("name") or "")
        built = api.preview_workflow(workflow_id, name) or {}
        if built.get("error"):
            raise AgentError("bad_input", built["error"])
        problems = (built.get("check") or {}).get("blocking") or (built.get("problems") or [])
        if problems:
            raise AgentError("bad_input", "草稿还有阻塞项：" + "；".join(problems[:5]))
        payload = dict(built.get("workflow") or {})
        if workflow_id:
            payload["id"] = workflow_id
        if name:
            payload["name"] = name
        saved = api.save_workflow(payload) or {}
        if saved.get("error"):
            raise AgentError("bad_input", saved["error"])
        return {"saved": True, "id": saved.get("id"), "steps": len(payload.get("steps") or []),
                "needs_secret": saved.get("needs_secret") or [],
                "warnings": (saved.get("check") or {}).get("warnings") or [],
                "file": str(data_root() / "workflows" / f"{saved.get('id')}.workflow.json"),
                "note": "needs_secret 非空时运行会被拦下，先 ra.set_secret 补值"}
    finally:
        _cleanup(api, owned)


def _run_status(input: dict) -> dict:
    run_id = str(input.get("run_id") or "").strip()
    if not run_id:
        raise AgentError("bad_input", "缺少必填参数：run_id")
    api, session, owned = _runtime()
    try:
        status = (api.run_status(run_id) or {}).get("run") or {}
        detail = (api.run_detail(run_id) or {}).get("detail") or {}
        timeline = detail.get("timeline") or []
        return {"run_id": run_id, "status": status.get("status") or detail.get("status") or "unknown",
                "running": (status.get("status") or "") == "running",
                "step_id": detail.get("step_id", ""), "code": detail.get("code", ""),
                "steps_total": detail.get("steps_total", 0), "steps_run": detail.get("steps_run", 0),
                "duration_s": detail.get("duration_s", 0),
                "timeline": timeline[:MAX_TIMELINE], "timeline_truncated": len(timeline) > MAX_TIMELINE,
                "resumable": detail.get("resumable", False), "uncertain": detail.get("uncertain", False),
                "note": "running 时继续轮询；failed/cancelled 且 resumable 时可用 ra.run_workflow 的 from_step 续跑"}
    finally:
        _cleanup(api, owned)


def _cancel_run(input: dict) -> dict:
    _require_confirm(input, "给正在运行的回放发出停止请求（停在当前步骤，不自动重试）")
    api, session, owned = _runtime()
    try:
        result = api.cancel_run() or {}
        if isinstance(result, dict) and result.get("error"):
            raise AgentError("cancel_failed", result["error"])
        return {"cancelled": True, "state": (api.state() or {}),
                "note": "停止在当前动作返回之后生效；已发出的动作不会被重发"}
    finally:
        _cleanup(api, owned)


NEW_TOOLS = [
    {"name": "ra.workflow_schema", "risk": "read", "handler": _schema,
     "description": "取回 Workflow v1 的 JSON Schema、动作与定位器目录、可跑的最小示例，以及保存前双层校验"
                    "的全部规则和结果分类含义。写工作流前先调这个，不用读源码。",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "ra.app_state", "risk": "read", "handler": _app_state,
     "description": "读当前会话状态机（idle/recording/review/running）、草稿步数与阻塞项、界面窗口的实测形态"
                    "（模式、标题条是否已顶出屏幕、内容矩形）。",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "ra.record_start", "risk": "exec", "handler": _record_start,
     "description": "打开受控浏览器并开始录制（需要 confirm:true）。之后由人在浏览器里操作，用 ra.draft 读候选事件。",
     "input_schema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "要录制的页面地址"},
         "confirm": {"type": "boolean", "description": "必须为 true"}},
         "required": ["url", "confirm"], "additionalProperties": False}},
    {"name": "ra.record_stop", "risk": "write", "handler": _record_stop,
     "description": "结束自己这一场录制，并把整理好的草稿原样返回（步数、丢弃计数、问题列表）。"
                    "这是对自己那场录制的可逆收尾：不写本机文件、不起进程、也不驱动页面，"
                    "所以按标准的 write 档处理、不要求 confirm（把 confirm 塞给每一个写入才是真的退化）。"
                    "要 confirm 的是开始录制（ra.record_start，它真的开浏览器）与把草稿存为工作流（ra.save_draft）。",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "ra.draft", "risk": "read", "handler": _draft,
     "description": "读当前草稿：每步的编号、id、动作、目标定位器与匹配情况、还缺什么。不写盘。",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "ra.edit_step", "risk": "write", "handler": _edit_step,
     "description": "剪辑草稿里的一个步骤（换定位器、补完成条件 expected、改 value/secret_ref/timeout_s）。"
                    "需要 confirm:true。",
     "input_schema": {"type": "object", "properties": {
         "step_id": {"type": "string"}, "patch": {"type": "object"},
         "confirm": {"type": "boolean"}},
         "required": ["step_id", "patch", "confirm"], "additionalProperties": False}},
    {"name": "ra.move_step", "risk": "write", "handler": _move_step,
     "description": "调整草稿步骤顺序：offset（负数前移）或 to_index（0 基目标位置）。需要 confirm:true。",
     "input_schema": {"type": "object", "properties": {
         "step_id": {"type": "string"}, "offset": {"type": "integer"}, "to_index": {"type": "integer"},
         "confirm": {"type": "boolean"}},
         "required": ["step_id", "confirm"], "additionalProperties": False}},
    {"name": "ra.remove_step", "risk": "write", "handler": _remove_step,
     "description": "从草稿里删掉一个步骤。需要 confirm:true。",
     "input_schema": {"type": "object", "properties": {
         "step_id": {"type": "string"}, "confirm": {"type": "boolean"}},
         "required": ["step_id", "confirm"], "additionalProperties": False}},
    {"name": "ra.add_step", "risk": "write", "handler": _add_step,
     "description": "往草稿追加步骤：action=wait_for 追加等待步；其他动作请改用 ra.save_workflow 写完整定义。"
                    "需要 confirm:true。",
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["wait_for", "click", "fill", "hotkey"]},
         "id": {"type": "string"}, "target": {"type": "object"},
         "text": {"type": "string", "description": "fill 要写进去的普通文本（秘密值改用 secret_ref）"},
         "secret_ref": {"type": "string"}, "expected": {"type": "object"},
         "timeout_s": {"type": "number"}, "confirm": {"type": "boolean"}},
         "required": ["confirm"], "additionalProperties": False}},
    {"name": "ra.save_draft", "risk": "write", "handler": _save_draft,
     "description": "把当前草稿过一遍保存前校验并写成本机工作流。有阻塞项直接返回原因，不会写坏文件。"
                    "需要 confirm:true。",
     "input_schema": {"type": "object", "properties": {
         "workflow_id": {"type": "string", "description": "可选：保存用的 id，缺省沿用草稿自己的 id"},
         "name": {"type": "string", "description": "可选：界面显示名"},
         "confirm": {"type": "boolean"}},
         "required": ["confirm"], "additionalProperties": False}},
    {"name": "ra.run_status", "risk": "read", "handler": _run_status,
     "description": "轮询一次运行的实时状态：status / running / 停在哪一步 / 时间线 / 是否可续跑。"
                    "配合 ra.run_workflow 的 wait_s:0 使用。",
     "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"}},
                      "required": ["run_id"], "additionalProperties": False}},
    {"name": "ra.cancel_run", "risk": "exec", "handler": _cancel_run,
     "description": "给正在运行的回放发停止请求（人工决定，不是自动重试）。需要 confirm:true。",
     "input_schema": {"type": "object", "properties": {"confirm": {"type": "boolean"}},
                      "required": ["confirm"], "additionalProperties": False}},
    {"name": "ra.delete_workflow", "risk": "write", "handler": _delete_workflow,
     "description": "删除本机一个工作流定义（运行日志保留）。需要 confirm:true 并显式给出 workflow_id。",
     "input_schema": {"type": "object", "properties": {
         "workflow_id": {"type": "string"}, "confirm": {"type": "boolean"}},
         "required": ["workflow_id", "confirm"], "additionalProperties": False}},
    {"name": "ra.set_secret", "risk": "write", "handler": _set_secret,
     "description": "给秘密引用赋值：用当前 Windows 账户加密写进本机 secrets.json。返回值只有引用名和长度，"
                    "永不回显值。需要 confirm:true。",
     "input_schema": {"type": "object", "properties": {
         "ref": {"type": "string"}, "value": {"type": "string"}, "confirm": {"type": "boolean"}},
         "required": ["ref", "value", "confirm"], "additionalProperties": False}},
    {"name": "ra.delete_secret", "risk": "write", "handler": _delete_secret,
     "description": "删除一个秘密引用。用到它的工作流会在打开浏览器之前被拦下并列出引用名。需要 confirm:true。",
     "input_schema": {"type": "object", "properties": {
         "ref": {"type": "string"}, "confirm": {"type": "boolean"}},
         "required": ["ref", "confirm"], "additionalProperties": False}},
]

TOOLS += NEW_TOOLS


def _window_state(input: dict) -> dict:
    api, session, owned = _runtime()
    try:
        state = api.win_state() or {}
        return {key: state.get(key) for key in (
            "mode", "docked", "maximized", "fullscreen", "frameless", "titlebar_hidden", "strip", "insets",
            "content", "rect", "client", "viewport", "monitor", "work", "caption", "hwnd", "cdp",
            "supported", "place_error", "asked", "band_bottom", "work_top",
                    "debug", "pixel_ok", "calibrated", "via")}             if state.get("mode") is not None or state.get("supported") is not None else {"error": "没有窗口可测"}
    finally:
        _cleanup(api, owned)


def _window_mode(input: dict) -> dict:
    mode = str(input.get("mode") or "")
    _require_confirm(input, f"把界面窗口切成 {mode or '?'} 模式")
    api, session, owned = _runtime()
    try:
        result = api.win_mode(mode) or {}
        if not result.get("ok"):
            raise AgentError("mode_refused", result.get("error") or "切换失败")
        return result
    finally:
        _cleanup(api, owned)


TOOLS += [
    {"name": "ra.window_state", "risk": "read", "handler": _window_state,
     "description": "读界面窗口的实测形态：模式、Chromium 自绘标题条高度、内容矩形、窗口矩形、工作区、"
                    "标题条是否已被顶出屏幕，以及摆放失败时的原因。",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "ra.window_mode", "risk": "write", "handler": _window_mode,
     "description": "切换界面窗口形态：docked（停靠无边框）/ free（自由窗口，会露出浏览器标题条）/ fullscreen。"
                    "需要 confirm:true。",
     "input_schema": {"type": "object", "properties": {
         "mode": {"type": "string", "enum": ["docked", "free", "fullscreen"]},
         "confirm": {"type": "boolean"}},
         "required": ["mode", "confirm"], "additionalProperties": False}},
]
