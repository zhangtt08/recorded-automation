"""Durable append-only run journal.

Events carry identifiers, phase names, error codes and — for terminal phases only — a
redacted one-line reason. Page input values and secret material are scrubbed upstream
(``Runner.scrub``) before they can ever reach this file.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .core import RunEvent

TERMINAL = {"completed", "completed_unverified", "failed", "uncertain", "cancelled"}

# 历史列表只需要最近的运行；长日志（每步 2–4 条事件）整份读盘会拖慢每一次界面刷新。
TAIL_BYTES = 4_000_000


class FileJournal:
    """JSONL journal; action_started is on disk before the call returns."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, event: RunEvent) -> None:
        record = {
            "at": round(time.time(), 3),
            "run_id": event.run_id,
            "workflow_id": event.workflow_id,
            "step_id": event.step_id,
            "phase": event.phase,
            "code": event.code,
        }
        # 失败原因跟着终态阶段一起落盘：程序重启之后，历史运行仍然答得出「为什么停在这一步」。
        # 内容是错误码 + 定位器/超时这类可诊断信息，不含输入值与秘密值（脱敏在 session 侧做）。
        reason = str(getattr(event, "reason", "") or "")
        if reason:
            record["reason"] = reason[:2000]
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def _recent_lines(self, tail_only: bool) -> list[str]:
        """Read the file, or only its last TAIL_BYTES when a bounded view is enough."""
        if not self.path.exists():
            return []
        with self._lock, open(self.path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if tail_only and size > TAIL_BYTES:
                handle.seek(size - TAIL_BYTES)
                handle.readline()          # 丢掉被切断的半行
            raw = handle.read().decode("utf-8", errors="ignore")
        return raw.splitlines()

    @staticmethod
    def _parse(line: str) -> dict | None:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return None
        return item if isinstance(item, dict) else None

    def read_run(self, run_id: str) -> list[dict]:
        events = []
        for raw in self._recent_lines(tail_only=False):
            item = self._parse(raw)
            if item and item.get("run_id") == run_id:
                events.append(item)
        return events

    def list_runs(self, limit: int = 50) -> list[dict]:
        """Runs in journal order, each summarised for the UI history list.

        只看日志尾部（TAIL_BYTES），因此最早那几次运行的记录可能被截断；
        界面上的历史列表本来就是最近 N 次，完整证据仍可由 read_run / 导出取到。
        """
        order: list[str] = []
        summary: dict[str, dict] = {}
        for raw in self._recent_lines(tail_only=True):
            item = self._parse(raw)
            if not item:
                continue
            run_id = item.get("run_id")
            if not run_id:
                continue
            if run_id not in summary:
                order.append(run_id)
                summary[run_id] = {
                    "run_id": run_id,
                    "workflow_id": item.get("workflow_id", ""),
                    "steps": 0,
                    "status": "",
                    "step_id": "",
                    "code": "",
                    "started_at": item.get("at", 0),
                    "ended_at": item.get("at", 0),
                }
            row = summary[run_id]
            if item.get("phase") == "step_started":
                row["steps"] += 1
            row["ended_at"] = max(row["ended_at"], item.get("at", 0) or 0)
            if item.get("phase") in TERMINAL:
                row["status"] = item["phase"]
                row["step_id"] = item.get("step_id", "")
                row["code"] = item.get("code", "")
        runs = []
        for run_id in order[-limit:]:
            row = summary[run_id]
            row["duration_s"] = round(max(0.0, row["ended_at"] - row["started_at"]), 1)
            runs.append(row)
        return runs[::-1]
