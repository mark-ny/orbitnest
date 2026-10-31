import { api } from "./api.js";
import { errorBox, loadCaps, page, state } from "./core.js";
import { installImageDialog } from "./images.js";
import { ago, clear, confirmAction, dialog, fmtTime, h, pill, toast, errorMessage } from "./ui.js";

const TABS = [["users", "Users"], ["policy", "Limits"], ["templates", "Templates"], ["audit", "Audit log"], ["system", "System"]];

function adminPage(tab, ...content) {
  page("admin", h("h1", { text: "Administration" }),
    h("nav", { class: "tabs", "aria-label": "Admin sections" }, ...TABS.map(([k, l]) => h("a", { href: `#/admin/${k}`, text: l, "aria-current": tab === k ? "page" : null }))),
    ...content);
}

const act = async (label, fn) => {
  try { await fn(); return true; } catch (e) { toast(`${label}: ${errorMessage(e)}`, "error"); return false; }
};

export async function adminView(tab = "users") {
  try {
    if (tab === "users") return await usersTab();
    if (tab === "policy") return await policyTab();
    if (tab === "templates") return await templatesTab();
    if (tab === "audit") return await auditTab();
    return await systemTab();
  } catch (e) {
    adminPage(tab, errorBox(e));
  }
}

async function usersTab() {
  const users = (await api("GET", "/users", { query: { limit: 200 } })).items;
  const reload = () => usersTab();
  adminPage("users",
    h("div", { class: "row between" }, h("h2", { text: "Users" }), h("button", { class: "primary", text: "Add user", onclick: async () => {
      const v = await dialog({ title: "Add user", confirmText: "Create", fields: [
        { name: "username", label: "Username", maxlength: 64, required: true },
        { name: "password", label: "Temporary password", type: "password", autocomplete: "new-password", hint: "At least 12 characters. Ask them to change it after signing in." },
        { name: "role", label: "Role", type: "select", value: "user", options: [{ value: "user", label: "User - can use workspaces" }, { value: "read_only", label: "Read only - can view, not change" }, { value: "admin", label: "Administrator" }] }] });
      if (v && (await act("Create failed", () => api("POST", "/users", { json: v })))) { toast("User created", "ok"); reload(); }
    } })),
    h("div", { class: "card tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["User", "Role", "Status", "Last sign-in", ""].map((t) => h("th", { text: t })))),
      h("tbody", {}, users.map((u) => h("tr", {},
        h("td", { text: u.username }),
        h("td", {}, h("select", { "aria-label": `Role of ${u.username}`, onchange: async (e) => {
          if (!(await act("Update failed", () => api("PATCH", `/users/${u.id}`, { json: { role: e.target.value } })))) e.target.value = u.role; else toast("Role updated", "ok");
        } }, ["admin", "user", "read_only"].map((r) => h("option", { value: r, selected: r === u.role, text: r })))),
        h("td", {}, pill(u.is_active ? "running" : "stopped"), " ", u.is_active ? "active" : "disabled"),
        h("td", { text: u.last_login_at ? ago(u.last_login_at) : "never" }),
        h("td", { class: "row" },
          h("button", { text: u.is_active ? "Disable" : "Enable", onclick: async () => {
            if (u.is_active && !(await confirmAction({ title: `Disable ${u.username}?`, body: "Their sessions end immediately.", confirmText: "Disable", danger: true }))) return;
            if (await act("Update failed", () => api("PATCH", `/users/${u.id}`, { json: { is_active: !u.is_active } }))) reload();
          } }),
          h("button", { text: "Reset password", onclick: async () => {
            try {
              const r = await api("POST", `/users/${u.id}/password-reset`);
              await dialog({ title: "Reset token", body: `Give this one-time token to ${u.username}. It is shown only now and expires at ${fmtTime(r.expires_at)}.`, confirmText: "Done", fields: [{ name: "t", label: "Token", value: r.token }] });
            } catch (e) { toast(e.message, "error"); }
          } }),
          h("button", { text: "Reset 2FA", onclick: async () => {
            if (!(await confirmAction({ title: "Reset two-step verification", body: `Remove ${u.username}'s authenticator and sign them out everywhere?`, danger: true }))) return;
            if (await act("Reset failed", () => api("DELETE", `/users/${u.id}/mfa`))) toast("Two-step verification removed", "ok");
          } }))))))));
}

async function policyTab() {
  const p = await api("GET", "/settings/policy");
  const f = (name, label, hint) => ({ name, label, type: "number", value: String(p[name]), hint });
  const inputs = {};
  const form = h("form", { class: "card stack" }, h("h2", { text: "Per-user limits and idle behaviour" }));
  const add = (name, label, hint, type = "number") => {
    inputs[name] = h("input", { id: name, type, value: String(p[name]) });
    form.append(h("div", { class: "field" }, h("label", { for: name, text: label }), inputs[name], hint ? h("div", { class: "hint", text: hint }) : null));
  };
  add("max_workspaces_per_user", "Workspaces per user", "Counts every workspace that is not deleted.");
  add("max_running_per_user", "Running workspaces per user", "Counts provisioning, starting, running and idle.");
  add("idle_mark_minutes", "Mark idle after (minutes)", "Cosmetic: shows the idle state.");
  inputs.idle_action = h("select", { id: "idle_action" }, ["stop", "mark"].map((v) => h("option", { value: v, selected: v === p.idle_action, text: v === "stop" ? "Stop workspaces past their idle timeout" : "Only mark them idle" })));
  form.append(h("div", { class: "field" }, h("label", { for: "idle_action", text: "When a workspace's idle timeout passes" }), inputs.idle_action));
  const err = h("div", { class: "err", role: "alert" });
  form.append(err, h("button", { class: "primary", type: "submit", text: "Save" }));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    const body = {};
    for (const [k, el] of Object.entries(inputs)) body[k] = k === "idle_action" ? el.value : Number(el.value);
    try { await api("PATCH", "/settings/policy", { json: body }); toast("Saved", "ok"); } catch (ex) { err.textContent = ex.message; }
  });
  void f;
  adminPage("policy", form);
}

async function templatesTab() {
  const [templates, caps] = await Promise.all([api("GET", "/templates"), loadCaps(true)]);
  const reload = () => templatesTab();
  adminPage("templates",
    h("div", { class: "row between" }, h("h2", { text: "Templates" }), h("button", { class: "primary", text: "Add custom template", onclick: async () => {
      const example = JSON.stringify({ id: "custom-example", name: "Example", description: "My image", category: "development", image: "docker.io/library/alpine:3.20", command: ["sleep", "infinity"], access: "terminal" }, null, 2);
      const v = await dialog({ title: "Add custom template", confirmText: "Add", body: "Validated strictly: no privileged options, host mounts or published ports can be requested. Runs in containers only.", fields: [{ name: "json", label: "Definition (JSON)", type: "textarea", value: example }] });
      if (!v) return;
      let payload; try { payload = JSON.parse(v.json); } catch { toast("That is not valid JSON.", "error"); return; }
      if (await act("Add failed", () => api("POST", "/templates", { json: payload }))) { toast("Template added", "ok"); reload(); }
    } })),
    h("div", { class: "card row between" },
      h("div", {}, h("strong", { text: "Tor gateway" }), h("div", { class: "muted", text: "Shared helper image: every workspace with \"Through Tor\" networking routes through one of these. Install it once; it is not a template." })),
      h("button", { text: "Install / rebuild Tor gateway image", onclick: async () => { await installImageDialog({ id: "tor-gateway", name: "Tor gateway", buildPath: "/images/tor-gateway/build" }); } })),
    h("div", { class: "card tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Template", "Access", "Availability on this host", ""].map((t) => h("th", { text: t })))),
      h("tbody", {}, templates.items.map((t) => h("tr", {},
        h("td", {}, h("strong", { text: t.name }), h("div", { class: "muted", text: t.id })),
        h("td", { text: t.access }),
        h("td", {}, t.availability.creatable ? h("span", { text: `Available (${t.availability.isolation === "container" ? "container" : "no isolation"})` }) : h("span", { class: "muted", text: t.availability.reason || "Unavailable" })),
        h("td", { class: "row" }, t.builtin ? [h("span", { class: "muted", text: "built-in" }), t.buildable ? h("button", { class: t.availability.creatable ? "" : "primary", text: t.availability.creatable ? "Rebuild image" : "Install image", onclick: async () => { await installImageDialog({ ...t, buildPath: `/templates/${t.id}/build` }); reload(); } }) : null] : [
          h("button", { text: t.enabled ? "Disable" : "Enable", onclick: async () => { if (await act("Update failed", () => api("PATCH", `/templates/${t.id}`, { json: { enabled: !t.enabled } }))) reload(); } }),
          h("button", { class: "danger", text: "Delete", onclick: async () => { if (await confirmAction({ title: `Delete ${t.name}?`, danger: true, confirmText: "Delete" }) && await act("Delete failed", () => api("DELETE", `/templates/${t.id}`))) reload(); } })]))))))); void caps;
}

async function auditTab() {
  const box = h("tbody");
  const more = h("button", { text: "Load more", hidden: true });
  const filter = h("input", { placeholder: "Filter by action prefix, e.g. workspace. or auth.", "aria-label": "Action prefix" });
  const outcome = h("select", { "aria-label": "Outcome" }, [["", "Any outcome"], ["success", "Success"], ["failure", "Failure"], ["denied", "Denied"]].map(([v, l]) => h("option", { value: v, text: l })));
  let cursor = null;
  const load = async (reset) => {
    if (reset) { cursor = null; clear(box); }
    try {
      const r = await api("GET", "/audit-events", { query: { limit: 50, cursor, action: filter.value.trim(), outcome: outcome.value } });
      for (const e of r.items) box.append(h("tr", {}, h("td", { text: fmtTime(e.occurred_at) }), h("td", { text: e.action }), h("td", { text: e.outcome }), h("td", { class: "mono", text: e.actor_username || e.actor_user_id || "-" }), h("td", { class: "mono", text: e.target_id || "" }), h("td", { class: "mono", text: e.ip || "" })));
      cursor = r.next_cursor; more.hidden = !cursor;
    } catch (e) { toast(e.message, "error"); }
  };
  filter.addEventListener("change", () => load(true));
  outcome.addEventListener("change", () => load(true));
  more.addEventListener("click", () => load(false));
  adminPage("audit", h("div", { class: "row" }, filter, outcome),
    h("div", { class: "card tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Time", "Action", "Outcome", "Actor", "Target", "Address"].map((t) => h("th", { text: t })))), box)), more);
  await load(true);
}

async function systemTab() {
  const [caps, report] = await Promise.all([loadCaps(true), api("GET", "/system/report")]);
  const LEVEL = { error: "danger", warning: "warn", info: "" };
  const problems = report.problems.length
    ? report.problems.map((p) => h("div", { class: `banner ${LEVEL[p.level] || ""}` }, h("div", {}, h("strong", { text: p.what }), h("div", { class: "muted", text: p.fix }))))
    : [h("div", { class: "banner" }, h("div", {}, h("strong", { text: "Everything looks fine." })))];
  const imageRow = (i) => h("tr", {}, h("td", { text: i.name }), h("td", { class: "mono", text: i.image }), h("td", {}, i.present === null ? "unknown" : i.present ? "installed" : "not installed"));
  const health = h("div", { class: "stack" },
    h("div", { class: "row between" }, h("h2", { text: "Health" }), h("span", { class: "muted", text: `OrbitNest ${report.version}${report.update.current ? ` (${report.update.current})` : ""}` })),
    ...problems,
    h("div", { class: "card tablewrap" }, h("h3", { text: "Images" }), h("table", {}, h("thead", {}, h("tr", {}, ...["Image", "Tag", "State"].map((t) => h("th", { text: t })))), h("tbody", {}, report.images.map(imageRow)))),
    h("div", { class: "card" }, h("dl", { class: "kv" },
      h("dt", { text: "Tor gateways" }), h("dd", { text: `${report.gateways.running} running, ${report.gateways.orphaned} leftover` }),
      h("dt", { text: "Last backup" }), h("dd", { text: report.backup.last_backup_days === null ? "never" : `${report.backup.last_backup_days} days ago` }))));
  const feature = ([name, f]) => h("tr", {}, h("td", { text: name.replaceAll("_", " ") }), h("td", {}, pill(f.state === "available" ? "running" : "stopped"), " ", f.state), h("td", { text: f.reason }), h("td", { text: f.remediation || "" }));
  adminPage("system", health,
    h("div", { class: "card" }, h("h2", { text: "This host" }), h("dl", { class: "kv" },
      h("dt", { text: "Mode" }), h("dd", { text: caps.mode }),
      h("dt", { text: "OS" }), h("dd", { text: `${caps.platform.os_family} ${caps.platform.os_release} (${caps.platform.architecture})` }),
      h("dt", { text: "Python" }), h("dd", { text: caps.platform.python_version }),
      h("dt", { text: "Storage" }), h("dd", { text: caps.storage.detail }),
      h("dt", { text: "Runtimes" }), h("dd", { text: caps.runtimes.map((r) => `${r.name}: ${r.available ? "available" : "not available"}`).join("; ") }))),
    h("div", { class: "card tablewrap" }, h("h2", { text: "Features" }), h("table", {}, h("thead", {}, h("tr", {}, ...["Feature", "State", "Why", "What to do"].map((t) => h("th", { text: t })))), h("tbody", {}, Object.entries(caps.features).map(feature)))));
  void state;
}
