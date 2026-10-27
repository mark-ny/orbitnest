// DOM helpers. Everything user- or workload-controlled goes through textContent: there is no
// innerHTML anywhere, which together with the CSP is the XSS defence.

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// append that skips null/false (Node.append would print "null")
export const add = (el, ...kids) => { for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) el.append(k instanceof Node ? k : document.createTextNode(String(k))); return el; };

export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); };

let toastBox;
export function toast(message, kind = "") {
  toastBox ||= document.body.appendChild(h("div", { class: "toasts", role: "status" }));
  const t = h("div", { class: `toast ${kind}`, text: message });
  toastBox.append(t);
  setTimeout(() => t.remove(), kind === "error" ? 7000 : 3500);
}

export function pill(status) {
  return h("span", { class: `pill ${status}`, text: status });
}

export function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n.toFixed(n < 10 ? 1 : 0)} ${u[i]}`;
}

export function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return isNaN(d) ? "-" : d.toLocaleString();
}

export function ago(iso) {
  if (!iso) return "-";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

// Modal form/confirm. fields: [{name,label,type,value,options,hint,required}]; returns values or null.
export function dialog({ title, body, fields = [], confirmText = "OK", danger = false, requireText = null, extra = null, wide = false }) {
  return new Promise((resolve) => {
    const err = h("div", { class: "err", role: "alert" });
    const inputs = {};
    const form = h("form", { method: "dialog" });
    form.append(h("h2", { text: title }));
    if (body) form.append(h("p", { class: "muted", text: body }));
    for (const f of fields) {
      let input;
      if (f.type === "select") {
        input = h("select", { id: `f-${f.name}` }, f.options.map((o) => h("option", { value: o.value, disabled: o.disabled, text: o.label }, null)));
        input.value = f.value ?? f.options.find((o) => !o.disabled)?.value ?? "";
      } else if (f.type === "checkbox") {
        input = h("input", { id: `f-${f.name}`, type: "checkbox", checked: !!f.value });
      } else if (f.type === "textarea") {
        input = h("textarea", { id: `f-${f.name}`, rows: 8, class: "mono" });
        input.value = f.value ?? "";
      } else {
        input = h("input", { id: `f-${f.name}`, type: f.type || "text", autocomplete: f.autocomplete || "off", maxlength: f.maxlength, required: f.required });
        input.value = f.value ?? "";
      }
      inputs[f.name] = input;
      form.append(h("div", { class: "field" },
        f.type === "checkbox" ? h("label", {}, input, " ", f.label) : [h("label", { for: `f-${f.name}`, text: f.label }), input],
        f.hint ? h("div", { class: "hint", text: f.hint }) : null));
    }
    let typed;
    if (requireText) {
      typed = h("input", { id: "f-confirm", autocomplete: "off" });
      form.append(h("div", { class: "field" }, h("label", { for: "f-confirm", text: `Type "${requireText}" to confirm` }), typed));
    }
    if (extra) form.insertBefore(extra.node, form.children[body ? 2 : 1] || null);
    const ok = h("button", { class: danger ? "danger" : "primary", type: "submit", text: confirmText });
    const cancel = h("button", { type: "button", text: "Cancel" });
    if (requireText) { ok.disabled = true; typed.addEventListener("input", () => { ok.disabled = typed.value !== requireText; }); }
    form.append(err, h("div", { class: "row end" }, cancel, ok));
    const dlg = h("dialog", { class: wide ? "wide" : "" }, form);
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    cancel.addEventListener("click", () => done(null));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(null); });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const out = {};
      for (const f of fields) out[f.name] = f.type === "checkbox" ? inputs[f.name].checked : inputs[f.name].value;
      if (extra) Object.assign(out, extra.read());
      done(out);
    });
    document.body.append(dlg);
    dlg.showModal();
    (Object.values(inputs)[0] || typed || ok).focus();
  });
}

export const confirmAction = (opts) => dialog({ confirmText: "Confirm", ...opts }).then((v) => v !== null);

// ---- icons (Feather-style strokes, built with the DOM; no innerHTML) -------------------------
const NS = "http://www.w3.org/2000/svg";
const ICONS = {
  globe: [["circle", { cx: 12, cy: 12, r: 10 }], ["path", { d: "M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" }]],
  monitor: [["rect", { x: 2, y: 3, width: 20, height: 14, rx: 2 }], ["path", { d: "M8 21h8M12 17v4" }]],
  terminal: [["path", { d: "M4 17l6-6-6-6M12 19h8" }]],
  code: [["path", { d: "M16 18l6-6-6-6M8 6l-6 6 6 6" }]],
  shield: [["path", { d: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" }]],
  onion: [["circle", { cx: 12, cy: 12, r: 10 }], ["circle", { cx: 12, cy: 12, r: 6.5 }], ["circle", { cx: 12, cy: 12, r: 3 }]],
  play: [["path", { d: "M6 4l14 8-14 8z" }]],
  stop: [["rect", { x: 5, y: 5, width: 14, height: 14, rx: 2 }]],
  refresh: [["path", { d: "M23 4v6h-6M20.5 15a9 9 0 1 1-2.1-9.4L23 10" }]],
  trash: [["path", { d: "M3 6h18M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" }]],
  folder: [["path", { d: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" }]],
  upload: [["path", { d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" }]],
  download: [["path", { d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" }]],
  user: [["path", { d: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" }], ["circle", { cx: 12, cy: 7, r: 4 }]],
  logout: [["path", { d: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" }]],
  sun: [["circle", { cx: 12, cy: 12, r: 5 }], ["path", { d: "M12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4" }]],
  moon: [["path", { d: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" }]],
  eye: [["path", { d: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" }], ["circle", { cx: 12, cy: 12, r: 3 }]],
  maximize: [["path", { d: "M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3" }]],
  clipboard: [["path", { d: "M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" }], ["rect", { x: 8, y: 2, width: 8, height: 4, rx: 1 }]],
  keyboard: [["rect", { x: 2, y: 4, width: 20, height: 16, rx: 2 }], ["path", { d: "M6 8h.01M10 8h.01M14 8h.01M18 8h.01M8 12h.01M12 12h.01M16 12h.01M7 16h10" }]],
  plus: [["path", { d: "M12 5v14M5 12h14" }]],
  search: [["circle", { cx: 11, cy: 11, r: 8 }], ["path", { d: "M21 21l-4.3-4.3" }]],
  check: [["path", { d: "M20 6L9 17l-5-5" }]],
  alert: [["path", { d: "M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01" }]],
  lock: [["rect", { x: 3, y: 11, width: 18, height: 11, rx: 2 }], ["path", { d: "M7 11V7a5 5 0 0 1 10 0v4" }]],
  layers: [["path", { d: "M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" }]],
  power: [["path", { d: "M18.4 6.6a9 9 0 1 1-12.8 0M12 2v10" }]],
  grid: [["rect", { x: 3, y: 3, width: 7, height: 7, rx: 1 }], ["rect", { x: 14, y: 3, width: 7, height: 7, rx: 1 }], ["rect", { x: 3, y: 14, width: 7, height: 7, rx: 1 }], ["rect", { x: 14, y: 14, width: 7, height: 7, rx: 1 }]],
  settings: [["circle", { cx: 12, cy: 12, r: 3 }], ["path", { d: "M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" }]],
  orbit: [["circle", { cx: 12, cy: 12, r: 3.2 }], ["ellipse", { cx: 12, cy: 12, rx: 10, ry: 4.5, transform: "rotate(-25 12 12)" }]],
};

export function icon(name, label) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  if (label) { svg.setAttribute("role", "img"); svg.setAttribute("aria-label", label); } else svg.setAttribute("aria-hidden", "true");
  for (const [tag, attrs] of ICONS[name] || []) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    svg.append(el);
  }
  return svg;
}

// Button with an icon and a label.
export const btn = (label, iconName, props = {}) => h("button", { type: "button", ...props }, iconName ? icon(iconName) : null, label);

// Marks a button busy (spinner) while `fn` runs.
export async function busy(button, fn) {
  button.classList.add("busy");
  button.disabled = true;
  try { return await fn(); } finally { button.classList.remove("busy"); button.disabled = false; }
}

// ---- theme -----------------------------------------------------------------------------------
export function applyTheme() {
  let t = null;
  try { t = localStorage.getItem("orbitnest-theme"); } catch { /* storage may be blocked */ }
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
}
export function toggleTheme() {
  const dark = document.documentElement.getAttribute("data-theme")
    ? document.documentElement.getAttribute("data-theme") === "dark"
    : !matchMedia("(prefers-color-scheme: light)").matches;
  const next = dark ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("orbitnest-theme", next); } catch { /* ignore */ }
  return next;
}
export const themeButton = () => {
  const b = h("button", { class: "ghost iconbtn themebtn-inner", type: "button", "aria-label": "Switch light or dark theme", title: "Switch theme" }, icon("sun"));
  b.addEventListener("click", toggleTheme);
  return b;
};
