"""可诊断日志：每次运行的真实原因，脱敏之后单独落盘。

`journal.jsonl` 的契约是「只含标识、阶段与错误码」——它是运行事实的账本，不该被
几十行的 Playwright 调用记录污染。但只在内存里的 `record["error"]` 又留不住：程序一重启，
历史运行就只剩一个 `TargetTimeout` 码，人想知道为什么只能重跑一次（对企业客户来说这就是
「报错但不说为什么」）。所以这里补第三份产物：

* 只在失败 / 未确认 / 取消时写，成功运行不产生噪声；
* 带真实原因（含 Playwright 的调用日志），但秘密值与普通输入值先被换掉；
* 按 run_id 能读回来，运行页与 Agent 都能引用它；
* 写失败绝不影响运行结果本身。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

MAX_REASON = 1500
MAX_TAIL = 600
TAIL_BYTES = 1_000_000
# 万一上游漏了某种写法，这里再挡一道：键名一看就是凭据的，值一律换掉。
KEY_VALUE = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|"
                       r"authorization|cookie|credential|session[_-]?id)\b\"?\'?\s*[:=]\s*\"?[^\"',}\s]+")
TERMINAL = ("failed", "uncertain", "cancelled")


def redact(text: object) -> str:
    """压成一行、限长、并把凭据样式的键值换掉。空值原样返回空串。"""
    value = str(text or "")
    if not value:
        return ""
    return KEY_VALUE.sub(r"\1=***", re.sub(r"\s+", " ", value).strip())[:MAX_REASON]


class RunDiagnostics:
    """JSONL of *why* a run stopped, keyed by run_id. Never written for successful runs."""

    def __init__(self, path: Path, limit_records: int = 400) -> None:
        self.path = Path(path)
        self.limit = max(20, int(limit_records))
        self._lock = threading.Lock()

    # -- writing ---------------------------------------------------------
    def launch(self, run_id: str, workflow_id: str, workflow_name: str, total_steps: int,
               resumed_from: str = "") -> None:
        """起跑时写一条 started：没有它就分不清「取消了一次运行」和「压根没跑起来」。"""
        self._write({"at": round(time.time(), 3), "run_id": str(run_id), "status": "started",
                     "workflow_id": str(workflow_id or ""), "workflow_name": str(workflow_name or ""),
                     "steps_total": int(total_steps or 0), "resumed_from": str(resumed_from or "")})

    def record(self, *, run_id: str, workflow_id: str = "", workflow_name: str = "", status: str = "",
               step_id: str = "", index: int = 0, total: int = 0, action: str = "", label: str = "",
               code: str = "", reason: str = "", error: str = "", events=None,
               secret_refs=None, secret_values_used: int = 0) -> None:
        if status not in TERMINAL:
            return
        phases = [str(phase) for phase in (events or [])]
        self._write({
            "at": round(time.time(), 3), "run_id": str(run_id), "status": str(status),
            "workflow_id": str(workflow_id or ""), "workflow_name": str(workflow_name or ""),
            "step_id": str(step_id or ""), "step_index": int(index or 0), "steps_total": int(total or 0),
            "action": str(action or ""), "label": redact(label),
            "code": str(code or ""), "reason": redact(reason), "error": redact(error),
            "phases": phases[-MAX_TAIL:], "steps_reached": phases.count("step_started"),
            "actions_fired": phases.count("action_started"),
            "secret_refs": [str(ref) for ref in (secret_refs or [])],
            "secret_values_used": int(secret_values_used or 0),
        })

    def _write(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._trim()
            except OSError:
                # 诊断写不进去也不能把运行带跑；至少界面上还能看到原因。
                pass

    def _trim(self) -> None:
        """只留最近 limit 条：这台机器上企业用户可能一天跑几百次，日志不能无上限长。"""
        try:
            with open(self.path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return
        if len(lines) <= self.limit:
            return
        keep = [line for line in lines if line.strip()][-self.limit:]
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(keep) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    # -- reading ---------------------------------------------------------
    def read_run(self, run_id: str) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock:
            try:
                with open(self.path, "rb") as handle:
                    size = os.fstat(handle.fileno()).st_size
                    if size > TAIL_BYTES:
                        handle.seek(size - TAIL_BYTES)
                        handle.readline()
                    raw = handle.read().decode("utf-8", errors="ignore")
            except OSError:
                return []
        rows = []
        for line in raw.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and str(item.get("run_id")) == str(run_id):
                rows.append(item)
        return rows

    def message_for(self, run_id: str) -> str:
        """把这条运行的诊断写成人话；没有诊断记录就如实说没有。"""
        rows = [row for row in self.read_run(run_id) if row.get("status") != "started"]
        if not rows:
            return ""
        row = rows[-1]
        head = f"第 {row['step_index']} / {row['steps_total']} 步" if row.get("step_index") else "还没有进步骤"
        bits = [f"{head}：{row.get('label') or row.get('action') or ''}".strip("： ")]
        bits.append(f"原因：{row.get('reason') or row.get('error') or row.get('code') or '未记录'}")
        if row.get("actions_fired"):
            bits.append(f"已发出 {row['actions_fired']} 个动作（不会自动重试）")
        else:
            bits.append("没有发出任何动作")
        if row.get("secret_values_used"):
            bits.append(f"秘密值被取用 {row['secret_values_used']} 次（值不落盘）")
        if row.get("error") and row.get("reason") and row["error"] != row["reason"]:
            bits.append(f"原始信息：{row['error']}")
        return " · ".join(bit for bit in bits if bit)


class NullDiagnostics:
    """测试或非 Windows 环境下用：接口一样，什么都不写。"""

    def launch(self, *args, **kwargs) -> None:
        return None

    def record(self, *args, **kwargs) -> None:
        return None

    def read_run(self, run_id: str) -> list[dict]:
        return []

    def message_for(self, run_id: str) -> str:
        return ""
