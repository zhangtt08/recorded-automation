(() => {
  /** 录制桥接：只上报操作候选，由 Python 端复核来源并交给人工审阅。 */
  if (window.__raInstalled) return;
  window.__raInstalled = true;

  const MAX_BYTES = 4000;
  const TEXT_CAP = 120;
  const TYPE_DEBOUNCE = 350;
  const PASSWORD_KEYS = /password|passwd|pwd|secret|token|cvv|验证码|密码/i;

  const send = (payload) => {
    try {
      const clone = JSON.parse(JSON.stringify(payload));
      if (JSON.stringify(clone).length > MAX_BYTES) return;
      Promise.resolve(window.__raEmit(clone)).catch(() => {});
    } catch (err) { /* 单个事件失败不影响页面 */ }
  };

  const squash = (value) => (value || "").replace(/\s+/g, " ").trim();
  const textOf = (el) => squash(el.textContent).slice(0, TEXT_CAP);

  const INPUT_ROLES = {
    text: "textbox", email: "textbox", tel: "textbox", url: "textbox", search: "searchbox",
    number: "spinbutton", date: "textbox", time: "textbox", password: "textbox",
    checkbox: "checkbox", radio: "radio", range: "slider", file: "button",
    submit: "button", button: "button", reset: "button", image: "button", color: "textbox"
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return squash(explicit).split(" ")[0];
    const tag = el.tagName.toLowerCase();
    if (tag === "button") return "button";
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "select") return el.multiple ? "listbox" : "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") return INPUT_ROLES[(el.getAttribute("type") || "text").toLowerCase()] || "textbox";
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "img") return "img";
    if (el.isContentEditable) return "textbox";
    return "";
  };

  const labelText = (el) => {
    if (el.labels && el.labels.length) {
      return squash(Array.from(el.labels).map((label) => label.textContent).join(" ")).slice(0, TEXT_CAP);
    }
    const byId = el.getAttribute("aria-labelledby");
    if (byId) {
      const parts = byId.split(/\s+/).map((id) => {
        const node = document.getElementById(id);
        return node ? squash(node.textContent) : "";
      }).filter(Boolean);
      if (parts.length) return squash(parts.join(" ")).slice(0, TEXT_CAP);
    }
    return "";
  };

  const nameOf = (el, role) => {
    return squash(el.getAttribute("aria-label"))
      || labelText(el)
      || squash(el.getAttribute("placeholder"))
      || squash(el.getAttribute("title"))
      || ((role === "button" || role === "link" || role === "heading") ? textOf(el) : "")
      || "";
  };

  const testIdOf = (el) => {
    for (const key of ["data-testid", "data-test-id", "data-test", "data-qa", "data-cy"]) {
      const value = el.getAttribute(key);
      if (value) return value;
    }
    return "";
  };

  const cssPath = (el) => {
    const unique = (id) => {
      try { return document.querySelectorAll("#" + CSS.escape(id)).length === 1; } catch (err) { return false; }
    };
    if (el.id && unique(el.id)) return "#" + el.id;
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 8) {
      if (node.id && unique(node.id)) { parts.push("#" + node.id); break; }
      let selector = node.tagName.toLowerCase();
      const parent = node.parentElement;
      const sameTag = parent ? Array.from(parent.children).filter((child) => child.tagName === node.tagName) : [];
      if (sameTag.length > 1) selector += ":nth-of-type(" + (sameTag.indexOf(node) + 1) + ")";
      parts.push(selector);
      node = parent;
    }
    return parts.reverse().join(" > ");
  };

  const isSensitive = (el) => {
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (type === "password") return true;
    const autocomplete = (el.getAttribute("autocomplete") || "").toLowerCase();
    if (autocomplete === "current-password" || autocomplete === "new-password") return true;
    return PASSWORD_KEYS.test(`${el.name || ""} ${el.id || ""} ${labelText(el)} ${el.getAttribute("placeholder") || ""}`);
  };

  const editable = (el) => {
    const tag = el.tagName.toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
  };

  const describe = (el) => {
    const role = roleOf(el);
    const descriptor = {
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute("type") || "").toLowerCase(),
      role: role,
      name: nameOf(el, role),
      label: labelText(el),
      testId: testIdOf(el),
      text: textOf(el),
      css: cssPath(el),
      disabled: !!el.disabled,
      sensitive: isSensitive(el)
    };
    if (editable(el) && !descriptor.sensitive) {
      descriptor.currentValue = el.tagName.toLowerCase() === "select"
        ? (el.selectedOptions[0] ? el.selectedOptions[0].value : "")
        : (el.value === undefined ? "" : String(el.value).slice(0, TEXT_CAP * 4));
      if (el.tagName.toLowerCase() === "select") {
        descriptor.options = Array.from(el.options).slice(0, 20).map((option) => option.value);
      }
    }
    return descriptor;
  };

  const actionTarget = (node) => {
    let current = node;
    while (current && current.nodeType === 1) {
      if (editable(current)) return current;
      const tag = current.tagName.toLowerCase();
      if (tag === "button" || (tag === "a" && current.hasAttribute("href")) || current.getAttribute("role")
        || current.getAttribute("onclick") || tag === "label" || tag === "summary") return current;
      current = current.parentElement;
    }
    return node.nodeType === 1 ? node : null;
  };

  const frameMark = () => ({
    isMainFrame: window.top === window.self,
    frameName: window.name || "",
    url: location.href
  });

  const timers = new WeakMap();

  document.addEventListener("click", (event) => {
    const el = actionTarget(event.target);
    if (!el) return;
    send({ kind: "click", at: Date.now(), ...frameMark(), element: describe(el) });
  }, true);

  document.addEventListener("input", (event) => {
    const el = event.target;
    if (!el || !editable(el)) return;
    const pending = timers.get(el);
    if (pending) clearTimeout(pending);
    timers.set(el, setTimeout(() => {
      timers.delete(el);
      send({ kind: "input", at: Date.now(), ...frameMark(), element: describe(el) });
    }, TYPE_DEBOUNCE));
  }, true);

  const commit = (event) => {
    const el = event.target;
    if (!el || !editable(el)) return;
    const pending = timers.get(el);
    if (pending) { clearTimeout(pending); timers.delete(el); }
    send({ kind: "input", final: true, at: Date.now(), ...frameMark(), element: describe(el) });
  };
  document.addEventListener("change", commit, true);
  document.addEventListener("blur", commit, true);

  document.addEventListener("keydown", (event) => {
    const combo = event.ctrlKey || event.metaKey || event.altKey;
    const named = ["Enter", "Escape", "Tab"].includes(event.key);
    if (!combo && !named) return;
    if (combo && event.key.toLowerCase() === "t") return;  // 浏览器快捷键不属于页面
    const parts = [];
    if (event.ctrlKey) parts.push("Control");
    if (event.metaKey) parts.push("Meta");
    if (event.altKey) parts.push("Alt");
    if (event.shiftKey && (combo || event.key !== "Tab")) parts.push("Shift");
    if (!["Control", "Meta", "Alt", "Shift"].includes(event.key)) parts.push(event.key === " " ? "Space" : event.key);
    if (parts.length < 2 && !named) return;
    const focus = document.activeElement;
    send({
      kind: "hotkey", at: Date.now(), ...frameMark(),
      hotkey: parts.join("+"),
      element: focus && focus !== document.body ? describe(focus) : null
    });
  }, true);
})();
