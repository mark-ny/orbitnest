import { api, setCsrf } from "./api.js";
import { brand, full, page, state } from "./core.js";
import { ago, confirmAction, dialog, h, icon, themeButton, toast } from "./ui.js";

function authLayout({ title, sub, form }) {
  const feature = (ico, text) => h("li", {}, h("span", { class: "ficon" }, icon(ico)), text);
  const hero = h("section", { class: "hero", "aria-label": "About OrbitNest" },
    brand("#/"),
    h("h2", { class: "headline" }, "Your environments.", h("br"), h("span", { text: "One secure workspace." })),
    h("p", { class: "lead", text: "Browsers, desktops and terminals that run in isolated containers and stream to any device." }),
    h("ul", { class: "features" },
      feature("shield", "Isolated by default, hardened containers"),
      feature("monitor", "Browser and desktop streaming, including Tor Browser"),
      feature("lock", "Self-hosted: your data stays on your machine")),
    h("div", { class: "orbits", "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("b")));
  const side = h("section", { class: "authside" },
    h("div", { class: "themebtn" }, themeButton()),
    h("div", { class: "authcard" },
      h("div", { class: "card stack" }, h("div", {}, h("h1", { text: title }), h("p", { class: "sub", text: sub })), form),
      h("div", { class: "secnote" }, icon("lock"), "Encrypted session, protected against CSRF and brute force")));
  full(h("main", { class: "auth" }, hero, side));
}

function authForm(title, sub, fields, submitText, onSubmit) {
  const err = h("div", { class: "err", role: "alert" });
  const caps = h("div", { class: "capswarn", "aria-live": "polite" });
  const inputs = fields.map((f) => h("input", { id: f.name, name: f.name, type: f.type || "text", autocomplete: f.autocomplete || "off", required: true, maxlength: f.maxlength || 1100, spellcheck: "false", autocapitalize: "none" }));
  const fieldEls = fields.map((f, i) => {
    let control = inputs[i];
    if (f.type === "password") {
      const toggle = h("button", { type: "button", class: "ghost", "aria-label": "Show or hide password", title: "Show or hide password" }, icon("eye"));
      toggle.addEventListener("click", () => { inputs[i].type = inputs[i].type === "password" ? "text" : "password"; });
      control = h("div", { class: "inputwrap" }, inputs[i], toggle);
      inputs[i].addEventListener("keyup", (e) => { caps.textContent = e.getModifierState?.("CapsLock") ? "Caps Lock is on" : ""; });
    }
    return h("div", { class: "field" }, h("label", { for: f.name, text: f.label }), control, f.hint ? h("div", { class: "hint", text: f.hint }) : null);
  });
  const btn = h("button", { class: "primary", type: "submit", text: submitText });
  const form = h("form", { class: "stack", novalidate: false }, ...fieldEls, caps, err, btn);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    btn.classList.add("busy"); btn.disabled = true;
    try {
      await onSubmit(Object.fromEntries(fields.map((f, i) => [f.name, inputs[i].value])));
    } catch (ex) {
      err.textContent = ex.message;
      btn.classList.remove("busy"); btn.disabled = false;
      inputs[inputs.length - 1].select?.();
    }
  });
  authLayout({ title, sub, form });
  inputs[0].focus();
}

export function showLogin(onDone) {
  const finish = async (res) => {
    setCsrf(res.csrf_token);
    state.user = res.user;
    await onDone();
  };
  const askCode = (creds) => authForm("Two-step verification", "Enter the 6-digit code from your authenticator app, or one of your recovery codes.", [
    { name: "code", label: "Authentication code", autocomplete: "one-time-code", maxlength: 32 },
  ], "Verify", async (v) => finish(await api("POST", "/auth/login", { json: { ...creds, code: v.code } })));
  authForm("Welcome back", "Sign in to your OrbitNest.", [
    { name: "username", label: "Username", autocomplete: "username", maxlength: 64 },
    { name: "password", label: "Password", type: "password", autocomplete: "current-password" },
  ], "Sign in", async (v) => {
    try {
      await finish(await api("POST", "/auth/login", { json: v }));
    } catch (ex) {
      if (ex.code !== "mfa_required") throw ex;
      askCode(v);
    }
  });
}

export function showSetup(status, onDone) {
  authForm("Set up OrbitNest", "First run: create the administrator account. This page disappears once setup is done.", [
    { name: "setup_token", label: "Setup token", type: "password", hint: "Printed to the server log and stored in the file \"setup-token\" inside the data directory. Run: python -m app setup-token" },
    { name: "username", label: "Administrator username", autocomplete: "username", maxlength: 64 },
    { name: "password", label: "Password", type: "password", autocomplete: "new-password", hint: "At least 12 characters." },
  ], "Create administrator", async (v) => {
    const res = await api("POST", "/auth/setup", { json: v });
    setCsrf(res.csrf_token);
    state.user = res.user;
    await onDone();
  });
}

function groups(secret) { return secret.replace(/(.{4})/g, "$1 ").trim(); }

function mfaCard(mfa, refresh) {
  const status = h("p", { class: "muted", text: mfa.enabled ? `Enabled. ${mfa.recovery_codes_left} recovery code(s) left.` : "Add a second step to sign-in using an authenticator app (Aegis, Google Authenticator, 1Password, ...)." });
  const action = mfa.enabled
    ? h("button", { class: "danger", text: "Turn off", onclick: async () => {
        const v = await dialog({ title: "Turn off two-step verification", confirmText: "Turn off", danger: true, fields: [
          { name: "password", label: "Password", type: "password", autocomplete: "current-password" },
          { name: "code", label: "Authentication or recovery code", autocomplete: "one-time-code", maxlength: 32 }] });
        if (!v) return;
        try { await api("POST", "/auth/mfa/disable", { json: v }); toast("Two-step verification turned off", "ok"); refresh(); } catch (e) { toast(e.message, "error"); }
      } })
    : h("button", { class: "primary", text: "Set up", onclick: async () => {
        const pw = await dialog({ title: "Confirm your password", confirmText: "Continue", fields: [
          { name: "password", label: "Password", type: "password", autocomplete: "current-password" }] });
        if (!pw) return;
        let begun;
        try { begun = await api("POST", "/auth/mfa/begin", { json: pw }); } catch (e) { toast(e.message, "error"); return; }
        const v = await dialog({ title: "Add to your authenticator", confirmText: "Verify and turn on",
          body: "In your authenticator app choose \"enter a setup key\", paste the key below (time-based, 6 digits), then type the code it shows.",
          fields: [
            { name: "key", label: "Setup key", value: groups(begun.secret) },
            { name: "code", label: "6-digit code", autocomplete: "one-time-code", maxlength: 16 }] });
        if (!v) return;
        try {
          const r = await api("POST", "/auth/mfa/confirm", { json: { code: v.code } });
          await dialog({ title: "Save your recovery codes", confirmText: "I saved them", wide: true,
            body: "Each code works once if you lose your phone. They are shown only now.",
            fields: [{ name: "codes", label: "Recovery codes", type: "textarea", value: r.recovery_codes.join("\n") }] });
          toast("Two-step verification is on", "ok");
          refresh();
        } catch (e) { toast(e.message, "error"); }
      } });
  return h("div", { class: "card stack" }, h("h2", { text: "Two-step verification" }), status, h("div", { class: "row" }, action));
}

export async function accountView() {
  const sessions = (await api("GET", "/auth/sessions")).items;
  const mfa = await api("GET", "/auth/mfa");
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
    mfaCard(mfa, refresh),
    h("div", { class: "card stack" }, h("h2", { text: "Active sessions" }), table));
}

export { confirmAction };
