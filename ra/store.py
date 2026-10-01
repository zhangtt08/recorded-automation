"""工作流存储、校验与本地设置。"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

import jsonschema

from .core import workflow_from_dict

TOKEN_KEYS = re.compile(r"(token|access_key|apikey|api_key|secret|signature|sig|auth|password|passwd|session)", re.I)
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

DEFAULT_SETTINGS = {
    "default_timeout_s": 10.0,
    "headless": False,
    "proxy_server": "",
    "keep_browser_open": True,
}


class JsonFile:
    """Small durable JSON helper (atomic replace, UTF-8)."""

    def __init__(self, path: Path, default):
        self.path = Path(path)
        self.default = default
        self.lock = threading.Lock()

    def read(self):
        if not self.path.exists():
            return json.loads(json.dumps(self.default))
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError):
            return json.loads(json.dumps(self.default))

    def write(self, payload) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)


class Settings:
    def __init__(self, path: Path) -> None:
        self._file = JsonFile(path, DEFAULT_SETTINGS)

    def all(self) -> dict:
        payload = dict(DEFAULT_SETTINGS)
        stored = self._file.read()
        if isinstance(stored, dict):
            payload.update({key: value for key, value in stored.items() if key in DEFAULT_SETTINGS})
        return payload

    def update(self, patch: dict) -> dict:
        current = self.all()
        for key, value in (patch or {}).items():
            if key in DEFAULT_SETTINGS:
                current[key] = value
        self._file.write(current)
        return current


class WorkflowStore:
    """Workflow v1 files on disk, validated by JSON Schema plus execution semantics."""

    def __init__(self, root: Path, schema_path: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with open(schema_path, encoding="utf-8") as handle:
            self.schema = json.load(handle)
        self.validator = jsonschema.Draft202012Validator(self.schema)

    # -- validation ------------------------------------------------------
    def schema_problems(self, payload: dict) -> list[str]:
        problems = []
        for error in sorted(self.validator.iter_errors(payload), key=lambda item: list(item.path)):
            where = "/".join(str(part) for part in error.path) or "workflow"
            problems.append(f"{where}: {error.message}")
        return problems

    def semantic_problems(self, payload: dict) -> list[str]:
        problems: list[str] = []
        try:
            workflow = workflow_from_dict(payload)
        except (ValueError, KeyError, TypeError) as exc:
            return [f"执行语义：{exc}"]
        for step in workflow.steps:
            if step.action.value == "click" and step.expected is None:
                problems.append(f"步骤 {step.id} 没有完成条件，运行结果将记为「完成 · 未验证」")
        query = workflow.start_url.split("?", 1)[1] if "?" in workflow.start_url else ""
        for pair in query.split("&"):
            key = pair.split("=", 1)[0]
            if key and TOKEN_KEYS.search(key):
                problems.append(f"start_url 含疑似令牌参数 {key}，请改为运行时参数后再保存")
        return problems

    def problems(self, payload: dict) -> dict:
        schema = self.schema_problems(payload)
        semantic = [] if schema else self.semantic_problems(payload)
        blocking = list(schema) + [item for item in semantic if "完成条件" not in item]
        return {"schema_ok": not schema, "blocking": blocking,
                "warnings": [item for item in semantic if "完成条件" in item]}

    def save(self, payload: dict) -> dict:
        clean = dict(payload or {})
        clean["steps"] = [{key: value for key, value in step.items() if key != "label"}
                          for step in (payload.get("steps") or []) if isinstance(step, dict)]
        report = self.problems(clean)
        if report["blocking"]:
            raise ValueError("；".join(report["blocking"][:4]))
        workflow = workflow_from_dict(clean)
        if not ID_RE.match(workflow.id):
            raise ValueError("工作流 ID 只能包含字母、数字、下划线和短横线")
        self._file(workflow.id).write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def load(self, workflow_id: str) -> dict:
        path = self.root / f"{workflow_id}.workflow.json"
        if not path.exists():
            raise KeyError(workflow_id)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def delete(self, workflow_id: str) -> None:
        path = self.root / f"{workflow_id}.workflow.json"
        if path.exists():
            path.unlink()

    def list(self) -> list[dict]:
        rows = []
        for path in sorted(self.root.glob("*.workflow.json")):
            try:
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
                workflow = workflow_from_dict(payload)
            except Exception as exc:
                rows.append({"id": path.stem, "name": f"{path.stem}（文件损坏）", "broken": str(exc), "steps": 0,
                             "origin": "", "start_url": "", "actions": [], "secret_refs": [], "unverified": 0,
                             "file": str(path), "updated_at": path.stat().st_mtime})
                continue
            rows.append({
                "id": workflow.id,
                "name": workflow.name or workflow.id,
                "origin": workflow.origin,
                "start_url": workflow.start_url,
                "steps": len(workflow.steps),
                "actions": sorted({step.action.value for step in workflow.steps}),
                "secret_refs": sorted({step.secret_ref for step in workflow.steps if step.secret_ref}),
                "unverified": sum(1 for step in workflow.steps if step.action.value != "wait_for" and step.expected is None),
                "file": str(path),
                "updated_at": path.stat().st_mtime,
                "broken": "",
            })
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        return rows

    def _file(self, workflow_id: str) -> Path:
        return self.root / f"{workflow_id}.workflow.json"
