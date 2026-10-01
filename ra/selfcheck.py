"""端到端自检：用本机站点走一遍录制 → 审阅 → 保存 → 回放，返回可显示的结果。

自检在独立的数据目录与无界面浏览器里运行，不会动到你保存的工作流。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .fixture import FixtureSite

SECRET = "selfcheck-secret-value"
PLAIN = "selfcheck@example.test"
DONE_TEXT = "留言已收到，感谢反馈。"


def _wait(session, run_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = session.run_status(run_id)
        if status.get("status") != "running":
            return status
        time.sleep(0.2)
    return {"status": "timeout"}


def run(root: Path) -> dict:
    from .main import build

    results: list[dict] = []

    def add(case: str, ok: object, detail: object = "") -> None:
        results.append({"case": case, "ok": bool(ok), "detail": str(detail)[:200]})

    site = FixtureSite().start()
    api, session, _ = build(root)
    api.settings.update({"headless": True, "keep_browser_open": True})
    try:
        session.start_recording(site.url)

        def user(page):
            page.locator("#open-form").click()
            page.wait_for_selector("#contact:not([hidden])")
            page.locator("#email").fill(PLAIN)
            page.locator("#pwd").fill(SECRET)
            page.locator("#msg").press_sequentially("自检留言", delay=25)
            page.locator("#send").click()
            page.wait_for_selector("#done:not([hidden])")

        session.page_action(user)
        time.sleep(1.2)
        draft = session.stop_recording()
        steps = draft["steps"]
        kinds = "/".join(step["kind"] for step in steps)
        add("录到候选事件并归并连续输入", len(steps) >= 4, f"{len(steps)} 步：{kinds}")
        add("同一输入的多次事件合并为一次", any(step.get("merged", 0) >= 1 for step in steps)
            or kinds.count("fill") <= 3, kinds)
        secrets = [step for step in steps if step.get("sensitive")]
        leaked = SECRET in json.dumps(steps, ensure_ascii=False)
        add("密码只保存引用，不写入步骤", bool(secrets) and not leaked,
                            (secrets[0]["secret_ref"] if secrets else "没识别到敏感输入"))

        last_id = steps[-1]["id"]
        session.edit_draft(last_id, {"expected": {"locators": [{"strategy": "text", "value": DONE_TEXT}]}})
        built = session.build_draft("wf_selfcheck", "自检流程")
        add("Schema 与执行语义校验通过", not built["problems"], built["problems"][:1] or "无阻塞项")
        payload = built["workflow"]
        for step in secrets:
            session.secrets.set(step["secret_ref"], SECRET)
        saved = api.save_workflow(payload)
        add("工作流写入本机", "error" not in saved, saved.get("error") or payload["id"])

        started = api.run_workflow("wf_selfcheck")
        finished = _wait(session, started["run_id"])
        add("回放按顺序执行到结束", finished.get("status") in {"completed", "completed_unverified"},
            finished.get("status"))
        visible = False
        try:
            visible = bool(session.page_action(lambda page: page.locator("#done").is_visible()))
        except Exception as exc:
            visible = exc
        add("页面被自动化操作（提交成功提示出现）", visible is True, "成功提示可见" if visible is True else "未见成功提示")

        events = session.journal.read_run(started["run_id"])
        phases = [event["phase"] for event in events]
        action_steps = len([step for step in payload["steps"] if step["action"] != "wait_for"])
        add("每个动作只发出一次", phases.count("action_started") == phases.count("action_returned") == action_steps,
            f"action_started {phases.count('action_started')} / 步骤 {action_steps}")
        journal_text = (Path(root) / "journal.jsonl").read_text(encoding="utf-8")
        add("日志不含输入内容与秘密值", SECRET not in journal_text and PLAIN not in journal_text,
            "只记录运行/步骤 ID、阶段与错误码")

        session.draft = None
        session.state = "idle"
        session.start_recording(site.url)

        def wander(page):
            page.goto(site.foreign_origin + "/", wait_until="domcontentloaded")
            page.locator("h1").click()

        session.page_action(wander)
        time.sleep(0.8)
        snap = session.recording_snapshot()
        add("其他来源的操作不进入工作流", snap["count"] == 0 and snap["discarded"] >= 1,
            f"候选 {snap['count']}，丢弃 {snap['discarded']}")
        session.stop_recording()
        session.draft = None
        session.state = "idle"

        ambiguous = {
            "schema_version": 1, "id": "wf_ambiguous", "name": "多匹配检查",
            "origin": site.origin, "start_url": site.url,
            "steps": [{"id": "s1", "action": "click", "timeout_s": 1.0,
                       "target": {"page": "main", "locators": [{"strategy": "css", "value": "button.copy"}]}}],
        }
        session.start_run(ambiguous, "ambself")
        blocked = _wait(session, "ambself")
        add("匹配多个元素时在动作前停止", blocked.get("status") == "failed" and blocked.get("code") == "AmbiguousTarget",
            f"{blocked.get('status')} / {blocked.get('code')}")

        missing = {
            "schema_version": 1, "id": "wf_missing", "name": "缺失目标检查",
            "origin": site.origin, "start_url": site.url,
            "steps": [{"id": "s1", "action": "click", "timeout_s": 1.0,
                       "target": {"page": "main", "locators": [{"strategy": "text", "value": "页面里没有这段文字"}]}}],
        }
        session.start_run(missing, "missself")
        notfound = _wait(session, "missself")
        add("目标不存在时不点击别处", notfound.get("status") == "failed" and notfound.get("code") == "TargetTimeout",
            f"{notfound.get('status')} / {notfound.get('code')}")
    except Exception as exc:  # 自检本身失败也要报告出来
        add("自检执行未中断", False, f"{type(exc).__name__}: {str(exc)[:160]}")
    finally:
        try:
            session.shutdown()
        except Exception:
            pass
        site.stop()

    passed = sum(1 for item in results if item["ok"])
    return {"results": results, "passed": passed, "failed": len(results) - passed, "data_dir": str(root)}
