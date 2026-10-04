"""Durable append-only run journal.

Events carry identifiers, phase names, error codes and — for terminal phases only — a
redacted one-line reason. Page input values and secret material are scrubbed upstream
(``Runner.scrub``) before they can ever reach this file.

有界，而且诚实：日志会轮转（一份 ``MAX_BYTES``、留 ``KEEP`` 份归档），被挤出去的最旧那份会没。
这件事不悄悄发生 —— 每次轮转都往新的一份里写一条 ``journal_rotated`` 标记（带这次归档了多少字节、
删掉了多少字节），``stats()`` 把它连同各份大小一起报出去，历史列表与 Agent 工具在同一次返回里带上同一句话。
以前只有 ``TAIL_BYTES`` 限制**读取**（``append`` 无上限地长），于是「读到的就是全部」这个印象是假的：
文件一直在涨、读取一直在丢头。现在上限装在文件本身上。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .core import RunEvent

TERMINAL = {"completed", "completed_unverified", "failed", "uncertain", "cancelled"}

#: 轮转标记的阶段名：它不带 run_id，因此不会在历史列表里冒充一次运行。
ROTATION_PHASE = "journal_rotated"
ROTATION_CODE = "journal_rotation"

#: 单份上限。2MB ≈ 每步 3–4 条事件的上千步，界面与工具面都用不到更长的历史。
MAX_BYTES = 2_000_000
#: 保留几份归档（journal.jsonl.1 … .KEEP）。被挤出的那份是真删除，所以 stats() 必须说出来。
KEEP = 2

# 读取上限留出余量，保证「一份之内读全」：有界性由轮转负责，而不是靠悄悄跳过文件头。
TAIL_BYTES = MAX_BYTES + 1_000_000


class FileJournal:
    """JSONL journal; action_started is on disk before the call returns."""

    def __init__(self, path: Path, max_bytes: int = MAX_BYTES, keep: int = KEEP) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self.keep = max(0, int(keep))
        self._lock = threading.Lock()

    # -- 文件布局 --------------------------------------------------------
    def rotated(self, index: int) -> Path:
        return self.path.with_name(self.path.name + f".{index}")

    def _files(self) -> list[Path]:
        """按时间顺序返回现存的日志文件：最旧的归档在前，正在写的那份在最后。"""
        files = [self.rotated(i) for i in range(self.keep, 0, -1)]
        files.append(self.path)
        return [item for item in files if item.exists()]

    @staticmethod
    def record_of(event: RunEvent) -> dict:
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
        return record

    def append(self, event: RunEvent) -> None:
        self._append_record(self.record_of(event))

    def _append_record(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.stat().st_size > self.max_bytes:
                self._rotate_locked()

    def _rotate_locked(self) -> None:
        """当前这份整份归档为 .1，旧的依次后移；被挤出的最旧一份删除并如实记账。

        留整份归档而不是就地截断：一次运行的事件不能被切成两半，而「切掉头部」会让
        read_run 对早期运行永远查无此事、还不留痕迹。
        """
        archived = self.path.stat().st_size
        dropped = 0
        dropped_name = ""
        if self.keep > 0:
            oldest = self.rotated(self.keep)
            if oldest.exists():
                dropped = oldest.stat().st_size
                dropped_name = oldest.name
                try:
                    oldest.unlink()
                except OSError:
                    pass
            for index in range(self.keep - 1, 0, -1):
                source = self.rotated(index)
                if source.exists():
                    os.replace(source, self.rotated(index + 1))
            os.replace(self.path, self.rotated(1))
        else:                       # keep=0：不留归档，整份直接丢掉（上限仍然成立，但更要知道代价）
            dropped = archived
            dropped_name = self.path.name
            try:
                self.path.unlink()
            except OSError:
                pass
        marker = {
            "at": round(time.time(), 3),
            "run_id": "", "workflow_id": "", "step_id": "",
            "phase": ROTATION_PHASE, "code": ROTATION_CODE,
            "archived_bytes": archived,
            "archived_to": self.rotated(1).name if self.keep else "",
            "dropped_bytes": dropped, "dropped_file": dropped_name,
            "keep": self.keep, "max_bytes": self.max_bytes,
        }
        marker["reason"] = self._rotation_note(marker)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(marker, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _rotation_note(record: dict) -> str:
        """评审要的那句"诚实"落在这里：删掉了多少字节必须说出来。"""
        if not record["archived_to"]:         # keep=0：这一份没有留档，整份就是丢的那部分
            return (f"日志超过 {record['max_bytes']} 字节，而这份不保留归档："
                    f"{record['dropped_bytes']} 字节（{record['dropped_file']}）已删除，那段历史不在本机了。")
        text = (f"日志超过 {record['max_bytes']} 字节，已把上一份 {record['archived_bytes']} 字节整份归档为 "
                f"{record['archived_to']}")
        if record.get("dropped_bytes"):
            text += (f"；最旧那份 {record['dropped_file']}（{record['dropped_bytes']} 字节）已删除，"
                     "那段历史不在本机了")
        return text + "。"

    # -- 读取 ------------------------------------------------------------
    def _recent_lines(self, tail_only: bool) -> list[str]:
        """现存几份日志的行，按时间顺序拼起来（旧归档在前）。"""
        lines: list[str] = []
        for item in self._files():
            with self._lock, open(item, "rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if tail_only and size > TAIL_BYTES:
                    handle.seek(size - TAIL_BYTES)
                    handle.readline()          # 丢掉被切断的半行
                raw = handle.read().decode("utf-8", errors="ignore")
            lines.extend(raw.splitlines())
        return lines

    def stats(self) -> dict:
        """这份日志现在多大、轮转过几次、被删掉过多少字节 —— 一句"日志很长"不算报告。"""
        files = []
        total = 0
        for item in self._files():
            try:
                size = item.stat().st_size
            except OSError:
                continue
            files.append({"name": item.name, "bytes": size})
            total += size
        rotations = 0
        dropped_bytes = 0
        last: dict = {}
        for raw in self._recent_lines(tail_only=True):
            item = self._parse(raw)
            if item and item.get("phase") == ROTATION_PHASE:
                rotations += 1
                dropped_bytes += int(item.get("dropped_bytes") or 0)
                last = item
        capacity = self.max_bytes * (self.keep + 1)
        return {
            "path": str(self.path),
            "bytes": total,
            "files": files,
            "max_bytes": self.max_bytes,
            "keep": self.keep,
            "rotations": rotations,
            "dropped_bytes": dropped_bytes,
            "trimmed": rotations > 0,
            "at_capacity": total >= capacity,
            "note": last.get("reason", "") if last else
                    "还没有轮转过：现在读到的是这台机器上全部的运行日志。",
        }

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

        读的是现存的全部几份（当前 + 归档），所以"最近 N 次"不会因为某次轮转而凭空少一截；
        被删掉的最旧那份不在其中 —— 那件事由 stats() 说出来，调用方把它跟列表一起展示。
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
