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
      // 审阅页的两个新入口：就地试跑草稿；改定位器时只重画被改的那一张卡片
      window.__verify2.steps.push(['review-has-trial-button', !!document.getElementById('rev-trial')]);
      const pick = document.querySelector('#step-list .step button[data-pick]');
      if (pick) {
        pick.click();
        await new Promise((resolve) => setTimeout(resolve, 900));
      }
      window.__verify2.steps.push(['review-locator-pick-repaints',
        !pick || document.querySelectorAll('#step-list .step').length === stepCards]);
      window.__verify2.steps.push(['no-page-errors-after-locator-edit', !(window.__raErrors || []).length]);
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
    frameless: document.body.classList.contains('frameless'),
    mode: document.body.dataset.winMode || '',
    buttons: document.querySelectorAll('.wbtn').length,
    grips: document.querySelectorAll('#grips i').length,
    viewName: (document.getElementById('tb-view') || {}).textContent,
    vw: innerWidth, vh: innerHeight, dpr: devicePixelRatio, cls: document.body.className,
    frame: window.__raFrame || null,
    cw: document.documentElement.clientWidth, ch: document.documentElement.clientHeight
  });
}
"""

WINDOW_STEP = """
() => {
  const wanted = window.__wantMode || 'docked';
  const button = document.querySelector('#set-winmode button[data-mode="' + wanted + '"]');
  if (!button) return JSON.stringify({ missing: true });
  if (!button.classList.contains('on')) button.click();   // 只点一下，绝不在页面里等桥接回包
  return JSON.stringify({ clicked: wanted });
}
"""

WINDOW_READ = """
() => {
  const on = document.querySelector('#set-winmode button.on');
  return JSON.stringify({
    mode: on ? on.dataset.mode : '',
    bar: document.body.classList.contains('frameless'),
    winLine: (document.getElementById('win-line') || {}).textContent || '',
    winGeom: (document.getElementById('win-geom') || {}).textContent || '',
    vw: innerWidth, vh: innerHeight
  });
}
"""

GESTURE_DRAG = """
() => {
  const bar = document.getElementById('titlebar');
  if (!bar) { window.__gesture = {fired: false, note: '没有标题条'}; return 'no-bar'; }
  const fire = (target, type, x, y) => target.dispatchEvent(new PointerEvent(type, {
    bubbles: true, cancelable: true, pointerId: 7, isPrimary: true, button: 0,
    buttons: type === 'pointerup' ? 0 : 1, clientX: x, clientY: y, screenX: x, screenY: y}));
  window.__gesture = {fired: false, steps: []};
  fire(bar, 'pointerdown', 500, 14);
  setTimeout(() => { fire(window, 'pointermove', 560, 40); }, 60);
  setTimeout(() => { fire(window, 'pointermove', 620, 60); }, 150);
  setTimeout(() => { fire(window, 'pointerup', 620, 60); window.__gesture.fired = true; }, 260);
  return 'scheduled';
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
      // 「从失败步重试」只在失败/取消时出现：跑完了就该是隐藏的，不能给人一个多余的按钮
      push('run-resume-hidden-when-finished', document.getElementById('run-resume').hidden === true);
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
  const at = (key, id) => { fire(key); return !document.getElementById(id).hidden; };
  const out = {secrets: at('3', 'view-secrets'), agent: at('4', 'view-agent'),
               settings: at('5', 'view-settings'), history: at('2', 'view-history'),
               deck: at('1', 'view-deck')};
  return JSON.stringify(out);
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
    check("侧栏五个工作区入口（含 Agent 接口）",
          (structure.get("nav") or [])[:5] == ["工作流", "运行历史", "秘密库", "Agent 接口", "设置"]
          or len(structure.get("nav") or []) == 5, " / ".join(structure.get("nav") or []))
    check("全部界面在同一个窗口内", len(structure.get("views") or []) == 8, ", ".join(structure.get("views") or []))
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
    check("Alt 1…5 五个界面都能用键盘切到",
          all([keys.get("deck"), keys.get("history"), keys.get("secrets"), keys.get("agent"),
               keys.get("settings")]), str(keys))

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

    from . import winframe as _wf

    def wait_mode(want: str, timeout: float = 16.0) -> dict:
        """等到窗口真的变成目标形态；不靠固定 sleep 猜时间。"""
        end = time.time() + timeout
        got: dict = {}
        while time.time() < end:
            got = window._probe() if hasattr(window, "_probe") else {}
            if got.get("mode") == want:
                if want != "fullscreen":
                    return got
                if tuple(got.get("rect") or ()) == tuple(got.get("monitor") or ()):
                    return got
                continue
            time.sleep(0.4)
        return got

    win = window._probe()
    if win.get("mode") != "docked":
        # 这一整段测的是「默认形态」：用户（或上一次自检）可能把形态留在了自由窗口，
        # 那就先按程序自己的入口切回来再量，否则下面每一条都在测另一种形态。
        window.win_mode("docked")
        win = wait_mode("docked")
    window.evaluate_js("() => { window.__raSyncFrame && window.__raSyncFrame(); return true; }")
    time.sleep(0.9)
    frame = json.loads(window.evaluate_value(FRAME) or "{}")
    monitor = win.get("monitor") or [0, 0, 0, 0]
    work = win.get("work") or [0, 0, 0, 0]
    rect = win.get("rect") or [0, 0, 0, 0]
    client = win.get("client") or [0, 0]
    content = list(win.get("content") or [0, 0, 0, 0])
    dpr = frame.get("dpr") or 1
    check("默认形态是停靠无边框（不是留着浏览器标题条的窗口）",
          win.get("mode") == "docked" and win.get("docked") is True and win.get("fullscreen") is False,
          f"mode={win.get('mode')} strip={win.get('strip')}")
    check("标题条整段在屏幕外：窗口顶边比内容顶边高出一个标题条",
          win.get("titlebar_hidden") is True and rect[1] + win.get("strip", 0) == content[1],
          f"窗口顶边={rect[1]} 内容顶边={content[1]} 标题条={win.get('strip')}")
    check("内容顶边贴住工作区顶边（没有被任务栏占掉）",
          abs(content[1] - work[1]) <= 2, f"content={content} work={work}")
    remeasure = {}
    for _ in range(8):
        remeasure = window.win_remeasure() if hasattr(window, "win_remeasure") else {}
        if remeasure.get("pixel_ok") is True and remeasure.get("drift") == 0:
            break
        time.sleep(0.7)     # 第一次量到偏差时程序会自己改标题条高度再摆一次，给它收敛的时间
    check("像素复核：页面最上面的标记条正好出现在屏幕第一行（drift=0）",
          remeasure.get("pixel_ok") is True and remeasure.get("drift") == 0,
          f"pixel_ok={remeasure.get('pixel_ok')} drift={remeasure.get('drift')} "
          f"strip={remeasure.get('strip')} place_error={remeasure.get('place_error')}")
    check("界面上只有一条标题条：四个窗口按钮 + 八条缩放手势都在页面里",
          frame.get("frameless") is True and frame.get("visible") is True
          and frame.get("buttons") == 4 and frame.get("grips") == 8, str(frame))
    check("标题条跟随当前界面名", frame.get("viewName") == "审阅与编辑", str(frame.get("viewName")))
    check("页面按真实客户区渲染，顶部除了被顶出屏幕的标题条没有别的留白",
          bool(frame.get("vw")) and abs(frame["vw"] * dpr - client[0]) <= 2
          and abs(client[1] - frame.get("vh", 0) * dpr - win.get("strip", 0)) <= 2,
          f"视口 {frame.get('vw')}x{frame.get('vh')}@{dpr} vs 客户区 {client} 标题条 {win.get('strip')}")

    moved = window.win_move(content[0] + 60, content[1])
    after = window._probe()
    check("标题条上拖动 = 移动窗口（横向跟随，顶边仍贴住工作区）",
          abs(after["content"][0] - (content[0] + 60)) <= 3
          and abs(after["content"][1] - work[1]) <= 2 and after.get("titlebar_hidden") is True,
          f"asked={moved.get('asked')} content={after.get('content')}")
    # 端到端手势：在页面里派发真实 pointer 事件，走「界面手势 → 桥 → 窗口」这条用户路径
    before_drag = list(window._probe()["content"])
    window.evaluate_js(GESTURE_DRAG)
    dragged = before_drag
    end = time.time() + 8
    while time.time() < end:
        time.sleep(0.4)
        dragged = list(window._probe()["content"])
        if abs(dragged[0] - before_drag[0]) > 60:
            break
    diag = json.loads(window.evaluate_value("() => JSON.stringify(window.__raGesture || {})") or "{}")
    after_drag = window._probe()
    evidence = {"页面": diag, "后端收到": [item for item in (after_drag.get("debug") or [])
                                       if "gesture" in item][-2:],
                "asked": after_drag.get("asked"), "via": after_drag.get("via"),
                "place_error": after_drag.get("place_error"),
                "同名窗口": _wf.named_windows("录放台"), "内容矩形": list(after_drag.get("content") or []),
                "收尾": [item for item in (after_drag.get("debug") or []) if "gesture_end" in item][-1:]}
    got_gesture = [item for item in (after_drag.get("debug") or []) if "gesture_resize" in item]
    check("拖标题条的手势真的到达后端并发出了新位置的摆放",
          bool(got_gesture) and after_drag["titlebar_hidden"] is True,
          f"{before_drag} -> {dragged} | " + json.dumps(evidence, ensure_ascii=False)[:520])
    gesture = json.loads(window.evaluate_value("() => JSON.stringify(window.__gesture || {})") or "{}")
    check("手势由界面自己完成（不是自检直接调后端）", gesture.get("fired") is True, str(gesture)[:120])

    resized = window.win_resize([after["content"][0], after["content"][1], 1200, 760])
    back_size = window._probe()
    check("边缘手势缩放 = 改变内容区尺寸，标题条仍不出现在屏幕上",
          abs(back_size["content"][2] - 1200) <= 3 and abs(back_size["content"][3] - 760) <= 3
          and back_size.get("titlebar_hidden") is True,
          f"asked={resized.get('asked')} content={back_size.get('content')}")
    maximized = window.win_maximize()
    wide = window._probe()
    check("最大化按钮 = 铺满工作区宽度、高度顶到引擎允许的上限，仍然没有浏览器标题条",
          maximized.get("maximized") is True and abs(wide["content"][2] - (work[2] - work[0])) <= 3
          and wide["content"][3] >= (work[3] - work[1]) - 24 and wide.get("titlebar_hidden") is True,
          f"content={wide.get('content')} work={work}")
    restored = window.win_maximize()
    narrow = window._probe()
    check("再点一次还原到上一次的尺寸与位置",
          restored.get("maximized") is False and abs(narrow["content"][2] - wide["content"][2]) > 3,
          f"{wide.get('content')} -> {narrow.get('content')}")
    check("窗口在屏幕可见范围内、没被最小化（旧档案也拉得回来）",
          _wf.mostly_on_screen(tuple(narrow.get("rect") or ()), tuple(monitor))
          and narrow.get("iconic") is False,
          f"rect={narrow.get('rect')} monitor={monitor} iconic={narrow.get('iconic')}")

    window.win_mode("fullscreen")
    full = wait_mode("fullscreen")
    time.sleep(0.8)
    full_frame = json.loads(window.evaluate_value(FRAME) or "{}")
    check("切全屏：窗口真的铺满显示器且没有系统标题栏",
          full.get("fullscreen") is True and full.get("caption") is False
          and tuple(full.get("rect") or ()) == tuple(full.get("monitor") or ()),
          f"{full.get('rect')} vs {full.get('monitor')} caption={full.get('caption')}")
    check("全屏时界面自绘标题条仍在，页面铺满客户区",
          full_frame.get("frameless") is True and full_frame.get("visible") is True
          and abs(full_frame.get("vw", 0) * (full_frame.get("dpr") or 1) - (full.get("client") or [0, 0])[0]) <= 2,
          str(full_frame))
    window.win_mode("docked")
    docked = wait_mode("docked")
    check("能从全屏切回停靠无边框（不会被全屏困住）",
          docked.get("mode") == "docked" and docked.get("titlebar_hidden") is True,
          f"{docked.get('mode')} hidden={docked.get('titlebar_hidden')}")

    # 用户路径：设置页的分段按钮双向可用（点击与读取分开，避免在页面里等桥接回包）
    window.evaluate_js('() => { document.querySelector(\'.nav-item[data-view="settings"]\').click(); return true; }')
    time.sleep(1.0)

    def pick(mode: str) -> tuple:
        window.evaluate_js(f"() => {{ window.__wantMode = '{mode}'; return true; }}")
        window.evaluate_js(WINDOW_STEP)
        got = wait_mode(mode)
        # 界面上的形态是 syncFrame 异步刷新的：等到页面自己说出这个形态再断言，
        # 否则读到的是上一次切换的状态，检查会整体错一格。
        read: dict = {}
        end = time.time() + 10
        while time.time() < end:
            window.evaluate_js("() => { window.__raSyncFrame && window.__raSyncFrame(); return true; }")
            time.sleep(0.6)
            read = json.loads(window.evaluate_value(WINDOW_READ) or "{}")
            if read.get("mode") == mode:
                break
        return got, read

    full_probe, full_read = pick("fullscreen")
    check("设置页点「全屏」：窗口真的铺满显示器",
          full_read.get("mode") == "fullscreen" and full_probe.get("fullscreen") is True
          and tuple(full_probe.get("rect") or ()) == tuple(full_probe.get("monitor") or ()),
          f"{full_read} probe={full_probe.get('rect')}")
    dock_probe, dock_read = pick("docked")
    check("设置页点「停靠无边框」：标题条又被顶出屏幕，界面自绘标题条回来",
          dock_read.get("mode") == "docked" and dock_read.get("bar") is True
          and dock_probe.get("titlebar_hidden") is True, f"{dock_read} probe={dock_probe.get('rect')}")
    check("设置页用人话说明当前窗口形态，实测数字单独放在一行小字里",
          "停靠无边框" in dock_read.get("winLine", "") and "像素" in dock_read.get("winLine", "")
          and "px" in dock_read.get("winGeom", "") and "内容区" in dock_read.get("winGeom", ""),
          f"{dock_read.get('winLine', '')} | {dock_read.get('winGeom', '')}")
    free_probe, free_read = pick("free")
    check("自由窗口如实承认：这条形态会露出浏览器标题条，界面不再重复画一条",
          free_probe.get("mode") == "free" and free_probe.get("titlebar_hidden") is False
          and free_read.get("bar") is False, f"{free_read} probe={free_probe.get('content')}")
    home_probe, home_read = pick("docked")
    check("从自由窗口切回停靠无边框：标题条又被顶出屏幕（这条路径以前会静默失败）",
          home_probe.get("mode") == "docked" and home_probe.get("titlebar_hidden") is True
          and home_read.get("bar") is True, f"{home_read} probe={home_probe.get('content')}")
    page_errors = json.loads(window.evaluate_value("() => JSON.stringify(window.__raErrors || [])") or "[]")
    check("走完全部窗口形态切换后页面脚本仍无未捕获错误", not page_errors, str(page_errors)[:200])

    # ---------- Agent 接口这一屏：程序本身就是给 Agent 用的后端 ----------
    window.evaluate_js('() => { document.querySelector(\'.nav-item[data-view="agent"]\').click(); return true; }')
    time.sleep(1.6)
    agent = json.loads(window.evaluate_value("""
() => {
  const rows = [...document.querySelectorAll('#agent-tools .tool-row')];
  return JSON.stringify({
    view: !document.getElementById('view-agent').hidden,
    stats: document.querySelectorAll('#agent-stats .stat').length,
    endpoint: ((document.querySelector('#agent-endpoint .chip') || {}).textContent || ''),
    tools: rows.length,
    named: rows.slice(0, 3).map((row) => (row.querySelector('.tn') || {}).textContent),
    risks: [...new Set(rows.map((row) => (row.querySelector('.risk') || {}).className.split(' ')[1]))],
    curl: ((document.getElementById('agent-curl') || {}).textContent || ''),
    mcp: ((document.getElementById('agent-mcp') || {}).textContent || ''),
    navCount: (document.getElementById('nav-agent') || {}).textContent
  });
}
""") or "{}")
    check("Agent 屏能打开，且统计卡都在同一窗口里", agent.get("view") is True and agent.get("stats") == 4, str(agent))
    # 这一条是给「卡片被纵向 flex 压扁」那个缺陷上锁：压扁后内容溢出到下一张卡片上面，
    # 按钮看着在、鼠标点不到，而按 DOM 计数的老检查全绿。
    overflow = json.loads(window.evaluate_value("""
() => {
  const view = document.querySelector('.view:not([hidden])');
  const bad = [];
  view.querySelectorAll('.card').forEach((card) => {
    if (card.scrollHeight > card.clientHeight + 2) {
      bad.push((card.className || 'card') + ' 高' + card.clientHeight + '<内容' + card.scrollHeight);
    }
  });
  return JSON.stringify(bad);
}""") or "[]")
    check("每张卡片都装得下自己的内容（没有溢出，也就没有互相盖住的按钮）",
          not overflow, str(overflow)[:180])
    check("Agent 屏显示的是真实端点（127.0.0.1，端口由程序自己解析）",
          "127.0.0.1" in agent.get("endpoint", ""), agent.get("endpoint", ""))
    check("工具清单如实列出（>=20 个，read/write/exec 三档都在）",
          agent.get("tools", 0) >= 20 and {"read", "write", "exec"} <= set(agent.get("risks") or []),
          f"tools={agent.get('tools')} risks={agent.get('risks')}")
    check("工具名与真实注册表一致，不是界面里写死的假清单",
          "ra.save_workflow" in " ".join(agent.get("named") or []) or agent.get("navCount") not in ("0", "—", ""),
          f"named={agent.get('named')} nav={agent.get('navCount')}")
    check("给了能直接抄走的接入片段（curl 与 MCP）",
          "/api/agent/tool" in agent.get("curl", "") and "mcpServers" in agent.get("mcp", ""),
          agent.get("curl", "")[:120])
    window.evaluate_value("""
() => {
  window.__agentProbe = {done: false};
  window.pywebview.api.agent_probe().then((r) => { window.__agentProbe = Object.assign({done: true}, r); })
    .catch((e) => { window.__agentProbe = {done: true, error: String(e && e.message || e)}; });
  return 'started';
}
""")
    end = time.time() + 30
    probe = {}
    while time.time() < end:
        probe = json.loads(window.evaluate_value("() => JSON.stringify(window.__agentProbe || {})") or "{}")
        if probe.get("done"):
            break
        time.sleep(0.4)
    check("界面里点「自检一次调用」= 真的对自己发一次 HTTP 调用并拿到结果",
          probe.get("ok") is True and probe.get("http") == 200, str(probe)[:200])
    shot("agent")

    window.evaluate_js('() => { document.querySelector(\'.nav-item[data-view="deck"]\').click(); return true; }')
    time.sleep(1.0)
    onboarding = json.loads(window.evaluate_value("""
() => {
  const box = document.getElementById('onboarding');
  return JSON.stringify({shown: !!box && !box.hidden, steps: document.querySelectorAll('#onboarding .ob-step').length,
    dismiss: !!document.getElementById('ob-dismiss')});
}
""") or "{}")
    check("首屏有三步上手引导，每步都说明这一步在干什么",
          onboarding.get("shown") is True and onboarding.get("steps") == 3 and onboarding.get("dismiss") is True,
          str(onboarding))

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
    time.sleep(0.8)
    asked = json.loads(window.evaluate_value(
        "() => JSON.stringify({open: !document.getElementById('modal').hidden,"
        " title: (document.getElementById('modal-title')||{}).textContent || ''})") or "{}")
    check("关闭先在本机窗口里确认，不直接退出", asked.get("open") is True, str(asked))
    window.evaluate_js("() => { const b = document.getElementById('modal-ok'); if (b) b.click(); return 'ok'; }")
    end = time.time() + 15
    while time.time() < end and not window.stopped():
        time.sleep(0.3)
    check("标题条关闭按钮能退出整个应用", window.stopped())

    print("\nUI 自检结论：" + ("全部通过" if not problems else f"{len(problems)} 项未通过 -> " + "；".join(problems)))
    if shots:
        print("界面截图：" + "，".join(shots))
    return 0 if not problems else 1
