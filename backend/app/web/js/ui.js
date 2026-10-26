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
export function dialog({ title, body, fields = [], confirmText = "OK", danger = false, requireText = null }) {
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
    const ok = h("button", { class: danger ? "danger" : "primary", type: "submit", text: confirmText });
    const cancel = h("button", { type: "button", text: "Cancel" });
    if (requireText) { ok.disabled = true; typed.addEventListener("input", () => { ok.disabled = typed.value !== requireText; }); }
    form.append(err, h("div", { class: "row end" }, cancel, ok));
    const dlg = h("dialog", {}, form);
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    cancel.addEventListener("click", () => done(null));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(null); });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const out = {};
      for (const f of fields) out[f.name] = f.type === "checkbox" ? inputs[f.name].checked : inputs[f.name].value;
      done(out);
    });
    document.body.append(dlg);
    dlg.showModal();
    (Object.values(inputs)[0] || typed || ok).focus();
  });
}

export const confirmAction = (opts) => dialog({ confirmText: "Confirm", ...opts }).then((v) => v !== null);
