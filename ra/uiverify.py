"""窗口内自检：用真实的应用窗口 + JS 桥走一遍界面流程，并留下画面证据。

`python -m ra.main --verify [url]` 会在同一个窗口里检查 DOM 结构、界面导航、确认框，
以及录制 → 审阅的桥接调用；截图写到数据目录的 ui-shots/ 下便于核对界面。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

CHECK_DOM = """
() => {
  const views = Array.from(document.querySelectorAll('.view')).map((node) => node.id);
  const nav = Array.from(document.querySelectorAll('.nav-item[data-view]')).map((node) => node.textContent.trim().replace(/\\s+/g, ' '));
  const visible = Array.from(document.querySelectorAll('.view')).filter((node) => !node.hidden).map((node) => node.id);
  return JSON.stringify({
    brand: (document.querySelector('.brand h1') || {}).textContent,
    views: views,
    nav: nav,
    visible: visible,
    emptyState: (document.querySelector('#wf-list') || {}).textContent,
    rows: document.querySelectorAll('#wf-list .wf-row').length,
    stats: document.querySelectorAll('#deck-stats .stat').length,
    runRows: document.querySelectorAll('#recent-runs .rrow').length,
    recentEmpty: (document.getElementById('recent-runs') || {}).textContent,
    safety: (document.querySelector('#view-deck .safety') || {}).textContent.replace(/\\s+/g, ' ').trim(),
    errors: window.__raErrors || []
  });
}
"""

NAV_DOM = """
() => {
  const shown = [];
  const click = (name) => {
    const item = document.querySelector('.nav-item[data-view="' + name + '"]');
    if (item) item.click();
    shown.push(document.querySelectorAll('.view:not([hidden])').length);
  };
  ['secrets', 'history', 'settings', 'deck'].forEach(click);
  return JSON.stringify({
    singleVisible: shown.every((count) => count === 1),
    finalView: Array.from(document.querySelectorAll('.view')).filter((node) => !node.hidden).map((node) => node.id),
    settingsRows: document.querySelectorAll('#view-settings .set-row').length,
    secretsText: (document.getElementById('secret-table') || {}).textContent,
    engineNote: (document.getElementById('engine-note') || {}).textContent
  });
}
"""

MODAL = """
() => {
  document.querySelector('.nav-item[data-view="settings"]').click();
  document.getElementById('set-profile').click();
  const opened = !document.getElementById('modal').hidden;
  const title = (document.getElementById('modal-title') || {}).textContent;
  document.getElementById('modal-cancel').click();
  const closed = document.getElementById('modal').hidden;
  document.querySelector('.nav-item[data-view="deck"]').click();
  return JSON.stringify({opened: opened, title: title, closed: closed});
}
"""

RECORD_START = """
() => {
  window.__verify = {done: false, steps: []};
  const push = (name, value) => window.__verify.steps.push([name, value]);
  const api = window.pywebview.api;
  (async () => {
    try {
      const info = await api.app_info();
      push('app_info', !info.error && typeof info.version === 'string');
      document.getElementById('record-url').value = window.__verifyUrl;
      document.getElementById('record-start').click();
      let recording = false;
      for (let attempt = 0; attempt < 40; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        const current = await api.state();
        if (current.state === 'recording') { recording = true; break; }
      }
      push('recording-starts', recording);
      const clockBefore = (document.getElementById('state-clock') || {}).textContent || '';
      await new Promise((resolve) => setTimeout(resolve, 2600));
      const clockAfter = (document.getElementById('state-clock') || {}).textContent || '';
      push('sidebar-clock-ticks', clockBefore + '|' + clockAfter);
      push('titlebar-status-pill', !!document.querySelector('#tb-status .pill.rec'));
      const shot = await api.preview();
      push('preview-image', typeof shot.png === 'string' && shot.png.length > 500);
      const caps = await api.recording();
      push('recording-panel', typeof caps.count === 'number' && typeof caps.elapsed === 'number');
      window.__verify.done = true;
    } catch (error) {
      window.__verify = {done: true, error: String((error && error.message) || error)};
    }
  })();
  return 'pending';
}
"""

RECORD_STOP = """
() => {
  window.__verify2 = {done: false, steps: []};
  const api = window.pywebview.api;
  (async () => {
    try {
      document.getElementById('rec-stop').click();
      let reviewed = false;
      for (let attempt = 0; attempt < 30; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 300));
        if (!document.getElementById('view-review').hidden) { reviewed = true; break; }
      }
      const stepCards = document.querySelectorAll('#step-list .step').length;
      const checks = document.querySelectorAll('#check-list .ck').length;
      const jsonLines = document.querySelectorAll('#json-body .ln').length;
      const after = await api.state();
      const expected = window.__verifyExpectSteps;
      await new Promise((resolve) => setTimeout(resolve, 1400));
      const clock = (document.getElementById('state-clock') || {}).textContent || '';
      window.__verify2.steps.push(['stop-button-opens-review', reviewed]);
      window.__verify2.steps.push(['review-shows-steps', !expected || stepCards > 0]);
      window.__verify2.steps.push(['review-checks-and-json', checks >= 3 && jsonLines > 3]);
      window.__verify2.steps.push(['state-after-stop', after.state === 'review']);
      window.__verify2.steps.push(['clock-cleared-after-stop', clock === '']);
      window.__verify2.done = true;
    } catch (error) {
      window.__verify2 = {done: true, error: String((error && error.message) || error)};
    }
  })();
  return 'pending';
}
"""


FRAME = """
() => {
  const bar = document.getElementById('titlebar');
  const style = bar ? getComputedStyle(bar) : null;
  return JSON.stringify({
    visible: !!style && style.display !== 'none',
    frameless: !document.body.classList.contains('not-frameless'),
    buttons: document.querySelectorAll('.wbtn').length,
    viewName: (document.getElementById('tb-view') || {}).textContent
  });
}
"""

FULL_CYCLE = """
() => {
  window.__verify3 = {done: false, steps: []};
  const push = (name, value) => window.__verify3.steps.push([name, value]);
  const api = window.pywebview.api;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const setValue = (id, value) => {
    const box = document.getElementById(id);
    if (!box) return false;
    box.value = value;
    box.dispatchEvent(new Event('input', {bubbles: true}));
    return true;
  };
  (async () => {
    try {
      push('meta-fields-editable', setValue('review-name', '界面自检流程') && setValue('review-id', 'wf_uicheck'));
      await sleep(1000);
      const save = document.getElementById('rev-save');
      push('save-button-enabled', !!save && !save.disabled);
      if (save && !save.disabled) save.click();
      let listed = false;
      for (let attempt = 0; attempt < 30; attempt++) {
        await sleep(300);
        if (document.querySelector('#wf-list [data-run="wf_uicheck"]')) { listed = true; break; }
      }
      push('workflow-saved-and-listed', listed);
      if (!listed) { window.__verify3.done = true; return; }
      document.querySelector('#wf-list [data-run="wf_uicheck"]').click();
      let status = '';
      for (let attempt = 0; attempt < 120; attempt++) {
        await sleep(500);
        const current = await api.run_status(document.querySelector('#run-title .mono').textContent.trim());
        status = current.run.status;
        if (status !== 'running' && status !== 'unknown') break;
      }
      push('run-finished', ['completed', 'completed_unverified'].includes(status));
      push('run-timeline-rendered', document.querySelectorAll('#timeline .tstep').length > 0);
      push('run-journal-rendered', document.querySelectorAll('#j-body .j-row').length > 0);
      window.__verify3.paused = true;              // 等 Python 截屏后再继续
      for (let attempt = 0; attempt < 200 && !window.__verify3.go; attempt++) await sleep(150);
      document.querySelector('.run-head .back').click();
      await sleep(400);
      document.querySelector('#wf-list [data-del="wf_uicheck"]').click();
      await sleep(300);
      document.getElementById('modal-ok').click();
      let gone = false;
      for (let attempt = 0; attempt < 20; attempt++) {
        await sleep(300);
        if (!document.querySelector('#wf-list [data-run="wf_uicheck"]')) { gone = true; break; }
      }
      push('workflow-deleted-via-ui', gone);
      await sleep(500);
      const historyRow = document.querySelector('#recent-runs .rrow');
      if (historyRow) historyRow.click();
      await sleep(1200);
      push('history-row-opens-run', !document.getElementById('view-run').hidden);
      document.querySelector('.run-head .back').click();
      window.__verify3.done = true;
    } catch (error) {
      window.__verify3 = {done: true, error: String((error && error.message) || error)};
    }
  })();
  return 'pending';
}
"""


KEYS = """
() => {
  const fire = (key) => document.dispatchEvent(new KeyboardEvent('keydown', {key: key, altKey: true, bubbles: true}));
  fire('2');
  const history = !document.getElementById('view-history').hidden;
  fire('4');
  const settings = !document.getElementById('view-settings').hidden;
  fire('1');
  const deck = !document.getElementById('view-deck').hidden;
  return JSON.stringify({history: history, settings: settings, deck: deck});
}
"""


def run(window, target_url: str, shots_dir: Path | None = None, session=None) -> int:
    """Drive the real UI through the same bridge the user's clicks use."""
    deadline = time.time() + 15
    while time.time() < deadline:
        if window.evaluate_value("() => !!(window.pywebview && window.pywebview.api)"):
            break
        time.sleep(0.3)
    else:
        print("UI 自检失败：应用窗口与后端的桥接未在 15 秒内就绪")
        return 1

    problems: list[str] = []
    shots: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(("OK   " if ok else "FAIL ") + label + (f" | {detail}" if detail else ""))
        if not ok:
            problems.append(label)

    def shot(name: str) -> None:
        if shots_dir is None:
            return
        shots_dir.mkdir(parents=True, exist_ok=True)
        target = shots_dir / f"ui-{name}.png"
        if window.screenshot(target):
            shots.append(str(target))

    def await_flag(expression: str, timeout: float = 90.0) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            try:
                result = json.loads(window.evaluate_value(expression) or "{}")
            except json.JSONDecodeError:
                result = {}
            if result.get("done") or result.get("error"):
                return result
            time.sleep(0.4)
        return {"error": "超时未返回"}

    structure: dict = {}
    end = time.time() + 12
    while time.time() < end:
        structure = json.loads(window.evaluate_value(CHECK_DOM) or "{}")
        if (structure.get("rows") or 0) > 0 or "还没有工作流" in (structure.get("emptyState") or ""):
            break
        time.sleep(0.5)

    check("窗口标题为「录放台」", structure.get("brand") == "录放台", str(structure.get("brand")))
    check("侧栏四个工作区入口", len(structure.get("nav") or []) == 4, " / ".join(structure.get("nav") or []))
    check("全部界面在同一个窗口内", len(structure.get("views") or []) == 7, ", ".join(structure.get("views") or []))
    check("启动时显示工作台", structure.get("visible") == ["view-deck"], str(structure.get("visible")))
    check("工作台展示真实数据", bool(structure.get("rows")) or "还没有工作流" in (structure.get("emptyState") or ""),
          f"{structure.get('rows')} 行")
    check("工作台统计条就位", (structure.get("stats") or 0) == 4, f"{structure.get('stats')} 张卡片")
    check("运行历史区有内容", bool(structure.get("runRows")) or "还没有运行记录" in (structure.get("recentEmpty") or ""),
          f"{structure.get('runRows')} 条")
    check("底部安全说明存在", "不保存密码明文" in (structure.get("safety") or ""))
    check("页面脚本无未捕获错误", not structure.get("errors"), str(structure.get("errors"))[:150])
    shot("deck")

    navigation = json.loads(window.evaluate_value(NAV_DOM) or "{}")
    check("切换界面时始终只有一个可见视图", navigation.get("singleVisible") is True, str(navigation.get("singleVisible")))
    check("切回工作台", navigation.get("finalView") == ["view-deck"], str(navigation.get("finalView")))
    check("设置页有可操作项", (navigation.get("settingsRows") or 0) >= 7, str(navigation.get("settingsRows")))
    check("秘密库界面有内容或空状态", bool(navigation.get("secretsText")), str(navigation.get("secretsText"))[:40])
    check("侧栏显示运行方式与引擎", bool(navigation.get("engineNote")) and navigation.get("engineNote") != "—",
          str(navigation.get("engineNote")))

    modal = json.loads(window.evaluate_value(MODAL) or "{}")
    check("危险操作有应用内确认框", modal.get("opened") is True, str(modal.get("title"))[:36])
    check("确认框可取消", modal.get("closed") is True)

    keys = json.loads(window.evaluate_value(KEYS) or "{}")
    check("Alt 数字键切换界面", keys.get("history") and keys.get("settings") and keys.get("deck"), str(keys))

    def report_steps(steps: list) -> None:
        for name, value in steps or []:
            if name == "sidebar-clock-ticks":
                before, _, after = str(value).partition("|")
                check(f"界面调用后端：{name}", bool(before) and bool(after) and before != after, f"{before} → {after}")
            else:
                check(f"界面调用后端：{name}", bool(value))

    window.evaluate_js(f"() => {{ window.__verifyUrl = {json.dumps(target_url)}; }}")
    window.evaluate_js(RECORD_START)
    first = await_flag("() => JSON.stringify(window.__verify || {})")
    if first.get("error"):
        check("录制阶段", False, str(first["error"]))
    report_steps(first.get("steps"))

    # 在受控页面里做一次真实点击，让审阅界面有内容可渲染（用户给的地址没这个按钮时跳过断言）
    expect_steps = False
    if session is not None:
        try:
            session.page_action(lambda page: page.locator("#open-form").click())
            expect_steps = True
        except Exception:
            expect_steps = False
        time.sleep(1.2)
    shot("recording")

    window.evaluate_js(f"() => {{ window.__verifyExpectSteps = {'true' if expect_steps else 'false'}; }}")
    window.evaluate_js(RECORD_STOP)
    second = await_flag("() => JSON.stringify(window.__verify2 || {})", timeout=40)
    if second.get("error"):
        check("停止录制阶段", False, str(second["error"]))
    report_steps(second.get("steps"))
    time.sleep(1.0)
    shot("review")

    frame = json.loads(window.evaluate_value(FRAME) or "{}")
    check("窗口去掉系统边框并自绘标题条", frame.get("frameless") is True and frame.get("visible") is True, str(frame))
    check("标题条含三个窗口控制按钮", frame.get("buttons") == 3, str(frame.get("buttons")))
    check("标题条跟随当前界面名", frame.get("viewName") == "审阅与编辑", str(frame.get("viewName")))

    window.evaluate_js(FULL_CYCLE)
    end = time.time() + 180
    gate: dict = {}
    while time.time() < end:
        gate = json.loads(window.evaluate_value(
            "() => JSON.stringify({paused: !!(window.__verify3||{}).paused,"
            " done: !!(window.__verify3||{}).done, error: (window.__verify3||{}).error || ''})") or "{}")
        if gate.get("paused") or gate.get("done") or gate.get("error"):
            break
        time.sleep(0.5)
    shot("run")
    window.evaluate_js("() => { if (window.__verify3) window.__verify3.go = true; }")
    third = await_flag("() => JSON.stringify(window.__verify3 || {})", timeout=90)
    if third.get("error"):
        check("界面内完整流程", False, str(third["error"]))
    for name, ok in third.get("steps") or []:
        check(f"界面内完整流程：{name}", bool(ok))

    final_errors = json.loads(window.evaluate_value("() => JSON.stringify(window.__raErrors || [])") or "[]")
    check("整轮操作后页面脚本仍无错误", not final_errors, str(final_errors)[:150])

    window.evaluate_js("() => { const b = document.getElementById('win-close'); if (b) b.click(); return 'ok'; }")
    end = time.time() + 15
    while time.time() < end and not window.stopped():
        time.sleep(0.3)
    check("标题条关闭按钮能退出整个应用", window.stopped())

    print("\nUI 自检结论：" + ("全部通过" if not problems else f"{len(problems)} 项未通过 -> " + "；".join(problems)))
    if shots:
        print("界面截图：" + "，".join(shots))
    return 0 if not problems else 1
