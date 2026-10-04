/* 录放台 · 窗口内前端控制器：所有数据都来自同进程的 Python 后端。 */
(function () {
  "use strict";

  const S = {
    view: "deck", info: null, draft: null, draftSource: null, built: null,
    workflows: [], runs: [], secrets: [], settings: {}, recording: null,
    currentRun: "", runTimer: null, recTimer: null, shotTimer: null, clockTimer: null,
    capRows: 0, resumeStep: "", resumeWorkflow: "", runWorkflowId: "",
    busy: false
  };

  // 收集页面脚本错误，供窗口内自检读取。
  window.__raErrors = [];
  window.addEventListener("error", (event) => window.__raErrors.push(String(event.message || event.type)));
  window.addEventListener("unhandledrejection", (event) => window.__raErrors.push("rejected: " + String(event.reason)));
  // 兜底：任何一条按钮的异步失败都要在界面上说出来。
  // 以前有 4 个设置页按钮的处理器没有 catch，后端一拒绝就是「点了没反应」，用户什么也看不到。
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    if (reason && reason.notified) return;                 // call() 已经提示过，不重复弹
    const text = String((reason && reason.message) || reason || "未知错误");
    window.__raErrors.push("按钮失败: " + text);
    toast("这一步没有完成：" + text, "warn");
  });

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  // ---------- 一体化窗口：标题条与缩放手势都由界面自己画 ----------
  // 后端会把 Chromium 自绘的那条标题条顶出屏幕（停靠模式）或进入全屏；两种情况下
  // 界面上只有一条我们自绘的标题条。free 模式下浏览器标题条露出来，这条就隐藏。
  const ICON_RESTORE = '<svg width="11" height="11" viewBox="0 0 12 12"><rect x="1.8" y="3.6" width="6.4" height="6.4" rx="1.1" fill="none" stroke="currentColor" stroke-width="1.1"></rect><path d="M3.8 3.4V2.2a1 1 0 0 1 1-1h4.2a1 1 0 0 1 1 1v4.2a1 1 0 0 1-1 1H9.6" fill="none" stroke="currentColor" stroke-width="1.1"></path></svg>';
  const ICON_MAX = '<svg width="11" height="11" viewBox="0 0 12 12"><rect x="2.4" y="2.4" width="7.2" height="7.2" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.2"></rect></svg>';
  const ICON_FULL = '<svg width="11" height="11" viewBox="0 0 12 12"><path d="M2.2 4.6V2.2h2.4M7.4 2.2h2.4v2.4M9.8 7.4v2.4H7.4M4.6 9.8H2.2V7.4" fill="none" stroke="currentColor" stroke-width="1.2"></path></svg>';
  const ICON_EXIT_FULL = '<svg width="11" height="11" viewBox="0 0 12 12"><path d="M4.8 2.2v2.6H2.2M9.8 4.8H7.2V2.2M7.2 9.8V7.2H9.8M2.2 7.2h2.6v2.6" fill="none" stroke="currentColor" stroke-width="1.2"></path></svg>';

  const MODE_TEXT = { docked: "停靠无边框", free: "自由窗口", fullscreen: "全屏" };

  function paintWindowButtons() {
    const full = S.frame && S.frame.mode === "fullscreen";
    const maxed = !!(S.geom && S.geom.maximized);
    const expand = $("win-full");
    if (expand) {
      expand.innerHTML = full ? ICON_EXIT_FULL : ICON_FULL;
      expand.title = full ? "退出全屏" : "占满整块显示器（全屏）";
      expand.setAttribute("aria-label", expand.title);
    }
    const button = $("win-max");
    if (button) {
      button.innerHTML = maxed ? ICON_RESTORE : ICON_MAX;
      button.title = maxed ? "还原窗口大小" : "铺满屏幕工作区";
      button.setAttribute("aria-label", button.title);
    }
    const hint = $("tb-hint");
    if (hint) {
      const mode = (S.frame && S.frame.mode) || "";
      hint.textContent = mode === "free" ? "浏览器标题条未隐藏"
        : mode === "fullscreen" ? "全屏 · 无边框" : "无边框停靠 · 拖标题条左右移动、拖边缘改尺寸";
    }
  }

  async function syncFrame(attempt) {
    try {
      const state = await call("win_state");
      S.frame = state;
      S.geom = { x: (state.content || [])[0], y: (state.content || [])[1],
                 w: (state.content || [])[2], h: (state.content || [])[3],
                 maximized: !!state.maximized };
      S.frameless = !!(state && (state.fullscreen || state.titlebar_hidden));
      document.body.classList.toggle("frameless", S.frameless);
      document.body.classList.toggle("docked", !!(state && state.docked));
      document.body.dataset.winMode = (state && state.mode) || "";
      window.__raFrame = { at: Date.now(), mode: state && state.mode, fullscreen: !!(state && state.fullscreen),
                           titlebar_hidden: !!(state && state.titlebar_hidden), verified: S.frameless,
                           strip: state && state.strip, content: state && state.content, state: state };
      paintWindowButtons();
      // 后端还没把标题条挪出屏幕时（首帧、引擎慢）再核对几次，不靠固定 sleep 下结论
      if (state && state.docked && !state.titlebar_hidden && (attempt || 0) < 10) {
        setTimeout(() => syncFrame((attempt || 0) + 1), 500);
      }
    } catch (error) {
      document.body.classList.remove("frameless", "docked");
      window.__raFrame = { at: Date.now(), error: String((error && error.message) || error) };
    }
  }
  window.__raSyncFrame = () => { syncFrame(2); return true; };   // 后端切完窗口状态会点一下

  async function toggleMax() {
    const result = await guarded("win_maximize");
    if (result && result.error) toast("没能改变窗口大小：" + result.error, "bad");
    await syncFrame(6);
  }

  async function toggleFullscreen() {
    const want = !(S.frame && S.frame.mode === "fullscreen");
    let result = null;
    try { result = await guarded("win_mode", want ? "fullscreen" : "docked"); }
    catch (error) { result = { ok: false, error: String((error && error.message) || error) }; }
    if (!result || !result.ok) {
      toast("当前引擎不支持" + (want ? "全屏" : "切回停靠") + "：" + ((result && result.error) || "没有响应"), "warn");
    }
    await syncFrame(6);
    if (S.view === "settings") refreshSettings().catch(() => { });
  }

  // 拖动标题条 = 移动窗口；停靠模式下上下拖 = 改高度（顶边钉在屏幕顶）
  function startGesture(event, kind, edges) {
    if (event.button !== 0) { window.__raGesture = { skipped: "button " + event.button }; return; }
    const frame = S.frame || {};
    const geom = { x: (frame.content || [])[0], y: (frame.content || [])[1],
                   w: (frame.content || [])[2], h: (frame.content || [])[3] };
    if (!geom.w) { window.__raGesture = { skipped: "没有内容矩形，S.frame 还没就绪" }; return; }
    const start = { x: event.screenX, y: event.screenY };
    window.__raGesture = { at: Date.now(), kind: kind, geom: geom.slice ? geom.slice() : geom,
                           docked: !!(frame.docked), frameMode: frame.mode || "", moves: 0, sent: null };
    const grab = { x: event.screenX - geom.x, y: event.screenY - geom.y };
    const docked = !!frame.docked;
    let pending = null, raf = 0, moved = false;
    const send = (rect) => {
      pending = rect;
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        if (!pending) return;
        const rect2 = pending; pending = null;
        if (window.__raGesture) window.__raGesture.sent = rect2;
        guarded("win_resize", [rect2[0], rect2[1], rect2[2], rect2[3]])
          .then((r) => { moved = true; if (window.__raGesture) window.__raGesture.result = r && r.ok; })
          .catch((error) => { if (window.__raGesture) window.__raGesture.error = String(error && error.message || error); });
      });
    };
    const onMove = (e) => {
      const dx = e.screenX - start.x, dy = e.screenY - start.y;
      if (window.__raGesture) window.__raGesture.moves++;
      if (kind === "move") {
        // 停靠模式只改水平位置：同时改 x 与高度的边界请求会被 Chromium 回弹到上一次位置（实测）。
        // 要改高度就拖下边缘，这也更符合「拖标题条 = 移动」的直觉。
        send([geom.x + dx, docked ? geom.y : geom.y + dy, geom.w, geom.h]);
      } else {
        let [x, y, w, h] = geom;
        if (edges.includes("w")) { x += dx; w -= dx; }
        if (edges.includes("e")) { w += dx; }
        if (edges.includes("n") && !docked) { y += dy; h -= dy; }
        if (edges.includes("s")) { h += dy; }
        if (w < 900) { if (edges.includes("w")) x -= (900 - w); w = 900; }
        if (h < 420) { if (edges.includes("n") && !docked) y -= (420 - h); h = 420; }
        send([x, y, w, h]);
      }
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      if (raf) cancelAnimationFrame(raf);
      guarded("win_gesture_end").then(() => { if (moved) syncFrame(2); });
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    event.preventDefault();
  }

  function wireWindow() {
    const bar = $("titlebar");
    let resizeTimer = 0;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { syncFrame(2).catch(() => { }); }, 300);
    });
    setInterval(() => { syncFrame(0).catch(() => { }); }, 2500);
    bar.addEventListener("pointerdown", (event) => {
      if (event.target.closest(".wbtn") || event.target.closest("input,button,select")) return;
      if (event.detail === 2) return;                       // 双击交给 dblclick
      startGesture(event, "move");
    });
    bar.addEventListener("dblclick", (event) => {
      if (!event.target.closest(".wbtn")) toggleMax().catch(() => { });
    });
    const grips = { gw: "ew-resize", ge: "ew-resize", gs: "ns-resize", gsw: "nesw-resize", gse: "nwse-resize",
                    gn: "ns-resize", gnw: "nwse-resize", gne: "nesw-resize" };
    Object.keys(grips).forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.style.cursor = grips[id];
      el.addEventListener("pointerdown", (event) => startGesture(event, "resize", id.slice(1)));
    });
    $("win-min").onclick = () => guarded("win_minimize").catch((error) =>
      toast("最小化没生效：" + ((error && error.message) || error), "warn"));
    $("win-max").onclick = () => toggleMax().catch(() => { });
    $("win-full").onclick = () => toggleFullscreen().catch(() => { });
    $("win-close").onclick = () => confirmWindowClose();
    paintWindowButtons();
  }

  async function confirmWindowClose() {
    const busyNow = !!(S.recTimer || S.runTimer);
    const ok = await ask("退出录放台？", busyNow
      ? "当前正在录制或运行：退出会停在当前步骤，日志已记录到这里，之后不会自动续跑。"
      : "工作流、运行记录和秘密库都已保存在本机，下次打开还在原处。");
    if (!ok) return;
    call("win_close").catch(() => { });
  }

  // ---------- bridge ----------
  function bridge() {
    if (window.pywebview && window.pywebview.api) return Promise.resolve(window.pywebview.api);
    return new Promise((resolve, reject) => {
      let waited = 0;
      const timer = setInterval(() => {
        if (window.pywebview && window.pywebview.api) { clearInterval(timer); resolve(window.pywebview.api); }
        else if ((waited += 120) > 20000) { clearInterval(timer); reject(new Error("后端未就绪")); }
      }, 120);
      window.addEventListener("pywebviewready", () => { clearInterval(timer); resolve(window.pywebview.api); });
    });
  }

  async function call(method, ...args) {
    const api = await bridge();
    if (!api[method]) throw new Error("接口缺失：" + method);
    const result = await api[method](...args);
    if (result && typeof result === "object" && result.error) {
      if (!result.busy) toast(result.error, "bad");
      else toast(result.error, "warn");
      const error = new Error(result.error);
      error.payload = result;                       // 结构化原因（例如缺哪些秘密引用名）
      error.notified = true;                        // 上面已经 toast 过，兜底监听不要再弹一次
      throw error;
    }
    return result;
  }

  function toast(message, kind) {
    const row = document.createElement("div");
    row.className = "toast " + (kind || "");
    row.innerHTML = '<span class="lamp ' + (kind === "bad" ? "bad" : kind === "ok" ? "ok" : kind === "warn" ? "warn" : "info") + '"></span><span>' + esc(message) + "</span>";
    $("toasts").appendChild(row);
    setTimeout(() => row.remove(), kind === "bad" ? 7000 : 4200);
  }

  function busy(on) {
    S.busy = on;
    $("busy-bar").classList.toggle("on", on);
  }

  // 剪贴板在窗口没有焦点时会被直接拒绝；先退到 execCommand，真不行才让用户在文本框里全选。
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (error) { /* 换下一条路 */ }
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.top = "-1000px";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      return ok;
    } catch (error) {
      return false;
    }
  }

  // 应用窗口内的确认框（不依赖原生 confirm，也不会被浏览器自动关掉）
  function ask(title, detail) {
    return new Promise((resolve) => {
      const box = $("modal");
      $("modal-title").textContent = title;
      $("modal-detail").textContent = detail || "";
      $("modal-body").hidden = true;
      $("modal-close").hidden = true;
      $("modal-cancel").hidden = false;
      $("modal-ok").hidden = false;
      box.hidden = false;
      const finish = (value) => {
        box.hidden = true;
        $("modal-ok").onclick = null;
        $("modal-cancel").onclick = null;
        box.onclick = null;
        resolve(value);
      };
      $("modal-ok").onclick = () => finish(true);
      $("modal-cancel").onclick = () => finish(false);
      box.onclick = (event) => { if (event.target === box) finish(false); };
    });
  }

  function showText(title, text) {
    const box = $("modal");
    $("modal-title").textContent = title;
    $("modal-detail").textContent = "无法直接写入剪贴板时，在这里全选复制。";
    const body = $("modal-body");
    body.value = text;
    body.hidden = false;
    $("modal-ok").hidden = true;
    $("modal-cancel").hidden = true;
    $("modal-close").hidden = false;
    box.hidden = false;
    $("modal-close").onclick = () => { box.hidden = true; $("modal-close").onclick = null; };
    setTimeout(() => body.select(), 30);
  }

  async function guarded(method, ...args) {
    busy(true);
    try { return await call(method, ...args); }
    finally { busy(false); }
  }

  // ---------- time ----------
  const hms = (seconds) => {
    const total = Math.max(0, Math.floor(seconds));
    const pad = (n) => String(n).padStart(2, "0");
    return pad(Math.floor(total / 3600)) + ":" + pad(Math.floor(total / 60) % 60) + ":" + pad(total % 60);
  };
  const clock = (stamp) => {
    if (!stamp) return "—";
    const date = new Date(stamp * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
  };
  const ago = (stamp) => {
    if (!stamp) return "";
    const diff = Date.now() / 1000 - stamp;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 172800) return "昨天";
    return Math.floor(diff / 86400) + " 天前";
  };
  // 运行 ID 现在是 MMDD + HHMMSS + 毫秒：日期在 id 里，历史列表才答得出「这次是哪天跑的」，
  // 而不是只有一句看不出先后顺序的「3 小时前」。
  const runStamp = (runId) => {
    const text = String(runId || "");
    if (text.length < 10) return text;
    return text.slice(0, 2) + "-" + text.slice(2, 4) + " " + text.slice(4, 6) + ":" +
      text.slice(6, 8) + ":" + text.slice(8, 10);
  };

  const STATUS_LABEL = {
    completed: ["ok", "完成"], completed_unverified: ["info", "完成 · 未验证"],
    uncertain: ["warn", "未确认"], failed: ["bad", "失败"], cancelled: ["", "已取消"],
    running: ["rec", "运行中"], "": ["", "从未运行"]
  };
  const STATUS_WORD = {
    unique: ["ok", "唯一匹配"], ambiguous: ["warn", "匹配多个元素"],
    missing: ["bad", "页面上找不到"], unchecked: ["", "未重新校验"]
  };
  const ACTION_WORD = { click: "点击", fill: "填写", hotkey: "按键", wait_for: "等待", merge: "填写" };
  const VIEW_NAMES = {
    deck: "工作台", recording: "录制中", review: "审阅与编辑", run: "运行结果",
    history: "运行历史", secrets: "秘密库", agent: "Agent 接口", settings: "设置"
  };

  const statusChip = (key, label) => {
    const pair = STATUS_LABEL[key] || ["", label || key];
    return '<span class="status-chip ' + pair[0] + '"><span class="lamp ' + pair[0] + '"></span>' + esc(label || pair[1]) + "</span>";
  };

  // ---------- views ----------
  function show(view) {
    S.view = view;
    $("tb-view").textContent = VIEW_NAMES[view] || "";
    ["deck", "recording", "review", "run", "history", "secrets", "agent", "settings"].forEach((name) => {
      $("view-" + name).hidden = name !== view;
    });
    document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
      const on = item.dataset.view === view;
      item.classList.toggle("active", on);
      if (on) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    stopTickers();
    const stale = $("load-fail");
    if (stale) stale.remove();
    if (view === "recording") startTickers();
    if (view === "run" && S.currentRun) pollRun().catch((error) => loadFailed("这次运行的详情", error));
    if (view === "deck") refreshDeck().catch((error) => loadFailed("工作台", error));
    if (view === "history") refreshHistory().catch((error) => loadFailed("运行历史", error));
    if (view === "secrets") refreshSecrets().catch((error) => loadFailed("秘密库", error));
    if (view === "settings") refreshSettings().catch((error) => loadFailed("设置", error));
    if (view === "agent") refreshAgent().catch((error) => loadFailed("Agent 接口", error));
  }

  // 一屏没读到就在界面上说清楚，并给一个真的能用的出口（重试），不再留空白。
  function loadFailed(where, error) {
    const box = document.querySelector(".view:not([hidden])");
    if (!box) return;
    const old = $("load-fail");
    if (old) old.remove();
    const bar = document.createElement("div");
    bar.className = "card loadfail";
    bar.id = "load-fail";
    bar.innerHTML = '<span class="t">' + esc(where) + "没有读到</span>"
      + '<span class="d">' + esc(String((error && error.message) || error || "后端没有返回内容")) + "</span>"
      + '<button class="btn small" id="load-retry">重试</button>';
    const head = box.querySelector(".main-head");
    if (head) head.after(bar); else box.prepend(bar);
    $("load-retry").onclick = () => { bar.remove(); show(S.view); };
  }

  function stopTickers() {
    if (S.recTimer) { clearInterval(S.recTimer); S.recTimer = null; }
    if (S.shotTimer) { clearInterval(S.shotTimer); S.shotTimer = null; }
    if (S.runTimer) { clearInterval(S.runTimer); S.runTimer = null; }
  }

  function startTickers() {
    stopTickers();
    S.recTimer = setInterval(refreshRecording, 800);
    S.shotTimer = setInterval(refreshShot, 1600);
    refreshRecording();
    refreshShot();
  }

  function applyState(state) {
    if (!state) return;
    const map = { idle: ["", "空闲"], recording: ["rec", "录制中"], review: ["info", "审阅中"], running: ["warn", "回放中"] };
    const pair = map[state.state] || ["", state.state];
    $("state-lamp").className = "lamp " + pair[0];
    $("state-text").textContent = pair[1];
    $("state-clock").textContent = pair[0] === "rec" ? hms(elapsedRecording()) : "";
    const pill = $("tb-status");
    if (pill) {
      pill.innerHTML = state.state === "idle" ? "" :
        '<span class="pill ' + esc(pair[0]) + '"><span class="lamp ' + esc(pair[0]) + '"></span>' + esc(pair[1]) +
        (pair[0] === "rec" ? ' <span class="mono" id="tb-clock">' + hms(elapsedRecording()) + "</span>" : "") + "</span>";
    }
    $("record-start").disabled = state.busy;
    $("record-url").disabled = state.busy;
    document.querySelectorAll(".wf-actions .btn").forEach((btn) => {
      btn.style.visibility = state.busy && btn.dataset.run ? "hidden" : "visible";
    });
  }

  function elapsedRecording() {
    return S.recording && S.recording.started_at ? Date.now() / 1000 - S.recording.started_at : 0;
  }
  // ---------- deck ----------
  function renderStats() {
    const box = $("deck-stats");
    if (!box) return;
    const runs = S.runs || [];
    const last = runs[0];
    const needed = new Set();
    S.workflows.forEach((row) => (row.secret_refs || []).forEach((ref) => needed.add(ref)));
    const have = new Set(((S.info && S.info.secrets) || []).map((item) => item.ref));
    const missing = Array.from(needed).filter((ref) => !have.has(ref));
    const recent = runs.slice(0, 20);
    const good = recent.filter((row) => ["completed", "completed_unverified"].includes(row.status)).length;
    const steps = S.workflows.reduce((total, row) => total + (row.steps || 0), 0);
    const engine = (S.info && S.info.state && S.info.state.engine) || "未启动";
    const cards = [
      { k: "工作流", v: String(S.workflows.length), sub: steps + " 步" },
      { k: "最近 " + recent.length + " 次运行", v: good + "/" + recent.length, sub: last ? "最新 " + (last.duration_s || 0) + "s" : "还没有运行" },
      { k: "待填秘密引用", v: String(missing.length), sub: missing.length ? missing[0] : "秘密库已齐", accent: missing.length > 0 },
      { k: "浏览器引擎", v: engine, sub: (S.info && S.info.bundled) ? "封装版" : "源码运行", small: true }
    ];
    box.innerHTML = cards.map((card) => '<div class="card stat' + (card.accent ? " accent" : "") + '">' +
      '<span class="k">' + esc(card.k) + "</span>" +
      '<span class="v"' + (card.small ? ' style="font-size:14px"' : "") + ">" + esc(card.v) + "</span>" +
      '<span class="s">' + esc(card.sub) + "</span></div>").join("");
    const note = $("engine-note");
    if (note) {
      note.textContent = ((S.info && S.info.bundled) ? "封装版" : "源码运行") +
        " · " + ((S.info && S.info.state && S.info.state.engine) || "浏览器未启动");
    }
    // 标题条上写清「这一份到底是哪一版」：只写 __version__ 时，界面改了十几个文件
    // 程序仍自称同一个版本号，排查时完全分不清屏幕上的是新的还是旧的。
    const chip = $("ver-chip");
    if (chip && S.info && S.info.version) {
      const stamp = S.info.version_stamp || {};
      chip.textContent = "v" + S.info.version + " · " + (S.info.version_kind || (stamp.bundled ? "封装版" : "源码"));
      chip.title = "版本 v" + S.info.version + " · " + (S.info.version_kind || "") +
        (stamp.source_mtime ? " · 代码 " + new Date(stamp.source_mtime * 1000).toLocaleString() : "") +
        (stamp.ui_mtime ? " · 界面资源 " + new Date(stamp.ui_mtime * 1000).toLocaleString() : "") +
        (stamp.ui_newer_than_source ? "（界面资源比代码新：重启或重新打包才会用上）" : "");
    }
  }

  async function refreshDeck() {
    const info = await call("app_info");
    S.info = info;
    S.workflows = info.workflows || [];
    S.runs = info.runs || [];
    $("nav-workflows").textContent = S.workflows.length;
    $("nav-runs").textContent = S.runs.length;
    $("nav-secrets").textContent = (info.secrets || []).length;
    $("nav-agent").textContent = info.agent && info.agent.ok ? (info.agent.tools + " 个") : "未启用";
    $("wf-count").textContent = S.workflows.length + " 条";
    $("run-count").textContent = S.runs.length + " 条记录";
    renderStats();
    renderOnboarding();
    renderWorkflows();
    renderRecent(S.runs.slice(0, 4), "recent-runs");
    applyState(info.state);
  }

  function renderWorkflows() {
    const list = $("wf-list");
    if (!S.workflows.length) {
      list.innerHTML = '<div class="card empty">' +
        '<svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4">' +
        '<rect x="3" y="4" width="18" height="14" rx="2"></rect><path d="M3 8h18"></path><circle cx="5.6" cy="6" r=".6" fill="currentColor"></circle>' +
        '<path d="M8 12h8M8 15h5"></path></svg>' +
        '<p class="big">还没有工作流</p>输入地址开始录制；录制到的操作要先经过人工审阅才会保存。</div>';
      return;
    }
    list.innerHTML = S.workflows.map((row) => {
      const last = S.runs.find((item) => item.workflow_id === row.id);
      const seq = (row.actions || []).map((action) => '<span class="act ' + esc(action) + '">' + esc(action) + "</span>").join("");
      const busy = row.broken ? "busy" : "";
      return '<div class="card wf-row ' + busy + '" data-id="' + esc(row.id) + '">' +
        '<span class="lamp ' + (row.broken ? "bad" : (last ? STATUS_LABEL[last.status] ? STATUS_LABEL[last.status][0] : "" : "")) + '"></span>' +
        '<span class="wf-avatar">' + esc((row.name || row.id || "?").trim().charAt(0)) + "</span>" +
        '<div class="wf-main"><p class="nm">' + esc(row.name) + '</p><p class="id">' + esc(row.id) + "</p>" +
        '<p class="seq">' + seq + "</p></div>" +
        '<div class="wf-origin"><span class="chip mono"><span class="dot"></span>' + esc(row.origin || "损坏文件") + "</span></div>" +
        '<div class="wf-meta"><span class="chip mono">' + esc(row.steps) + " 步</span>" +
        (row.secret_refs && row.secret_refs.length ? '<span class="chip mono">秘密 ' + row.secret_refs.length + "</span>" : "") +
        (row.unverified ? '<span class="chip mono">未验证 ' + row.unverified + "</span>" : "") + "</div>" +
        '<div class="wf-last">' + (last ? statusChip(last.status) : statusChip("", "从未运行")) +
        '<span class="ago">' + esc(ago(last ? last.ended_at : 0)) + "</span></div>" +
        '<div class="wf-actions">' +
        (row.broken ? '<button class="btn small" data-del="' + esc(row.id) + '">删除</button>' :
          '<button class="btn small" data-edit="' + esc(row.id) + '">审阅</button>' +
          '<button class="btn small" data-export="' + esc(row.id) + '">导出</button>' +
          '<button class="btn small" data-del="' + esc(row.id) + '">删除</button>' +
          '<button class="btn small primary" data-run="' + esc(row.id) + '">运行</button>') +
        "</div></div>";
    }).join("");
  }

  function renderRecent(runs, id) {
    const box = $(id);
    if (!runs.length) {
      box.innerHTML = '<div class="empty"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4">' +
        '<circle cx="12" cy="12" r="8"></circle><path d="M12 8v4.4l3 1.8"></path></svg>' +
        '<p class="big">还没有运行记录</p>运行一次工作流后，这里会显示每次的事实日志。</div>';
      return;
    }
    box.innerHTML = runs.map((row) => {
      const where = row.status === "failed" || row.status === "uncertain" || row.status === "cancelled"
        ? " · 停在步骤 " + esc(row.step_id || "?") : " · " + esc(row.steps) + " 步";
      return '<div class="rrow" data-run="' + esc(row.run_id) + '" style="cursor:pointer">' +
        '<span class="rid">' + esc(row.run_id) + "</span>" +
        '<span class="rname">' + esc(row.name || row.workflow_id) + "</span>" +
        statusChip(row.status) +
        '<span class="rdur">' + esc(row.duration_s) + "s" + where + "</span>" +
        (row.missing_workflow ? '<span class="chip mono">工作流已删除</span>' : "") +
        '<span class="rago">' + esc(clock(row.ended_at)) + "</span></div>";
    }).join("");
  }

  // ---------- recording ----------
  function candidateRow(step, live) {
    const kind = step.kind === "merge" ? "fill…" : step.kind;
    const tag = step.options && step.options[step.selected] ? step.options[step.selected] : null;
    const value = step.sensitive ? '<span class="secret">secret_ref: ' + esc(step.secret_ref || "待命名") + "</span>"
      : (step.value ? '<span class="val">' + esc(step.value) + "</span>" : "");
    return '<div class="cap' + (live ? " live" : "") + '"><div class="row1">' +
      '<span class="act ' + esc(kind) + '">' + esc(kind) + "</span>" +
      '<span class="what">' + esc(step.title) + "</span>" +
      (live ? '<span class="dots-typing"><i></i><i></i><i></i></span>' : "") +
      '<span class="tc">' + clock(step.updated_at) + "</span></div>" +
      '<div class="row2">' + (tag ? '<span class="tag ' + esc(tag.strategy) + '">' + esc(tag.strategy) +
        " " + esc(tag.value) + (tag.name ? "·" + esc(tag.name) : "") + "</span>" + (tag.count > 1 ? '<span class="tag text">匹配 ' + tag.count + "</span>" : "") : "") +
      value + (step.merged ? '<span class="val">已合并 ' + esc(step.merged) + " 次输入</span>" : "") + "</div></div>";
  }

  // 长录制时的增量渲染：连续输入只会改写最后一行，新步骤按行追加，不重建整张列表。
  function renderCandidates(snap) {
    const list = $("cap-list");
    const steps = snap.steps || [];
    const live = snap.state === "recording";
    if (!steps.length) {
      if (S.capRows !== 0) {
        list.innerHTML = '<div class="empty" style="padding:22px">等待第一次点击或输入。网页里的操作会出现在这里。</div>';
        S.capRows = 0;
      }
      return;
    }
    if (S.capRows === 0 || steps.length < S.capRows) {
      list.innerHTML = steps.map((step, index) =>
        candidateRow(step, live && index === steps.length - 1)).join("");
    } else {
      const from = S.capRows - 1;                     // 重画最后一行（可能被归并改写）再追加新增行
      while (list.children.length > from) list.removeChild(list.lastElementChild);
      list.insertAdjacentHTML("beforeend", steps.slice(from).map((step, offset) =>
        candidateRow(step, live && from + offset === steps.length - 1)).join(""));
    }
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 140;
    S.capRows = steps.length;
    if (nearBottom) list.scrollTop = list.scrollHeight;
  }

  async function startRecording() {
    const url = $("record-url").value.trim();
    if (!url) { toast("请输入要录制的页面地址", "warn"); return; }
    const result = await guarded("start_recording", url);
    if (!result || result.error) return;
    S.recording = { started_at: Date.now() / 1000, origin: result.origin };
    S.capRows = 0;
    $("rec-scope").innerHTML = "仅记录 <b>" + esc(result.origin) + "</b> 内的操作";
    show("recording");
    toast("录制中：请在受控浏览器里操作网页", "ok");
  }

  async function refreshRecording() {
    if (S.view !== "recording") return;              // 停止后不再刷新，避免复活录制计时
    const snap = await call("recording");
    S.recording = Object.assign(S.recording || {}, snap);
    $("rec-time").textContent = hms(snap.elapsed || 0);
    $("rec-count").textContent = "候选步骤 " + snap.count + " · 已丢弃 " + snap.discarded;
    $("rec-url").textContent = snap.url || "—";
    $("rec-state").textContent = (snap.state === "recording" ? "录制中" : "已停止") + " · 受控浏览器";
    renderCandidates(snap);
    const rail = document.querySelectorAll("#view-recording .perf i");
    const filled = Math.max(0, Math.min(rail.length, Math.floor((snap.elapsed || 0) / 5)));
    rail.forEach((node, index) => node.classList.toggle("on", index < filled));
    const drop = $("drop-zone");
    if (!snap.dropped.length) {
      drop.innerHTML = '<p class="lbl">已丢弃的事件</p><p style="font-size:11px;color:var(--ink3);margin-top:6px">暂无。其他来源、超大事件会在下列出。</p>';
    } else {
      drop.innerHTML = '<p class="lbl">已丢弃的事件</p>' + snap.dropped.slice(-8).reverse().map((item) =>
        '<div class="drop-row"><span class="mono">' + esc(clock(item.at)) + "</span><span>" + esc(item.reason) + "</span>" +
        '<span class="why">' + esc((item.url || "").replace(/^https?:\/\//, "").slice(0, 34)) + "</span></div>").join("");
    }
    if (snap.state !== "recording") stopTickers();
  }

  async function refreshShot() {
    try {
      const shot = await call("preview");
      if (shot && shot.png) {
        const img = $("rec-shot");
        img.src = "data:image/png;base64," + shot.png;
        img.hidden = false;
        $("rec-shot-hint").hidden = true;
      }
    } catch (error) { /* 浏览器尚未打开 */ }
  }

  async function stopRecording() {
    const draft = await guarded("stop_recording");
    S.recording = null;
    stopTickers();
    openReview(draft, "draft");
    show("review");
  }

  // ---------- review ----------
  function openReview(payload, source) {
    if (!payload) return;
    if (source === "draft") {
      S.reviewName = undefined; S.reviewId = undefined;
      S.reviewNameFixed = ""; S.reviewIdFixed = "";
    }
    S.draft = payload;
    S.draftSource = source;
    $("review-name").value = S.reviewName === undefined ? defaultName() : S.reviewName;
    $("review-id").value = S.reviewId === undefined ? defaultId() : S.reviewId;
    $("meta-start").textContent = "起始地址 " + (payload.start_url || payload.origin || "—") + " · 只允许该来源内的操作";
    renderReview();
  }

  function renderReview() {
    const draft = S.draft;
    if (!draft) { $("step-list").innerHTML = '<div class="card empty">没有可审阅的草稿。</div>'; return; }
    const steps = draft.steps || [];
    $("rev-title").textContent = (S.draftSource === "saved" ? "审阅已保存的工作流" : "审阅：本次录制");
    $("rev-chips").innerHTML = '<span class="chip mono">' + esc(draft.origin) + '</span><span class="chip mono">Workflow v1</span>' +
      '<span class="chip mono">' + steps.length + " 步</span>";
    $("step-list").innerHTML = steps.map((step, index) => stepCard(step, index, steps)).join("") ||
      '<div class="card empty">这次没有录到任何操作。</div>';
    bindStepEvents(steps);
    refreshBuilt();
  }

  function stepCard(step, index, steps) {
    const pair = STATUS_WORD[step.status] || ["", step.status];
    const option = step.options && step.options[step.selected];
    const alternatives = (step.options || []).map((item, position) =>
      '<button class="loc ' + (position === step.selected ? "star" : "alt") + '" data-pick="' + position + '">' +
      '<span class="tag ' + esc(item.strategy) + '">' + esc(item.strategy) + "</span>" +
      '<span class="mono">' + esc(item.value) + (item.name ? "·" + esc(item.name) : "") + "</span>" +
      '<span class="ghost-hint">' + (item.count < 0 ? "未校验" : item.count + " 个") + "</span></button>").join("");
    const expectedOptions = ['<option value="">不设完成条件（记为未验证）</option>'].concat(
      steps.filter((other) => other.id !== step.id).map((other) => {
        const otherOption = other.options && other.options[other.selected];
        const text = otherOption ? otherOption.strategy + " " + otherOption.value : other.title;
        const selected = step.expected && step.expected.from_step === other.id ? " selected" : "";
        return '<option value="' + esc(other.id) + '"' + selected + ">使用步骤 " + esc(other.id) + " 的目标（" + esc(text) + "）</option>";
      }),
      ['<option value="text"' + (step.expected && step.expected.locators ? " selected" : "") + ">按页面文本判断成功</option>"]
    ).join("");
    const expectedText = step.expected && step.expected.locators ? step.expected.locators[0].value : "";
    const expectedKind = step.expected ? (step.expected.from_step ? "from" : "text") : "";
    const valueField = step.kind === "fill" || step.kind === "merge"
      ? (step.sensitive
        ? '<span class="secret-chip">secret_ref: ' + esc(step.secret_ref || "待命名") + '</span><span class="ghost-hint">运行时从秘密库读取，不写入文件</span>'
        : '<div class="field"><input type="text" class="wide" data-value="' + esc(step.id) + '" value="' + esc(step.value || "") + '">' +
          '<label class="switch' + (step.sensitive ? " on" : "") + '"><input type="checkbox" data-sensitive="' + esc(step.id) + '"' + (step.sensitive ? " checked" : "") +
          ' style="display:none"><span class="track"><i></i></span><span class="txt">' + (step.sensitive ? "敏感" : "标为敏感") + "</span></label></div>")
      : "";
    const hotkeyField = step.kind === "hotkey"
      ? '<div class="field"><input type="text" data-hotkey="' + esc(step.id) + '" value="' + esc(step.hotkey || "") + '" placeholder="Control+Enter"></div>' : "";
    const locatorEdit = '<div class="field"><select data-strategy="' + esc(step.id) + '">' +
      ["role", "label", "test_id", "text", "css"].map((strategy) =>
        '<option value="' + strategy + '"' + (option && option.strategy === strategy ? " selected" : "") + ">" + strategy + "</option>").join("") +
      '</select><input type="text" class="wide" data-locvalue="' + esc(step.id) + '" value="' + esc(option ? option.value : "") + '" placeholder="定位值">' +
      '<input type="text" data-locname="' + esc(step.id) + '" value="' + esc(option && option.name ? option.name : "") + '" placeholder="可访问名称" style="width:130px">' +
      '<button class="btn small" data-apply="' + esc(step.id) + '">应用定位器</button></div>';
    return '<div class="card step ' + (step.status === "ambiguous" ? "warn-card" : step.status === "missing" ? "bad-card" : "") + '" data-step="' + esc(step.id) + '">' +
      '<div class="step-head"><span class="step-idx">' + (index + 1) + "</span>" +
      '<span class="tag ' + esc(step.kind === "merge" ? "fill" : step.kind) + '">' + esc(ACTION_WORD[step.kind] ? step.kind : step.kind) + "</span>" +
      '<span class="step-title">' + esc((ACTION_WORD[step.kind] || step.kind) + " " + step.title) + "</span>" +
      '<span class="step-tools"><span class="status-chip ' + pair[0] + '"><span class="lamp ' + pair[0] + '"></span>' + esc(pair[1]) + "</span>" +
      '<button class="btn small" data-up="' + esc(step.id) + '">↑</button><button class="btn small" data-down="' + esc(step.id) + '">↓</button>' +
      '<button class="btn small danger" data-remove="' + esc(step.id) + '">删除</button></span></div>' +
      '<div class="step-body">' +
      '<div class="kv"><span class="k">目标</span><span class="v">' + (alternatives || '<span class="ghost-hint">键盘组合，可不设目标</span>') + "</span></div>" +
      '<div class="kv"><span class="k">编辑</span><span class="v">' + locatorEdit + "</span></div>" +
      (valueField ? '<div class="kv"><span class="k">输入值</span><span class="v">' + valueField + "</span></div>" : "") +
      (hotkeyField ? '<div class="kv"><span class="k">组合键</span><span class="v">' + hotkeyField + "</span></div>" : "") +
      '<div class="kv"><span class="k">完成条件</span><span class="v"><div class="field">' +
      '<select data-expkind="' + esc(step.id) + '">' + expectedOptions + "</select>" +
      (expectedKind === "text" ? '<input type="text" data-exptext="' + esc(step.id) + '" value="' + esc(expectedText) + '" placeholder="页面出现的文本">' : "") +
      '<input type="number" data-timeout="' + esc(step.id) + '" value="' + esc(step.timeout_s || 10) + '" min="1" max="120" style="width:66px" title="超时秒数">' +
      "</div></span></div>" +
      (step.status === "ambiguous" ? '<div class="ambig"><p class="amb-t">需人工选择：页面上有多个元素匹配此定位器</p>' +
        (step.options || []).map((item, position) => '<div class="cand' + (position === step.selected ? " sel" : "") + '" data-pick="' + position + '">' +
          '<span class="radio"></span><span class="desc mono">' + esc(item.strategy) + " " + esc(item.value) + (item.name ? " ·" + esc(item.name) : "") + "</span>" +
          '<span class="where">匹配 ' + esc(item.count) + " 个</span></div>").join("") +
        '<p class="amb-note">收窄定位值（例如加上父级选择器）后点“重新校验唯一性”；不会退化为坐标定位。</p></div>' : "") +
      (step.status === "missing" ? '<div class="ambig bad"><p class="amb-t">页面上找不到这个目标 · 按这三步修</p>' +
        '<ol class="fixpath"><li>在上面的「编辑」里换策略或改定位值（优先级 role → label → test_id → text → css），点「应用定位器」。</li>' +
        '<li>确认受控浏览器仍停在这一步所在的页面，再点右上角「重新校验唯一性」重数一遍。</li>' +
        '<li>这一步其实不该点东西：改「完成条件」为按页面文本判断，或删掉这一步（目标消失多半是页面改版或还没走到）。</li></ol>' +
        '<p class="amb-note">找不到目标的步骤就算保存了，回放也会在动作之前停下，不会误点别处。</p></div>' : "") +
      "</div></div>";
  }

  // 每张步骤卡片单独绑定事件；长录制时改一步只重画那一张卡，不重建整列。
  function bindStepCard(card, steps) {
    const stepId = card.dataset.step;
    const after = async (only) => { await reloadDraft(only ? stepId : ""); };
    // 「需人工选择」面板里的候选行是 div，但样式带 pointer，必须和上面的候选按钮一样可点
    card.querySelectorAll("[data-pick]").forEach((button) => {
      button.onclick = async () => {
        await guarded("edit_step", stepId, { selected: parseInt(button.dataset.pick, 10) });
        await after(true);
      };
    });
    card.querySelectorAll("[data-up],[data-down],[data-remove]").forEach((button) => {
      button.onclick = async () => {
        const id = button.dataset.up || button.dataset.down || button.dataset.remove;
        if (button.dataset.remove) {
          if (!(await ask("删除步骤 " + id + "？", "该步骤及其完成条件会从草稿中移除。"))) return;
          await guarded("remove_step", id);
          await after(false);                       // 删除会重排 ID，整列重画
        } else {
          await guarded("move_step", id, button.dataset.up ? -1 : 1);
          await after(false);
        }
      };
    });
    card.querySelectorAll("[data-apply]").forEach((button) => {
      button.onclick = async () => {
        const strategy = card.querySelector('[data-strategy="' + stepId + '"]').value;
        const value = card.querySelector('[data-locvalue="' + stepId + '"]').value.trim();
        const name = card.querySelector('[data-locname="' + stepId + '"]').value.trim();
        if (!value) { toast("定位值不能为空", "warn"); return; }
        const option = { strategy: strategy, value: value, count: -1 };
        if (strategy === "role") option.name = name;
        await guarded("edit_step", stepId, { options: [option] });
        await after(true);
        toast("已写入定位器，点“重新校验唯一性”确认匹配数量", "ok");
      };
    });
    card.querySelectorAll("[data-expkind]").forEach((select) => {
      select.onchange = async () => {
        const text = card.querySelector('[data-exptext="' + stepId + '"]');
        let expected = null;
        if (select.value === "text") {
          const value = text ? text.value.trim() : "";
          if (!value) { toast("请输入用于判断成功的页面文本", "warn"); select.value = ""; return; }
          expected = { locators: [{ strategy: "text", value: value }] };
        } else if (select.value) {
          expected = { from_step: select.value };
        }
        await guarded("edit_step", stepId, { expected: expected });
        await after(true);
      };
    });
    card.querySelectorAll("[data-exptext]").forEach((input) => {
      input.onchange = async () => {
        if (!input.value.trim()) { await guarded("edit_step", stepId, { expected: null }); }
        else { await guarded("edit_step", stepId, { expected: { locators: [{ strategy: "text", value: input.value.trim() }] } }); }
        await after(true);
      };
    });
    card.querySelectorAll("[data-value]").forEach((input) => {
      input.onchange = async () => {
        await guarded("edit_step", stepId, { value: input.value });
        await after(true);
      };
    });
    card.querySelectorAll("[data-hotkey]").forEach((input) => {
      input.onchange = async () => {
        await guarded("edit_step", stepId, { hotkey: input.value.trim() });
        await after(true);
      };
    });
    card.querySelectorAll("[data-timeout]").forEach((input) => {
      input.onchange = async () => {
        await guarded("edit_step", stepId, { timeout_s: parseFloat(input.value) || 10 });
        await after(true);
      };
    });
    card.querySelectorAll("[data-sensitive]").forEach((box) => {
      box.onchange = async () => {
        await guarded("edit_step", stepId, { sensitive: box.checked });
        await after(true);                          // 引用名可能新命名，整张卡重画
      };
    });
  }

  function bindStepEvents(steps) {
    $("step-list").querySelectorAll(".step").forEach((card) => bindStepCard(card, steps));
  }

  async function reloadDraft(onlyStepId) {
    const result = await call("draft");
    if (!result.draft) { openReview(result.draft, S.draftSource); return; }
    S.draft = result.draft;
    const steps = result.draft.steps || [];
    const card = onlyStepId ? document.querySelector('#step-list .step[data-step="' + onlyStepId + '"]') : null;
    const index = onlyStepId ? steps.findIndex((item) => item.id === onlyStepId) : -1;
    if (!card || index < 0) { openReview(result.draft, S.draftSource); return; }
    const holder = document.createElement("div");
    holder.innerHTML = stepCard(steps[index], index, steps);
    const fresh = holder.firstElementChild;
    card.replaceWith(fresh);
    bindStepCard(fresh, steps);
    refreshBuilt();
  }

  async function refreshBuilt() {
    const name = S.reviewName === undefined ? defaultName() : S.reviewName;
    const id = S.reviewId === undefined ? defaultId() : S.reviewId;
    let result;
    try { result = await call("preview_workflow", id, name); }
    catch (error) { return; }
    S.built = result;
    renderChecks(result);
    renderJson(result);
  }

  function defaultName() {
    if (S.draftSource === "saved" && S.info) return S.reviewNameFixed || "";
    return S.recording && S.recording.origin ? "录制 " + S.recording.origin.replace(/^https?:\/\//, "") : "新工作流";
  }

  function defaultId() {
    if (S.reviewIdFixed) return S.reviewIdFixed;
    return "wf_" + Date.now().toString(36).slice(-6);
  }

  function renderChecks(result) {
    const problems = result.problems || [];
    const blocking = (result.check && result.check.blocking) || [];
    const warnings = (result.check && result.check.warnings) || [];
    const items = [];
    const steps = (S.draft && S.draft.steps) || [];
    const ids = steps.map((step) => step.id);
    items.push([ids.length === new Set(ids).size, "步骤 ID 唯一（" + ids.length + "/" + steps.length + "）"]);
    items.push([true, "start_url 与 origin 同源"]);
    const roleOk = steps.every((step) => (step.options || []).every((option) => option.strategy !== "role" || option.name));
    items.push([roleOk, "role 定位器均带可访问名称"]);
    const unchecked = steps.filter((step) => step.status === "unchecked").length;
    if (unchecked) items.push([false, unchecked + " 个步骤的定位器尚未在本页校验（可先保存，回放前建议校验）"]);
    problems.forEach((problem) => items.push([false, problem]));
    blocking.forEach((problem) => { if (!problems.includes(problem)) items.push([false, problem]); });
    warnings.forEach((warning) => items.push([null, warning]));
    const broken = items.filter((item) => item[0] === false).length;
    $("check-count").textContent = broken ? broken + " 项待修复" : "全部通过";
    $("rev-warn").textContent = broken ? broken + " 项待修复" : "";
    $("check-list").innerHTML = items.length ? items.map((item) =>
      '<div class="ck ' + (item[0] === true ? "ok" : item[0] === false ? "bad" : "warn") + '"><span class="mark">' +
      (item[0] === true ? "✓" : item[0] === false ? "✕" : "!") + "</span><span>" + esc(item[1]) + "</span></div>").join("")
      : '<div class="ck ok"><span class="mark">✓</span><span>没有步骤</span></div>';
    $("rev-save").disabled = broken > 0;
  }

  function renderJson(result) {
    const workflow = result.workflow || {};
    const id = workflow.id || "workflow";
    $("json-filename").textContent = id + ".workflow.json";
    const lines = jsonLines({
      schema_version: workflow.schema_version, id: workflow.id, name: workflow.name, origin: workflow.origin,
      start_url: workflow.start_url, steps: (workflow.steps || []).length
    });
    const steps = (workflow.steps || []).slice(0, 3).map((step) => JSON.stringify(step, null, 1)).join("\n");
    $("json-body").innerHTML = lines.concat(
      '<div class="ln"><span class="no">…</span><span class="jp">steps 共 ' + (workflow.steps || []).length + " 项，示例：</span></div>",
      steps.split("\n").map((line, index) => '<div class="ln"><span class="no">' + (index + 1) + "</span><span class=\"" +
        (/secret_ref/.test(line) ? "js hl-secret" : /"(text|hotkey|value|name)":/.test(line) ? "js" : /"(action|strategy)":/.test(line) ? "jk" : "jp") + '">' +
        esc(line) + "</span></div>").join("")).join("");
    const foot = $("json-foot");
    if ((result.problems || []).length || (result.check.blocking || []).length) {
      foot.className = "json-foot bad";
      foot.innerHTML = "✕ 未通过校验：" + esc((result.problems.concat(result.check.blocking))[0] || "") +
        '<span class="sub">Schema 校验：workflow.schema.json · 语义校验：步骤 ID · 同源 · role 名称</span>';
    } else {
      foot.className = "json-foot";
      foot.innerHTML = "✓ Schema 校验通过<span class=\"sub\">语义校验：步骤 ID · 同源 · role 名称 · secret_ref 无明文</span>";
    }
  }

  function jsonLines(object) {
    const out = [];
    let line = 1;
    Object.keys(object).forEach((key) => {
      const value = object[key];
      const kind = typeof value === "number" ? "jn" : typeof value === "string" ? "js" : "jp";
      out.push('<div class="ln"><span class="no">' + line++ + '</span><span><span class="jk">"' + esc(key) +
        '"</span>: <span class="' + kind + '">' + (typeof value === "string" ? '"' + esc(value) + '"' : esc(value)) + "</span></span></div>");
    });
    return out;
  }

  async function saveWorkflow() {
    if (!S.built) return;
    const payload = JSON.parse(JSON.stringify(S.built.workflow));
    payload.name = ($("review-name").value || "").trim() || payload.name || payload.id;
    payload.id = ($("review-id").value || "").trim() || payload.id;
    const result = await guarded("save_workflow", payload);
    if (result && !result.error) {
      toast("已保存 " + payload.id + "，共 " + payload.steps.length + " 步", "ok");
      if (result.needs_secret && result.needs_secret.length) {
        toast("秘密库还缺少：" + result.needs_secret.join("、") + "（运行前请填写）", "warn");
        show("secrets");
      } else {
        await refreshDeck();
        show("deck");
      }
    }
  }

  // ---------- run ----------
  async function runWorkflow(id, fromStep) {
    try {
      const started = await guarded("run_workflow", id, fromStep || "");
      if (started && !started.error) openRun(started.run_id, started.name, started);
    } catch (error) {
      const payload = error.payload || {};
      if (payload.missing_secret) {
        show("secrets");                            // 缺秘密值时直接把用户带到要填的那一屏
        toast("请填写：" + payload.missing_secret.join("、"), "warn");
      }
    }
  }

  async function runDraftNow() {
    try {
      const started = await guarded("run_draft_now");
      if (started && !started.error) openRun(started.run_id, started.name, started);
    } catch (error) {
      const payload = error.payload || {};
      if (payload.missing_secret) { show("secrets"); toast("请填写：" + payload.missing_secret.join("、"), "warn"); }
    }
  }

  function openRun(runId, name, started) {
    S.currentRun = runId;
    S.currentName = name || "";
    S.currentResumed = (started && started.resumed) || (started && started.from_step ? {
      from_step: started.from_step, skipped: started.skipped, total_steps: started.total_steps} : {});
    $("run-title").innerHTML = "运行 <span class=\"mono\">" + esc(runId) + "</span> · " + esc(name || "");
    show("run");
    pollRun();
    if (S.runTimer) clearInterval(S.runTimer);
    S.runTimer = setInterval(pollRun, 700);
  }

  async function pollRun() {
    if (!S.currentRun) return;
    const status = await call("run_status", S.currentRun);
    renderRun(status.run, status.detail);
    if (status.run.status !== "running" && S.runTimer) { clearInterval(S.runTimer); S.runTimer = null; }
  }

  function renderRun(run, detail) {
    $("run-chips").innerHTML = statusChip(detail.status_class === "rec" ? "running" : detail.status) +
      '<span class="chip mono">' + esc(detail.steps_run + "/" + detail.steps_total) + " 步</span>" +
      (detail.status !== "running" && detail.steps_verified !== undefined
        ? '<span class="chip mono">' + esc(detail.steps_verified + " 步已验证") + "</span>" : "") +
      (detail.status !== "running" && Number(detail.prior_runs || 0) > 0
        ? '<span class="chip mono warn">本次之前已跑过 ' + esc(detail.prior_runs) + " 次</span>" : "") +
      (detail.resumed && detail.resumed.from_step
        ? '<span class="chip mono warn">本次从 ' + esc(detail.resumed.from_step) + " 续跑 · 前 " +
          esc(detail.resumed.skipped) + " 步未执行</span>" : "");
    $("run-meta").innerHTML = "<b>" + esc(runStamp(detail.run_id)) + " · " + esc(clock(detail.started_at)) +
      "</b> · " + esc(detail.duration_s) + "s" + (detail.code ? " · " + esc(detail.code) : "");
    $("run-cancel").disabled = detail.status !== "running";
    $("run-again").disabled = detail.status === "running";
    const resume = $("run-resume");
    resume.hidden = !detail.resumable;
    if (detail.resumable) {
      resume.textContent = "从第 " + (detail.resume_index || "?") + " 步继续";
      resume.title = "这一步之前没有发出任何动作，从 " + detail.step_id + " 继续跑完剩下的步骤";
    }
    S.resumeStep = detail.resumable ? detail.step_id : "";
    S.resumeWorkflow = detail.workflow_id || "";
    S.detail = detail;
    $("timeline").innerHTML = (detail.timeline || []).map((step, index) =>
      '<div class="card tstep' + (step.state === "rec" ? " active" : "") + '"><span class="lamp ' + esc(step.state) + '"></span>' +
      '<span class="act ' + esc(step.action) + '">' + esc(step.action) + "</span>" +
      '<span class="t">' + esc(step.index + ". " + step.label) + "</span>" +
      '<span class="dur"><span class="status-chip ' + esc(step.state) + '"><span class="lamp ' + esc(step.state) + '"></span>' +
      esc(step.state_label) + '</span><span class="ms">' + esc(step.duration_s) + "s</span></span></div>").join("") ||
      '<div class="card empty">' + esc(timelineNote(detail)) + "</div>";
    const explanation = describeDetail(detail);
    const why = (detail.reason || "") + (detail.diagnostic ? "\n" + detail.diagnostic : "");
    $("run-detail").innerHTML = '<p class="d-expl">' + explanation + "</p>" +
      (why ? '<p class="d-why">' + esc(why) + "</p>" : "") +
      '<div class="d-kv"><span class="k">最后阶段</span><span class="v">' + esc((detail.events.slice(-1)[0] || {}).phase || "—") + "</span>" +
      '<span class="k">执行步骤</span><span class="v">' + esc(detail.steps_run + " / " + detail.steps_total) + "</span>" +
      '<span class="k">错误码</span><span class="v" style="color:' + (detail.code ? "var(--bad)" : "var(--ink3)") + '">' + esc(detail.code || "无") + "</span>" +
      '<span class="k">秘密值</span><span class="v">运行时读取，值不落盘</span></div>' +
      '<div class="d-actions"><button class="btn small" id="run-inspect">在浏览器中查看</button>' +
      '<button class="btn small" id="run-export">导出本次结果</button><span class="d-note">不会自动重试；重启后不自动续跑</span></div>';
    $("run-inspect").onclick = () => guarded("bring_to_front");
    $("run-export").onclick = () => guarded("export_run", S.currentRun).then((result) => { if (result && !result.error) toast("已导出：" + result.path, "ok"); });
    const recheck = $("run-recheck");
    if (detail.uncertain) {
      const stopped = (detail.timeline || []).filter((row) => row.step_id === detail.step_id)[0] || {};
      recheck.hidden = false;
      recheck.innerHTML = '<div class="rc-t">人工检查清单<span class="n" style="margin-left:auto;font-family:var(--font-mono);font-size:10px;color:var(--warn)">UNCERTAIN 之后</span></div>' +
        ['打开受控浏览器，确认停在的<b>第 ' + Number(detail.resume_index || 0) + ' 步「' +
          esc(stopped.label || detail.step_id || "") + "」</b>是否已经生效。",
          "若这一步<b>已经生效</b>：勾掉下面第二条，然后点「已检查：从这一步续跑」——" +
          "只跑剩下的步骤，前面已经做成的动作不会重做（重跑会把数据叠加一遍）。",
          "若这一步<b>没有生效</b>：勾掉下面第三条，到设置或业务系统里把留下的痕迹处理掉，再选择重跑方式。"
        ].map((text, index) =>
            '<div class="rc-item" data-check="' + index + '"><span class="box"></span><span>' + text + "</span></div>").join("") +
        '<div class="rc-go"' + (detail.manual_resume ? "" : " data-none=\"1\"") + '>' +
          '<button class="btn small primary" id="run-resume-checked" ' +
          (detail.manual_resume ? "disabled" : "") + ">" +
          (detail.manual_resume ? "已检查：从第 " + (detail.resume_index || "?") + " 步续跑" : "这一步不在工作流里，无法续跑") +
          "</button><span class=\"rc-tip\">勾满两条才放开：续跑由人做完检查后自己点，程序不替人确认。</span></div>" +
        '<p class="rc-note">程序不会根据日志自动续跑，也不会自动重试该动作；这一步的动作已经发出过一次，只可能执行了一遍。</p>';
      const box = recheck.querySelector("#run-resume-checked");
      recheck.querySelectorAll(".rc-item").forEach((item) => {
        item.onclick = () => {
          item.classList.toggle("done");
          if (box) box.disabled = recheck.querySelectorAll(".rc-item.done").length < 2;
        };
      });
      if (box && detail.manual_resume) {
        box.onclick = () => {
          if (box.disabled) return;
          S.resumeStep = detail.step_id;
          S.resumeWorkflow = detail.workflow_id || "";
          runWorkflow(detail.workflow_id, detail.step_id);
        };
      }
    } else { recheck.hidden = true; }
    $("j-filename").textContent = "run " + S.currentRun + " · journal.jsonl";
    $("j-body").innerHTML = (detail.events || []).map((event) =>
      '<div class="j-row' + (event.phase === "action_started" ? " hl-action" : "") + (event.phase === "uncertain" ? " hl-uncertain" : "") + '">' +
      '<span class="j-time">' + esc(clock(event.at)) + '</span><span class="j-phase">' + esc(event.phase) + "</span>" +
      '<span class="j-step">' + esc(event.step_id) + "</span>" +
      (event.code ? '<span class="j-code">' + esc(event.code) + "</span>" : "") +
      (event.reason ? '<span class="j-reason">' + esc(event.reason) + "</span>" : "") + "</div>").join("") ||
      '<div class="j-row">暂无阶段记录</div>';
    $("j-body").scrollTop = $("j-body").scrollHeight;
  }

  function describeDetail(detail) {
    const at = "第 " + Number(detail.resume_index || 0) + " 步「" + esc(detail.step_id || "") + "」";
    const why = detail.reason ? "：" + esc(detail.reason) : "。";
    if (detail.status === "running") return "回放进行中：每个动作<b>只发出一次</b>，阶段事件先落盘再执行。";
    if (detail.status === "uncertain") return "动作已发出，且<b>仅执行了 1 次</b>；停在 " + at + "，随后" +
      (detail.code === "TargetTimeout" ? "完成条件没有出现" : "驱动报告异常") + why +
      "<b>无法确认页面是否已经生效</b>，请按下面的人工检查清单处理。";
    if (detail.status === "failed") return "停在 " + at + "，在动作发生<b>之前</b>就停止了" +
      (detail.code ? "（错误码 " + esc(detail.code) + "）" : "") + why +
      "本次没有点击任何元素，也不会自动重试。" +
      (detail.resumable ? "修好这一步后用「从第 " + Number(detail.resume_index || 0) + " 步继续」，" +
        "不必整条重跑。" : "");
    if (detail.status === "cancelled") return "已按停止请求取消，停在 " + at + "：这一步的动作没有发出，" +
      "更早的步骤已经执行过（不会重做），之后的动作也不会发出。" +
      (detail.resumable ? "可以从这一步继续。" : "");
    if (detail.status === "completed_unverified") return "全部步骤执行完毕，其中 " + esc(detail.steps_unverified || 0) +
      " 步没有设完成条件，因此结果记为<b>完成 · 未验证</b>（不是失败，但程序没有证据）。";
    if (detail.status === "unknown") return "内存里没有这次运行，也没有读到它的终态日志；以下时间线只按日志里已有的事件显示。";
    return "全部步骤执行完毕，且设了完成条件的步骤都被验证通过。";
  }

  // 时间线是按工作流定义排的；定义读不到时（已删除，或本来就是没保存的草稿）不要谎报「已被删除」。
  function timelineNote(detail) {
    if (detail.workflow_found) return "这个工作流没有步骤。";
    return "读不到这次运行的工作流定义：它可能已被删除，或本来就是没保存的草稿 —— 下面只有阶段日志。";
  }

  // ---------- history / secrets / settings ----------
  async function refreshHistory() {
    const result = await call("list_runs");
    S.runs = result.runs || [];
    $("nav-runs").textContent = S.runs.length;
    renderRecent(S.runs, "history-list");
    const note = $("history-note");
    if (note) {                       // 日志轮转过的话，这一屏列出的就不是全部 —— 把那句话摊开在页面上
      const journal = result.journal || {};
      note.hidden = !journal.trimmed;
      note.textContent = journal.trimmed ? ("运行日志有上限：" + (journal.note || "更早的归档已被删除。")) : "";
    }
  }

  async function refreshSecrets() {
    const result = await call("secret_list");
    S.secrets = result.secrets || [];
    const needed = result.refs_needed || [];
    $("nav-secrets").textContent = S.secrets.length;
    const have = S.secrets.map((item) => item.ref);
    const usedBy = result.used_by || {};
    const usage = (ref) => (usedBy[ref] && usedBy[ref].length ? "被 " + usedBy[ref].join("、") + " 使用" : "还没有工作流在用");
    const rows = needed.filter((ref) => !have.includes(ref)).map((ref) =>
      '<div class="row"><span class="ref">' + esc(ref) + '</span><span class="len secref">' + esc(usage(ref)) + "</span>" +
      '<input type="password" data-secret="' + esc(ref) + '" placeholder="填入真实值，仅保存在本机（当前为空）">' +
      '<button class="btn small primary" data-save="' + esc(ref) + '">保存</button></div>');
    const stored = S.secrets.map((item) => '<div class="row"><span class="ref">' + esc(item.ref) +
      '</span><span class="len">' + (item.locked
        ? "值在本机读不出来（换过 Windows 账户或文件损坏）· 必须重新填写，运行前会被拦下"
        : "已存 " + esc(item.length) + " 字符") +
      '</span><span class="secref">' + esc(usage(item.ref)) + "</span>" +
      '<input type="password" data-secret="' + esc(item.ref) + '" placeholder="留空则不修改，输入新值可覆盖">' +
      '<button class="btn small primary" data-save="' + esc(item.ref) + '">更新</button>' +
      '<button class="btn small danger" data-forget="' + esc(item.ref) + '">删除</button></div>');
    $("secret-table").innerHTML = rows.concat(stored).join("") ||
      '<div class="empty">秘密库为空。录制到密码框时会自动生成引用名，保存后在这里填值。</div>';
    $("secret-table").querySelectorAll("[data-save]").forEach((button) => {
      button.onclick = async () => {
        const input = $("secret-table").querySelector('[data-secret="' + button.dataset.save + '"]');
        if (!input.value) { toast("值为空不会保存；请输入真实值", "warn"); return; }
        await guarded("secret_set", button.dataset.save, input.value);
        input.value = "";
        toast("已加密保存 " + button.dataset.save, "ok");
        refreshSecrets();
      };
    });
    $("secret-table").querySelectorAll("[data-forget]").forEach((button) => {
      button.onclick = async () => {
        if (!(await ask("删除 " + button.dataset.forget + " 引用？", "使用它的工作流在填回值之前将无法运行。"))) return;
        await guarded("secret_delete", button.dataset.forget);
        refreshSecrets();
      };
    });
  }

  async function refreshSettings() {
    const result = await call("settings_get");
    S.settings = result.settings || {};
    $("set-timeout").value = S.settings.default_timeout_s;
    $("set-proxy").value = S.settings.proxy_server || "";
    $("set-headless").classList.toggle("on", !!S.settings.headless);
    $("set-headless").querySelector(".txt").textContent = S.settings.headless ? "开启" : "关闭";
    $("set-keep").classList.toggle("on", !!S.settings.keep_browser_open);
    $("set-keep").querySelector(".txt").textContent = S.settings.keep_browser_open ? "开启" : "关闭";
    const mode = (S.frame && S.frame.mode) || S.settings.window_mode || "docked";
    document.querySelectorAll("#set-winmode button").forEach((button) => {
      button.classList.toggle("on", button.dataset.mode === mode);
    });
    const frame = S.frame || {};
    const off = Array.isArray(frame.content) && frame.content.some((n) => Math.abs(n) > 30000);
    const geom = $("win-geom");
    if (geom) {
      geom.textContent = frame.hwnd && !off
        ? "内容区 " + (frame.content || []).join("×") + " · 窗口 " + (frame.rect || []).join("×")
          + " · 标题条 " + frame.strip + "px · " + (frame.via || "")
        : "";
    }
    // 标题条高度量不出来时必须说「这一条测不了」，不能显示成 0 像素 ——
    // 0 在屏幕上就是「没有标题条」，而那时页面顶部其实正被浏览器自己的标题条切掉。
    const stripNote = frame.strip_note || (frame.pixel_ok === false
      ? "顶部还差 " + (frame.drift || "?") + " 像素，点「重新测量」校正" : "");
    $("win-line").textContent = !frame.hwnd ? "还没量到窗口句柄：点「重新测量」"
      : off ? "窗口现在不在屏幕上（被最小化或推到屏幕外）：点「重新测量」把它拉回来"
      : frame.mode === "fullscreen" ? "全屏：窗口铺满整块显示器，没有系统标题栏"
      : frame.mode === "free" ? "自由窗口：顶部 " + (frame.strip || "浏览器自带的") +
        " 像素是浏览器自带的标题条（可以自由移动的代价），界面里的无边框不适用于这一档"
      : "停靠无边框：浏览器自带的 " + (frame.strip || "?") + " 像素标题条整段在屏幕外，" +
        (frame.titlebar_hidden ? "页面从屏幕第一行开始" : "但标题条现在露出来了：点「重新测量」") +
        (stripNote ? " · " + stripNote : "");
    $("set-agent").classList.toggle("on", !!S.settings.agent_api);
    $("set-agent").querySelector(".txt").textContent = S.settings.agent_api ? "开启" : "关闭";
    $("agent-port-line").textContent = (S.agent && S.agent.url) || ("端口 " + (S.settings.agent_port || 8795));
    $("profile-path").textContent = result.profile_dir || "";
    $("data-path").textContent = result.data_dir || "";
    $("engine-line").textContent = result.engine || "未启动";
    const info = S.info || await call("app_info");
    $("about-line").textContent = "录放台 v" + (info.version || "—") + (info.bundled ? " · 已封装单进程" : " · 源码运行");
  }


  // ---------- Agent 接口这一屏：把「程序就是后端」讲清楚 ----------
  const RISK_TEXT = { read: ["read", "只读"], write: ["write", "写本机"], exec: ["exec", "真操作页面"] };

  async function refreshAgent() {
    const info = await call("agent_info");
    S.agent = info;
    $("nav-agent").textContent = info.ok ? (info.tools || 0) : "未运行";
    const state = (S.info && S.info.state) || {};
    $("agent-stats").innerHTML = [
      ["服务状态", info.ok ? "正在运行" : "未运行", info.ok ? "与界面同一个进程，Agent 做的每一步这里都看得见" : (info.error || "")],
      ["端点", info.ok ? info.url.replace(/^http:\/\//, "") : "—", "只监听 127.0.0.1，端口被占自动 +1"],
      ["鉴权", info.ok ? "本机令牌" : "—", info.ok
        ? ("非 GET 必带 x-agent-token · 已拦下 " + (info.denied || 0) + " 次外部调用") : ""],
      ["工具数", (info.tool_docs || []).length + " 个", "read " + countRisk(info, "read") + " · write " + countRisk(info, "write") + " · exec " + countRisk(info, "exec")],
      ["已被调用", info.ok ? String(info.calls || 0) + " 次" : "—", "从界面起到现在，Agent 发起的调用次数"]
    ].map((row) => '<div class="card stat"><p class="k">' + esc(row[0]) + '</p><p class="v">' + esc(String(row[1]))
      + '</p><p class="d">' + esc(row[2]) + "</p></div>").join("");

    $("agent-endpoint").innerHTML = info.ok
      ? '<div class="card-t">端点与自检</div><p class="s">健康检查、工具清单、清单里每个工具的入参 schema 都是现成的，'
      + '不需要读源码，也不需要第二个后台进程。</p>'
      + '<div class="agent-row"><span class="chip mono">' + esc(info.url) + '</span>'
      + '<button class="btn small" id="agent-copy-url">复制端点</button>'
      + '<span class="mut">GET /api/health · GET /api/agent/tools · POST /api/agent/tool</span></div>'
      + '<p class="composer-hint">MCP 客户端用 <span class="k mono">node agent/mcp-server.mjs</span>；'
      + '它会在服务没起来时按 <span class="k mono">agent/launch.json</span> 自己拉起。</p>'
      + '<p class="composer-hint">写操作要带本机令牌 <span class="k mono">x-agent-token</span>，'
      + '令牌文件：<span class="k mono">' + esc(info.token_file || "—") + '</span>。'
      + 'MCP 桥自动读它，不用手工传；网页一律调不动（没有通配 CORS，Host/Origin 也过不了）。</p>'
      : '<div class="card-t">端点与自检</div><p class="s bad">' + esc(info.error || "Agent 接口没有运行") + '</p>'
      + '<p class="composer-hint">这个开关在设置页；打开后重启程序即可让 Agent 直接调用本机工作流库。</p>';

    $("agent-tool-count").textContent = (info.tool_docs || []).length + " 个";
    $("agent-tools").innerHTML = (info.tool_docs || []).map((tool) => {
      const risk = RISK_TEXT[tool.risk] || ["read", "只读"];
      return '<div class="tool-row"><span class="risk ' + esc(risk[0]) + '" title="' + esc(risk[1]) + '">'
        + esc(risk[0]) + '</span><span class="tn mono">' + esc(tool.name) + '</span>'
        + '<span class="td">' + esc(tool.description) + "</span>"
        + '<span class="tp mono">' + esc((tool.required || []).length ? "必填 " + tool.required.join(" · ") : "无必填参数")
        + "</span></div>";
    }).join("") || '<p class="s">没有读到工具清单。</p>';

    $("agent-curl").textContent = info.curl || "";
    $("agent-mcp").textContent = info.mcp || "";
    const copy = (id, text) => {
      const button = $(id);
      if (!button) return;
      button.onclick = async () => {
        if (await copyText(text)) toast("已复制到剪贴板", "ok");
        else showText("复制这段内容", text);
      };
    };
    copy("agent-copy-http", info.curl || "");
    copy("agent-copy-mcp", info.mcp || "");
    copy("agent-copy-url", info.url || "");
  }

  function countRisk(info, risk) {
    return (info.tool_docs || []).filter((tool) => tool.risk === risk).length;
  }

  async function agentProbe() {
    const result = await guarded("agent_probe");
    if (!result || !result.ok) {
      toast("自检失败：" + ((result && (result.error || result.message)) || "没有响应"), "bad");
      return;
    }
    toast("自检通过：HTTP " + result.http + " · 本机 " + result.workflows + " 个工作流 · " + result.ms + "ms", "ok");
  }

  // ---------- 首屏三步上手：只说现在能做的下一步 ----------
  function renderOnboarding() {
    const box = $("onboarding");
    if (!box) return;
    if (S.settings && S.settings.onboarded) { box.hidden = true; return; }
    const state = (S.info && S.info.state) || {};
    const wf = S.workflows.length, runs = S.runs.length;
    const steps = [
      { n: 1, t: "录制一次操作", d: "在程序打开的受控浏览器里点一遍，界面把操作整理成可审阅的步骤。",
        done: wf > 0, now: !wf, action: ["去录制", "() => document.getElementById('record-url').focus()"] },
      { n: 2, t: "审阅并保存", d: "逐条看定位器与完成条件——多匹配会被标出来要你选一个，绝不退化成坐标点击。",
        done: wf > 0, now: !!state.state && state.state !== "idle" && state.state !== "running", action: ["看草稿", "draft"] },
      { n: 3, t: "运行，或交给 Agent", d: "点一下就按同样的步骤重放；也可以让任意 Agent 通过本机接口调用同一套能力。",
        // 只数「你自己存下来的工作流跑过没有」：自检留下的运行记录不算完成，否则第 1、2 步还空着
        // 第 3 步就先打了勾，读起来像反话。
        done: wf > 0 && runs > 0, now: wf > 0 && !runs, action: ["看 Agent 接口", "agent"] }
    ];
    box.hidden = false;
    box.innerHTML = '<div class="ob-head"><span class="t">三步就能用起来</span>'
      + '<span class="mut">每一步都在本机完成；这里不放示例数据，工作流和运行记录都来自你自己的操作。</span>'
      + '<button class="btn small" id="ob-dismiss">知道了</button></div>'
      + steps.map((step) => '<div class="ob-step' + (step.done ? " done" : "") + (step.now ? " now" : "") + '">'
        + '<span class="ob-n">' + (step.done ? "✓" : step.n) + '</span>'
        + '<div class="ob-b"><p class="t">' + esc(step.t) + '</p><p class="d">' + esc(step.d) + "</p></div>"
        + '<button class="btn small' + (step.now ? " primary" : "") + '" data-ob="' + step.n + '">'
        + esc(step.action[0]) + "</button></div>").join("");
    const dismiss = $("ob-dismiss");
    if (dismiss) dismiss.onclick = async () => {
      try { await guarded("settings_set", { onboarded: true }); } catch (error) { }
      S.settings.onboarded = true;
      box.hidden = true;
      toast("已收起上手引导（设置页里可以重新打开）", "info");
    };
    box.querySelectorAll("[data-ob]").forEach((button) => {
      button.onclick = () => {
        const which = button.dataset.ob;
        if (which === "1") { show("deck"); setTimeout(() => $("record-url").focus(), 120); }
        else if (which === "2") { if (S.draft) show("review"); else toast("还没有草稿：先完成第 1 步录制", "info"); }
        else show("agent");
      };
    });
  }

  // ---------- wiring ----------
  function wire() {
    wireWindow();
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        const box = $("modal");
        if (!box || box.hidden) return;
        if (!$("modal-cancel").hidden) $("modal-cancel").click();
        else if (!$("modal-close").hidden) $("modal-close").click();
        return;
      }
      const order = ["deck", "history", "secrets", "agent", "settings"];
      if (event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey) {
        const index = parseInt(event.key, 10);
        if (index >= 1 && index <= order.length) { event.preventDefault(); show(order[index - 1]); }
        return;
      }
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "s") {
        if (S.view === "review" && $("rev-save") && !$("rev-save").disabled) {
          event.preventDefault();
          saveWorkflow().catch(() => { });
        }
      }
    });
    let metaTimer = null;
    const onMeta = (event) => {
      if (event.target.id === "review-name") { S.reviewName = event.target.value; }
      else { S.reviewId = event.target.value; }
      clearTimeout(metaTimer);
      metaTimer = setTimeout(refreshBuilt, 400);
    };
    $("review-name").oninput = onMeta;
    $("review-id").oninput = onMeta;
    document.querySelectorAll(".nav-item[data-view]").forEach((item) => { item.onclick = () => show(item.dataset.view); });
    document.querySelectorAll("[data-view]").forEach((item) => {
      if (!item.classList.contains("nav-item")) item.onclick = () => show(item.dataset.view);
    });
    $("record-start").onclick = () => startRecording().catch(() => { });
    $("record-url").onkeydown = (event) => { if (event.key === "Enter") startRecording().catch(() => { }); };
    $("rec-stop").onclick = () => stopRecording().catch(() => { });
    $("rec-open-browser").onclick = () => guarded("bring_to_front");
    $("deck-refresh").onclick = () => refreshDeck().catch(() => { });
    $("rev-refresh").onclick = async () => {
      const result = await guarded("refresh_draft");
      if (result && !result.error) {
        openReview({ origin: S.draft.origin, start_url: S.draft.start_url, steps: result.steps }, S.draftSource);
        toast("已按当前页面重新计数：" + result.refreshed + " 个步骤", "ok");
      }
    };
    $("rev-save").onclick = () => saveWorkflow().catch(() => { });
    $("rev-trial").onclick = () => runDraftNow().catch(() => { });
    $("rev-export").onclick = () => {
      if (!S.built) return;
      const payload = JSON.stringify(S.built.workflow, null, 2);
      const write = navigator.clipboard && navigator.clipboard.writeText;
      if (write) {
        write.call(navigator.clipboard, payload)
          .then(() => toast("工作流 JSON 已复制到剪贴板", "ok"), () => showText("工作流 JSON", payload));
      } else {
        showText("工作流 JSON", payload);
      }
    };
    $("json-copy").onclick = $("rev-export").onclick;
    $("rev-add-step").onclick = async () => {
      const result = await guarded("add_wait_step");
      if (result && !result.error) { await reloadDraft(); toast("已添加等待步骤，填写定位器后校验", "ok"); }
    };
    $("run-cancel").onclick = async () => { const r = await guarded("cancel_run"); if (r && r.cancelled) toast(r.note, "warn"); };
    $("run-resume").onclick = async () => {
      if (!S.resumeWorkflow || !S.resumeStep) { toast("这次运行没有可续跑的步骤", "warn"); return; }
      if (!(await ask("从失败的步骤 " + S.resumeStep + " 继续？",
        "这一步之前没有发出任何动作，所以从它开始接着跑完剩下的步骤；已执行过的步骤不会重跑。"))) return;
      await runWorkflow(S.resumeWorkflow, S.resumeStep);
    };
    $("run-again").onclick = async () => {
      if (!S.runWorkflowId) { await runDraftNow().catch(() => { }); return; }
      // 重复运行会在真实系统里再叠一条数据，而浏览器自动化没法幂等 —— 所以这一眼必须先看历史。
      const detail = S.detail || {};
      const prior = Number(detail.prior_runs || 0);
      if (prior > 0) {
        const lines = ["这个工作流在本次之前已经记到 " + prior + " 次运行，最近一次是 " +
          runStamp(detail.run_id) + "（" + (detail.status_label || detail.status || "—") + "）。",
          "浏览器自动化按原样把动作再执行一遍：提交表单、点按钮这类步骤会在目标系统里再产生一条数据。",
          Number(detail.prior_completed || 0) > 0
            ? "只想补跑失败或未确认的那一步，请关窗后用「从第 N 步继续」；确实要再做一整遍再点确认。"
            : "确实要再跑一整遍请点确认。"];
        if (!(await ask("确认再运行一遍？", lines.join(" ")))) return;
      }
      await runWorkflow(S.runWorkflowId);
    };
    $("wf-list").onclick = (event) => handleWorkflowClick(event);
    $("recent-runs").onclick = (event) => handleRunClick(event);
    $("history-list").onclick = (event) => handleRunClick(event);
    $("secret-add").onclick = async () => {
      const ref = $("secret-ref").value.trim();
      const value = $("secret-value").value;
      if (!ref || !value) { toast("引用名和值都不能为空", "warn"); return; }
      await guarded("secret_set", ref, value);
      $("secret-ref").value = ""; $("secret-value").value = "";
      refreshSecrets();
    };
    $("set-save").onclick = async () => {
      const patch = {
        default_timeout_s: parseFloat($("set-timeout").value) || 10,
        proxy_server: $("set-proxy").value.trim(),
        headless: $("set-headless").classList.contains("on"),
        keep_browser_open: $("set-keep").classList.contains("on")
      };
      await guarded("settings_set", patch);
      toast("设置已保存", "ok");
      refreshSettings();
    };
    $("set-headless").onclick = () => toggleSwitch("set-headless");
    $("set-keep").onclick = () => toggleSwitch("set-keep");
    document.querySelectorAll("#set-winmode button").forEach((button) => {
      button.onclick = async () => {
        const wanted = button.dataset.mode;
        let result = null;
        try { result = await guarded("win_mode", wanted); }
        catch (error) { result = { ok: false, error: String((error && error.message) || error) }; }
        if (!result || !result.ok) {
          toast("切换失败：" + ((result && result.error) || "当前引擎不支持这个窗口模式")
            + "——界面仍可用；想换回去再点一次，或看设置页「当前窗口实测」", "bad");
          await syncFrame(6);
          refreshSettings().catch(() => { });
          return;
        }
        await syncFrame(6);
        toast("已切到" + (MODE_TEXT[result.mode] || result.mode) +
          (result.titlebar_hidden ? "：标题条已顶出屏幕" : "：顶部会露出浏览器自带的标题条"),
          result.titlebar_hidden ? "ok" : "warn");
        refreshSettings();
      };
    });
    $("agent-refresh").onclick = () => refreshAgent().catch(() => { });
    $("agent-probe").onclick = () => agentProbe().catch(() => { });
    $("set-agent").onclick = async () => {
      const on = !$("set-agent").classList.contains("on");
      await guarded("settings_set", { agent_api: on });
      S.settings.agent_api = on;
      refreshSettings().catch(() => { });
      toast(on ? "已允许 Agent 调用：下次启动程序时开始监听（现在先点下面的按钮立即生效）"
               : "已关闭：下次启动生效。界面本身不受影响", "info");
    };
    $("set-onboarding").onclick = async () => {
      try { await guarded("settings_set", { onboarded: false }); } catch (error) { }
      S.settings.onboarded = false;
      renderOnboarding();
      show("deck");
      toast("上手引导已回到工作台顶部", "ok");
    };
    $("set-winrefresh").onclick = async () => {
      const result = await guarded("win_remeasure");
      await syncFrame(6);
      refreshSettings().catch(() => { });
      if (result && result.pixel_ok === false) {
        toast("像素复核仍有 " + (result.drift || "?") + " 像素偏差：" + (result.place_error || "已按实际位置继续"), "warn");
      } else {
        toast("已重新测量：标题条 " + ((result && result.strip) || 0) + "px 已顶出屏幕，内容区 "
          + (((result && result.content) || []).join("×")), "ok");
      }
    };
    $("set-profile").onclick = async () => {
      if (!(await ask("清空受控浏览器的登录态与缓存？", "工作流、日志和秘密库不受影响。"))) return;
      const r = await guarded("reset_profile");
      toast(r && r.cleared ? "浏览器档案已清空" : "清空失败：" + (r && r.note || ""), r && r.cleared ? "ok" : "bad");
    };
    $("set-close-browser").onclick = async () => { await guarded("close_browser"); toast("受控浏览器已关闭", "ok"); };
    $("set-reveal").onclick = () => guarded("reveal_data_dir");
    $("set-selfcheck").onclick = runSelfCheck;
  }

  function toggleSwitch(id) {
    const node = $(id);
    const on = !node.classList.contains("on");
    node.classList.toggle("on", on);
    node.querySelector(".txt").textContent = on ? "开启" : "关闭";
  }

  function handleWorkflowClick(event) {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.run) { S.runWorkflowId = button.dataset.run; runWorkflow(button.dataset.run); }
    else if (button.dataset.del) {
      const id = button.dataset.del;
      ask("删除工作流 " + id + "？", "运行日志会保留；删除后无法从界面恢复。").then((yes) => {
        if (!yes) return;
        guarded("delete_workflow", id).then(refreshDeck);
      });
    } else if (button.dataset.export) guarded("export_workflow", button.dataset.export)
      .then((result) => { if (result && !result.error) toast("已导出：" + result.path, "ok"); });
    else if (button.dataset.edit) {
      guarded("open_in_editor", button.dataset.edit).then((result) => {
        if (!result || result.error) return;
        S.reviewNameFixed = result.workflow.name;
        S.reviewIdFixed = result.workflow.id;
        S.reviewName = result.workflow.name;
        S.reviewId = result.workflow.id;
        openReview(result.draft, "saved");
        show("review");
      });
    }
  }

  function handleRunClick(event) {
    const row = event.target.closest("[data-run]");
    if (!row) return;
    const run = S.runs.find((item) => item.run_id === row.dataset.run);
    S.runWorkflowId = run ? run.workflow_id : "";
    openRun(row.dataset.run, run ? run.name : "");
  }

  async function runSelfCheck() {
    const button = $("set-selfcheck");
    button.disabled = true;
    button.textContent = "自检运行中…";
    let result;
    try {
      result = await call("selfcheck");
    } finally {
      // 后端拒绝（比如正在录制）也要把按钮放回可点状态，否则它会永远停在「自检运行中…」
      button.disabled = false;
      button.textContent = "运行端到端自检";
    }
    if (result && result.error) return;
    const lines = (result.results || []).map((item) =>
      '<div class="ck ' + (item.ok ? "ok" : "bad") + '"><span class="mark">' + (item.ok ? "✓" : "✕") + "</span><span>" +
      esc(item.case) + " — " + esc(item.detail) + "</span></div>").join("");
    toast("自检完成：" + (result.passed || 0) + "/" + (result.results || []).length + " 项通过", result.failed ? "warn" : "ok");
    const card = document.createElement("div");
    card.className = "card set-row";
    card.innerHTML = '<div class="lab" style="width:auto"><p class="t">端到端自检结果</p><div id="check-results">' + lines + "</div></div>";
    const grid = document.querySelector(".set-grid");
    const old = $("check-results");
    if (old) old.closest(".set-row").remove();
    grid.appendChild(card);
  }

  window.__raPush = function (kind, payload) {
    if (kind === "state") { applyState(payload); if (payload.state !== "recording" && S.view === "recording") refreshRecording(); }
    if (kind === "run" && S.view === "run") pollRun();
    if (kind === "run_done") {
      S.runs = null;
      if (S.view !== "run") refreshDeck();
    }
    if (kind === "ready") {
      S.info = payload;
      $("nav-workflows").textContent = (payload.workflows || []).length;
      $("nav-runs").textContent = (payload.runs || []).length;
      $("nav-secrets").textContent = (payload.secrets || []).length;
    }
  };

  S.clockTimer = setInterval(() => {
    if (S.view === "recording") $("rec-time").textContent = hms(elapsedRecording());
    if (S.recording && S.recording.started_at) {
      $("state-clock").textContent = hms(elapsedRecording());
      const tbClock = $("tb-clock");
      if (tbClock) tbClock.textContent = hms(elapsedRecording());
    }
    if (S.view !== "recording" && S.recTimer) stopTickers();
  }, 1000);

  wire();
  syncFrame();
  show("deck");
  refreshDeck().catch(() => {
    $("state-text").textContent = "后端未连接";
    toast("后端桥接未就绪：请用封装后的程序启动", "bad");
  });
})();
