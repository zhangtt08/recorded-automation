"""按钮体检：把每个界面里每一个按钮都点一遍，证明它真的能点、点了真的有反应。

用法：python -m ra.main --audit     （结果写 <数据目录>/audit-report.json，并在控制台列问题）
这一层存在的原因：「按钮点不动」和「整屏空白」以前都是被 .catch(() => {}) 吞掉的静默失败，
绿色自检查不到。这里不看意图，只看产物：滚到眼前后仍可见、中心点没有被别的元素接走、
绑了处理器、点下去有可观察变化。

录制 / 审阅 / 运行三屏是状态驱动的（侧栏没有入口），由 --verify 走完整流程时检查。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# 审计模式下不真正打到后端的方法：这些会改数据、开浏览器、动窗口。
# 它们仍然会被点击（有反馈就算活着），只是不真的执行。
BLOCKED = [
    "delete_workflow", "delete_secret", "set_secret", "save_workflow", "save_draft",
    "run_workflow", "record_start", "record_stop", "reset_profile", "close_browser",
    "selfcheck", "settings_set", "agent_restart", "reveal_data_dir", "export_workflow",
    "open_in_editor", "win_close", "win_minimize", "win_maximize", "win_mode",
    "win_fullscreen", "win_move", "win_resize", "win_gesture_end", "win_remeasure",
]

# 注入到页面里（在 app.js 之前）：记录谁绑了点击、这次点击调了哪个后端方法。
AUDIT_JS = """
(function () {
  var store = new WeakMap();
  function note(el, kind) {
    try {
      if (!(el instanceof Element)) return;
      var list = store.get(el);
      if (!list) { list = []; store.set(el, list); }
      if (list.indexOf(kind) < 0) list.push(kind);
    } catch (error) { /* 探针本身不能影响页面 */ }
  }
  window.__raHandlers = store;
  window.__raBlocked = __BLOCKED__;
  window.__raBlock = function (method) { return window.__raBlocked.indexOf(method) >= 0; };
  window.__raCalls = [];
  var add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type) {
    if (type === 'click' || type === 'pointerdown' || type === 'mousedown') note(this, type);
    return add.apply(this, arguments);
  };
  var desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'onclick');
  if (desc && desc.set) {
    Object.defineProperty(HTMLElement.prototype, 'onclick', {
      get: desc.get,
      set: function (value) { if (value) note(this, 'onclick'); return desc.set.call(this, value); },
      configurable: true, enumerable: desc.enumerable
    });
  }
})();
"""

# 关掉可能残留的确认框：确认框是模态的，不关掉会让后面每一屏都被判成「被挡住」。
DISMISS_JS = """
() => {
  const box = document.getElementById('modal');
  if (!box || box.hidden) return false;
  const cancel = document.getElementById('modal-cancel');
  const close = document.getElementById('modal-close');
  if (cancel && !cancel.hidden) cancel.click();
  else if (close && !close.hidden) close.click();
  return true;
}
"""

NAV_JS = """
() => {
  window.__raCalls = [];
  document.querySelectorAll('.toast').forEach((n) => n.remove());
  const item = document.querySelector('.nav-item[data-view="__VIEW__"]');
  if (item) item.click();
  return !!item;
}
"""

INVENTORY_JS = """
() => {
  const view = document.querySelector('.view:not([hidden])');
  const scopes = [['标题条', document.getElementById('titlebar')],
                  ['侧栏', document.querySelector('.nav')],
                  ['内容区', view]].filter((pair) => pair[1]);
  const rows = [];
  let idx = 0;
  const seen = new Set();
  scopes.forEach((pair) => {
    pair[1].querySelectorAll('button, [role="button"], a.btn').forEach((el) => {
      if (seen.has(el)) return;
      seen.add(el);
      // 用户会滚到那一行再看能不能点：先滚进视野，量到的才是真实位置
      el.scrollIntoView({ block: 'center', inline: 'nearest' });
      const box = el.getBoundingClientRect();
      const shown = box.width > 0 && box.height > 0 && el.offsetParent !== null;
      el.setAttribute('data-ra-idx', String(idx));
      const handlers = (window.__raHandlers && window.__raHandlers.get(el)) || [];
      let cover = '';
      if (shown) {
        const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
        if (hit !== el && !el.contains(hit) && !(hit && hit.contains(el))) {
          cover = hit ? (hit.tagName.toLowerCase() + (hit.id ? '#' + hit.id : '') +
            (typeof hit.className === 'string' && hit.className.trim()
              ? '.' + hit.className.trim().split(/\\s+/).join('.') : '')) : 'null';
        }
      }
      rows.push({
        i: idx, where: pair[0], id: el.id || '', text: (el.textContent || '').trim().slice(0, 24),
        cls: (typeof el.className === 'string' ? el.className : '').slice(0, 40),
        shown: shown, disabled: !!el.disabled, cover: cover, handlers: handlers.slice(),
        rect: [Math.round(box.left), Math.round(box.top), Math.round(box.width), Math.round(box.height)],
        inHidden: !!el.closest('[hidden]')
      });
      idx += 1;
    });
  });
  return JSON.stringify(rows);
}
"""

SIGN_JS = """
() => JSON.stringify({
  view: (document.querySelector('.view:not([hidden])') || {}).id || '',
  text: (document.querySelector('.view:not([hidden])') || {innerText: ''}).innerText.length,
  body: document.body.innerText.length,
  toasts: document.querySelectorAll('.toast').length,
  modal: !!document.getElementById('modal') && !document.getElementById('modal').hidden,
  errors: (window.__raErrors || []).length,
  lastError: (window.__raErrors || []).slice(-1)[0] || '',
  active: document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : ''
})
"""

CLICK_JS = """
() => {
  const el = document.querySelector('[data-ra-idx="__IDX__"]');
  if (!el) return 'gone';
  el.scrollIntoView({ block: 'center', inline: 'nearest' });
  el.click();
  return 'ok';
}
"""

CALLS_JS = """
() => JSON.stringify((window.__raCalls || []).slice(-6))
"""

VIEWS = ["deck", "history", "secrets", "agent", "settings"]
BLOCK_NOTE = "体检模式"


def _json(window, expression: str, default=None):
    try:
        raw = window.evaluate_value(expression)
    except Exception as exc:  # noqa: BLE001 - 探针失败要如实报告，不能当成通过
        return default if default is not None else {"__error": f"{type(exc).__name__}: {exc}"}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return raw if raw is not None else default


def audit_script() -> str:
    return AUDIT_JS.replace("__BLOCKED__", json.dumps(BLOCKED))


def run(window, root: Path) -> int:
    """逐屏清点并点击所有按钮。返回 0 表示没有「被挡住 / 点不动 / 点了没反应」的按钮。"""
    deadline = time.time() + 15
    while time.time() < deadline:
        if window.evaluate_value("() => !!(window.pywebview && window.pywebview.api)"):
            break
        time.sleep(0.3)
    else:
        print("按钮体检失败：界面与后端的桥接未在 15 秒内就绪")
        return 1

    report: dict = {"views": {}, "blocked": BLOCKED, "problems": []}
    bad: list[str] = []
    shots = Path(root) / "ui-shots"
    shots.mkdir(parents=True, exist_ok=True)

    for view in VIEWS:
        if not window.evaluate_value(NAV_JS.replace("__VIEW__", view)):
            bad.append(f"{view}: 侧栏里没有这个入口，界面切不过去")
            continue
        time.sleep(1.2)
        while window.evaluate_value(DISMISS_JS):
            time.sleep(0.25)
        window.screenshot(shots / f"audit-{view}.png")   # 排版好不好，看图才算数
        rows = _json(window, INVENTORY_JS, default=[])
        if isinstance(rows, dict):
            bad.append(f"{view}: 清点探针失败 {rows.get('__error')}")
            continue
        results = []
        for row in rows:
            entry = dict(row)
            if not row["shown"]:
                entry["verdict"] = "invisible"
                entry["note"] = "在 DOM 里但看不见" + ("（在 [hidden] 容器内）" if row["inHidden"] else "")
                results.append(entry)
                continue
            before = _json(window, SIGN_JS, default={}) or {}
            window.evaluate_value(CLICK_JS.replace("__IDX__", str(row["i"])))
            time.sleep(0.8)
            after = _json(window, SIGN_JS, default={}) or {}
            calls = _json(window, CALLS_JS, default=[])
            entry["calls"] = calls if isinstance(calls, list) else []
            changed = [key for key in ("view", "text", "body", "toasts", "modal", "active")
                       if before.get(key) != after.get(key)]
            new_errors = int(after.get("errors") or 0) - int(before.get("errors") or 0)
            # 体检模式自己拦下的调用不算界面缺陷
            blocked_only = new_errors > 0 and BLOCK_NOTE in str(after.get("lastError") or "")
            entry["changed"] = changed
            entry["new_errors"] = new_errors
            if row["cover"]:
                entry["verdict"] = "covered"
                entry["note"] = "被 " + row["cover"] + " 挡住，鼠标点不到"
            elif row["disabled"]:
                entry["verdict"] = "disabled"
                entry["note"] = "禁用态（界面要说明为什么禁用）"
            elif new_errors > 0 and not blocked_only:
                entry["verdict"] = "throws"
                entry["note"] = "点击后页面脚本抛错：" + str(after.get("lastError"))[:90]
            elif changed or entry["calls"]:
                entry["verdict"] = "alive"
                entry["note"] = "、".join(changed) or "有后端调用"
            elif not row["handlers"]:
                entry["verdict"] = "dead"
                entry["note"] = "没有绑定点击处理器，点下去也没有任何变化"
            else:
                entry["verdict"] = "noeffect"
                entry["note"] = "绑了处理器，但点下去界面没有任何可观察变化"
            results.append(entry)
            while window.evaluate_value(DISMISS_JS):
                time.sleep(0.25)
        report["views"][view] = results
        for item in results:
            if item["verdict"] in ("alive", "invisible"):
                continue
            label = item["id"] or item["text"] or item["cls"] or f'#{item["i"]}'
            bad.append(f"{view}/{label}: {item['verdict']} — {item['note']}")

    report["problems"] = bad
    target = Path(root) / "audit-report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(v) for v in report["views"].values())
    alive = sum(1 for v in report["views"].values() for i in v if i["verdict"] == "alive")
    hidden = sum(1 for v in report["views"].values() for i in v if i["verdict"] == "invisible")
    print(f"按钮体检：{total} 个按钮，{alive} 个点了有反应，{hidden} 个当前不可见，{len(bad)} 个有问题")
    for line in bad:
        print("  问题 " + line)
    print(f"明细：{target}")
    return 0 if not bad else 1
