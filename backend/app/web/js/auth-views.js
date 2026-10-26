import { api, setCsrf } from "./api.js";
import { bare, page, state } from "./core.js";
import { ago, confirmAction, dialog, h, toast } from "./ui.js";

function authForm(title, fields, submitText, onSubmit, intro) {
  const err = h("div", { class: "err", role: "alert" });
  const inputs = fields.map((f) => h("input", { id: f.name, name: f.name, type: f.type || "text", autocomplete: f.autocomplete || "off", required: true, maxlength: f.maxlength || 1100 }));
  const btn = h("button", { class: "primary", type: "submit", text: submitText });
  const form = h("form", { class: "card form-narrow stack" },
    h("h1", { text: title }), intro ? h("p", { class: "muted", text: intro }) : null,
    ...fields.map((f, i) => h("div", { class: "field" }, h("label", { for: f.name, text: f.label }), inputs[i], f.hint ? h("div", { class: "hint", text: f.hint }) : null)),
    err, btn);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    btn.disabled = true;
    try {
      await onSubmit(Object.fromEntries(fields.map((f, i) => [f.name, inputs[i].value])));
    } catch (ex) {
      err.textContent = ex.message;
      btn.disabled = false;
    }
  });
  bare(form);
  inputs[0].focus();
}

export function showLogin(onDone) {
  authForm("Sign in to OrbitNest", [
    { name: "username", label: "Username", autocomplete: "username", maxlength: 64 },
    { name: "password", label: "Password", type: "password", autocomplete: "current-password" },
  ], "Sign in", async (v) => {
    const res = await api("POST", "/auth/login", { json: v });
    setCsrf(res.csrf_token);
    state.user = res.user;
    await onDone();
  });
}

export function showSetup(status, onDone) {
  authForm("Set up OrbitNest", [
    { name: "setup_token", label: "Setup token", type: "password", hint: "Printed to the server log and stored in the file \"setup-token\" inside the data directory. Run: python -m app setup-token" },
    { name: "username", label: "Administrator username", autocomplete: "username", maxlength: 64 },
    { name: "password", label: "Password", type: "password", autocomplete: "new-password", hint: "At least 12 characters." },
  ], "Create administrator", async (v) => {
    const res = await api("POST", "/auth/setup", { json: v });
    setCsrf(res.csrf_token);
    state.user = res.user;
    await onDone();
  }, "First run: create the administrator account. This page disappears once setup is done.");
}

export async function accountView() {
  const sessions = (await api("GET", "/auth/sessions")).items;
  const refresh = () => accountView();
  const table = h("div", { class: "tablewrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ...["Signed in", "Last active", "Address", "Device", ""].map((t) => h("th", { text: t })))),
    h("tbody", {}, sessions.map((s) => h("tr", {},
      h("td", { text: ago(s.created_at) }), h("td", { text: ago(s.last_seen_at) }), h("td", { class: "mono", text: s.ip || "-" }),
      h("td", { text: (s.user_agent || "-").slice(0, 60) }),
      h("td", {}, s.current ? h("span", { class: "muted", text: "this session" }) : h("button", { text: "Revoke", onclick: async () => {
        try { await api("DELETE", `/auth/sessions/${s.id}`); toast("Session revoked", "ok"); refresh(); } catch (e) { toast(e.message, "error"); }
      } })))))));
  page("account",
    h("h1", { text: "Account" }),
    h("div", { class: "card stack" }, h("h2", { text: state.user.username }),
      h("p", { class: "muted", text: `Role: ${state.user.role}` }),
      h("div", { class: "row" },
        h("button", { text: "Change password", onclick: async () => {
          const v = await dialog({ title: "Change password", confirmText: "Change", fields: [
            { name: "current_password", label: "Current password", type: "password", autocomplete: "current-password" },
            { name: "new_password", label: "New password", type: "password", autocomplete: "new-password", hint: "At least 12 characters. Other sessions are signed out." }] });
          if (!v) return;
          try { await api("POST", "/auth/password", { json: v }); toast("Password changed", "ok"); } catch (e) { toast(e.message, "error"); }
        } }),
        h("button", { onclick: async () => {
          try { await api("POST", "/auth/logout"); } catch { /* already signed out */ }
          location.hash = "#/"; location.reload();
        }, text: "Sign out" }))),
    h("div", { class: "card stack" }, h("h2", { text: "Active sessions" }), table));
}

export { confirmAction };
