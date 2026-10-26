import { api, filesUrl } from "./api.js";
import { canWrite, errorBox, loadCaps, modeBanner, navigate, page, poll, state } from "./core.js";
import { Terminal } from "./terminal.js";
import { add, ago, clear, confirmAction, dialog, fmtBytes, fmtTime, h, pill, toast } from "./ui.js";

const TRANSITIONAL = new Set(["provisioning", "starting", "stopping", "deleting"]);
const PROFILE_HELP = { small: "0.5 CPU, 512 MB RAM", medium: "1 CPU, 1 GB RAM", large: "2 CPU, 2 GB RAM" };

const run = async (label, fn) => {
  try { const r = await fn(); return r ?? true; } catch (e) { toast(`${label}: ${e.message}`, "error"); return false; }
};

// ---------------------------------------------------------------------------------------------
export async function workspacesView() {
  const [caps, templates, list] = await Promise.all([
    loadCaps(true), api("GET", "/templates"), api("GET", "/workspaces", { query: { limit: 200 } }),
  ]);
  const grid = h("div", { class: "grid" });
  const draw = (items) => {
    clear(grid);
    if (!items.length) grid.append(h("div", { class: "card muted", text: "No workspaces yet. Create one to get started." }));
    for (const w of items) {
      grid.append(h("article", { class: "card ws-card stack" },
        h("div", { class: "row between" }, h("h3", {}, h("a", { href: `#/w/${w.id}`, text: w.name })), pill(w.status)),
        h("dl", { class: "kv" },
          h("dt", { text: "Template" }), h("dd", { text: w.template_id }),
          h("dt", { text: "Resources" }), h("dd", { text: `${w.resource_profile} (${PROFILE_HELP[w.resource_profile] || ""})` }),
          h("dt", { text: "Isolation" }), h("dd", { text: w.isolation === "container" ? "Container" : "None (local process)" }),
          h("dt", { text: "Data" }), h("dd", { text: w.persistence })),
        w.status_reason ? h("div", { class: "banner warn", text: w.status_reason }) : null,
        h("div", { class: "row" }, h("a", { class: "btn", href: `#/w/${w.id}`, text: "Open" }))));
    }
  };
  draw(list.items);
  const create = h("button", { class: "primary", text: "New workspace", disabled: !canWrite(), onclick: () => createDialog(templates.items) });
  page("workspaces", h("div", { class: "row between" }, h("h1", { text: "Workspaces" }), create), modeBanner(), grid);
  poll(async () => draw((await api("GET", "/workspaces", { query: { limit: 200 } })).items), 4000);
  void caps;
}

async function createDialog(templates) {
  const v = await dialog({
    title: "New workspace", confirmText: "Create",
    fields: [
      { name: "template_id", label: "Template", type: "select", options: templates.map((t) => ({ value: t.id, disabled: !t.availability.creatable, label: t.availability.creatable ? `${t.name}` : `${t.name} (unavailable: ${(t.availability.reason || "").slice(0, 60)})` })) },
      { name: "name", label: "Name", maxlength: 64, required: true, hint: "Letters, digits, spaces, . _ -" },
      { name: "profile", label: "Size", type: "select", value: "small", options: Object.entries(PROFILE_HELP).map(([k, d]) => ({ value: k, label: `${k} - ${d}` })) },
      { name: "persistence", label: "Data", type: "select", value: "", options: [{ value: "", label: "Template default" }, { value: "persistent", label: "Persistent (kept when deleted unless you choose otherwise)" }, { value: "disposable", label: "Disposable (erased on stop)" }] },
      { name: "idle", label: "Stop after idle (minutes)", type: "number", value: "", hint: "Empty = template default, 0 = never." },
      { name: "ack", label: "I am authorized to use security-testing profiles (only needed for those)", type: "checkbox" },
    ],
  });
  if (!v) return;
  const body = { template_id: v.template_id, name: v.name.trim(), profile: v.profile };
  if (v.persistence) body.persistence = v.persistence;
  if (v.idle !== "") body.idle_timeout_minutes = Number(v.idle);
  if (v.ack) body.acknowledge_authorized_use = true;
  const created = await run("Create failed", () => api("POST", "/workspaces", { json: body }));
  if (created && created !== true) navigate(`#/w/${created.id}`);
}

// ---------------------------------------------------------------------------------------------
export async function workspaceView(id, tab = "overview") {
  let ws = await api("GET", `/workspaces/${encodeURIComponent(id)}`);
  const body = h("div", { class: "stack" });
  const head = h("div", { class: "stack" });
  let lastStatus = ws.status;

  const actions = () => {
    const s = ws.status;
    const w = canWrite() && s !== "deleted";
    const act = (label, fn, cls = "", enabled = true) => h("button", { class: cls, text: label, disabled: !w || !enabled, onclick: fn });
    const call = (verb) => async () => { await run(`${verb} failed`, () => api("POST", `/workspaces/${id}/${verb}`)); refresh(); };
    return h("div", { class: "row" },
      act("Start", call("start"), "primary", ["stopped", "failed"].includes(s)),
      act("Stop", call("stop"), "", ["running", "idle", "starting", "failed"].includes(s)),
      act("Restart", call("restart"), "", ["running", "idle"].includes(s)),
      act("Reset", () => resetFlow(), "", ["stopped", "failed"].includes(s)),
      act("Delete", () => deleteFlow(), "danger", !["deleting"].includes(s)));
  };

  const resetFlow = async () => {
    const v = await dialog({ title: "Reset workspace", body: "Re-creates the runtime from its template. Your files are kept unless you choose to wipe them.", confirmText: "Reset",
      fields: [{ name: "wipe", type: "checkbox", label: "Also erase all files in this workspace" }] });
    if (!v) return;
    const payload = {};
    if (v.wipe) {
      if (!(await confirmAction({ title: "Erase all files?", body: "This cannot be undone.", danger: true, requireText: ws.name }))) return;
      payload.wipe_data = true; payload.confirm_name = ws.name;
    }
    await run("Reset failed", () => api("POST", `/workspaces/${id}/reset`, { json: payload }));
    refresh();
  };

  const deleteFlow = async () => {
    const persistent = ws.persistence === "persistent";
    const v = await dialog({ title: `Delete "${ws.name}"?`, danger: true, confirmText: "Delete",
      body: persistent ? "The workspace is removed. By default its files stay on the host so nothing is lost by accident." : "Disposable workspaces lose their files when deleted.",
      fields: persistent ? [{ name: "purge", type: "checkbox", label: "Also permanently delete its files" }] : [] });
    if (!v) return;
    const query = {};
    if (v.purge) {
      if (!(await confirmAction({ title: "Permanently delete files?", body: "This cannot be undone.", danger: true, requireText: ws.name }))) return;
      query.delete_data = "true"; query.confirm_name = ws.name;
    }
    if (await run("Delete failed", () => api("DELETE", `/workspaces/${id}`, { query }))) navigate("#/");
  };

  const tabs = () => h("nav", { class: "tabs", "aria-label": "Workspace sections" },
    ...[["overview", "Overview"], ["files", "Files"], ["terminal", "Terminal"]].map(([k, label]) =>
      h("a", { href: `#/w/${id}/${k === "overview" ? "" : k}`, text: label, "aria-current": tab === k ? "page" : null })));

  const drawHead = () => {
    clear(head);
    add(head,
      h("div", { class: "row between" }, h("div", { class: "row" }, h("a", { href: "#/", text: "← Workspaces" }), h("h1", { text: ws.name }), pill(ws.status)), actions()),
      ws.status_reason ? h("div", { class: `banner ${ws.status === "failed" ? "danger" : "warn"}`, text: ws.status_reason }) : null,
      ws.isolation === "none" ? h("div", { class: "banner warn", text: "This workspace runs as a local process with no isolation." }) : null);
  };

  const refresh = async () => {
    ws = await api("GET", `/workspaces/${encodeURIComponent(id)}`);
    drawHead();
    if (tab === "overview") await drawOverview(); // status-dependent panes re-render; files/terminal keep state
    else if (lastStatus !== ws.status && tab === "terminal") terminalPane.statusChanged?.(ws);
    lastStatus = ws.status;
  };

  const overview = h("div", { class: "stack" });
  const drawOverview = async () => {
    const events = (await api("GET", `/workspaces/${id}/events`)).items;
    clear(overview);
    overview.append(
      h("div", { class: "card" }, h("dl", { class: "kv" },
        h("dt", { text: "Template" }), h("dd", { text: ws.template_id }),
        h("dt", { text: "Runtime" }), h("dd", { text: `${ws.runtime} (${ws.isolation === "container" ? "container" : "no isolation"})` }),
        h("dt", { text: "Size" }), h("dd", { text: `${ws.resource_profile}: ${ws.limits.cpu_millis / 1000} CPU, ${ws.limits.memory_mb} MB RAM, ${ws.limits.pids} processes, ${ws.limits.storage_mb} MB storage (reporting limit)` }),
        h("dt", { text: "Data" }), h("dd", { text: ws.persistence }),
        h("dt", { text: "Network" }), h("dd", { text: ws.network_policy }),
        h("dt", { text: "Idle timeout" }), h("dd", { text: ws.idle_timeout_minutes ? `${ws.idle_timeout_minutes} min` : "never" }),
        h("dt", { text: "Created" }), h("dd", { text: fmtTime(ws.created_at) }),
        h("dt", { text: "Last activity" }), h("dd", { text: ago(ws.last_activity_at) }),
        h("dt", { text: "Health" }), h("dd", { text: ws.health.ok === null ? "unknown" : ws.health.ok ? "ok" : "failing" }))),
      h("div", { class: "card" }, h("h2", { text: "History" }), h("div", { class: "tablewrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ...["When", "Event", "Change", "Note"].map((t) => h("th", { text: t })))),
        h("tbody", {}, events.map((e) => h("tr", {}, h("td", { text: ago(e.at), title: fmtTime(e.at) }), h("td", { text: e.event }),
          h("td", { text: e.from_status && e.to_status ? `${e.from_status} → ${e.to_status}` : e.to_status || "" }), h("td", { text: e.reason || "" }))))))));
  };

  const terminalPane = {};
  drawHead();
  page("workspaces", head, tabs(), body);
  if (tab === "files") { body.append(filesPane(ws)); }
  else if (tab === "terminal") { body.append(terminalTab(ws, terminalPane)); }
  else { body.append(overview); await drawOverview(); }
  poll(refresh, TRANSITIONAL.has(ws.status) ? 2000 : 6000);
}

// ---------------------------------------------------------------------------------------------
function filesPane(ws) {
  let cwd = "/";
  const box = h("div", { class: "card stack" });
  const join = (name) => (cwd === "/" ? `/${name}` : `${cwd}/${name}`);
  const base = `/workspaces/${encodeURIComponent(ws.id)}/files`;
  const writable = canWrite() && ws.status !== "deleted";

  const load = async (path = cwd) => {
    let data;
    try { data = await api("GET", base, { query: { path } }); } catch (e) { clear(box); box.append(errorBox(e)); return; }
    cwd = data.path;
    const parts = cwd.split("/").filter(Boolean);
    const crumbs = h("div", { class: "crumbs row" }, h("button", { class: "link", text: "/", onclick: () => load("/") }),
      ...parts.flatMap((p, i) => [h("button", { class: "link", text: p, onclick: () => load("/" + parts.slice(0, i + 1).join("/")) }), "/"]));
    const fileInput = h("input", { type: "file", multiple: true, hidden: true, onchange: () => upload([...fileInput.files]) });
    const toolbar = h("div", { class: "row" }, crumbs, h("div", { class: "grow" }),
      h("button", { text: "New folder", disabled: !writable, onclick: mkdir }),
      h("button", { class: "primary", text: "Upload", disabled: !writable, onclick: () => fileInput.click() }), fileInput);
    const rows = data.entries.map((e) => h("tr", {},
      h("td", {}, e.type === "dir" ? h("button", { class: "link", text: `${e.name}/`, onclick: () => load(join(e.name)) }) : h("span", { text: e.name }),
        e.type === "symlink" ? h("span", { class: "muted", text: " (symbolic link - not followed)" }) : null),
      h("td", { text: e.type === "file" ? fmtBytes(e.size) : "" }),
      h("td", { text: e.modified ? fmtTime(new Date(e.modified * 1000).toISOString()) : "" }),
      h("td", {}, h("div", { class: "file-actions row" },
        e.type === "file" ? h("a", { class: "btn", href: filesUrl(ws.id, join(e.name)), download: e.name, text: "Download" }) : null,
        h("button", { text: "Rename", disabled: !writable, onclick: () => rename(e) }),
        h("button", { class: "danger", text: "Delete", disabled: !writable, onclick: () => remove(e) })))));
    clear(box);
    add(box, toolbar,
      data.truncated ? h("div", { class: "banner warn", text: "This folder has more entries than can be shown." }) : null,
      h("div", { class: "tablewrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ...["Name", "Size", "Modified", ""].map((t) => h("th", { text: t })))),
        h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: 4, class: "muted", text: "Empty folder." }))))),
      h("p", { class: "hint", text: "Files are managed on the host and are available even while the workspace is stopped." }));
  };

  const mkdir = async () => {
    const v = await dialog({ title: "New folder", confirmText: "Create", fields: [{ name: "name", label: "Folder name", required: true, maxlength: 200 }] });
    if (v && (await run("Create folder failed", () => api("POST", `${base}/mkdir`, { json: { path: join(v.name.trim()) } })))) load();
  };
  const rename = async (e) => {
    const v = await dialog({ title: `Rename ${e.name}`, confirmText: "Rename", fields: [{ name: "name", label: "New name", value: e.name, required: true, maxlength: 200 }] });
    if (v && v.name !== e.name && (await run("Rename failed", () => api("POST", `${base}/move`, { json: { source: join(e.name), destination: join(v.name.trim()) } })))) load();
  };
  const remove = async (e) => {
    if (!(await confirmAction({ title: `Delete ${e.name}?`, body: e.type === "dir" ? "The folder and everything in it will be deleted." : "This cannot be undone.", danger: true, confirmText: "Delete" }))) return;
    if (await run("Delete failed", () => api("DELETE", base, { query: { path: join(e.name), recursive: e.type === "dir" ? "true" : "" } }))) load();
  };
  const upload = async (files) => {
    for (const f of files) {
      const path = join(f.name);
      try {
        try { await api("PUT", `${base}/content`, { query: { path }, body: f }); }
        catch (e) {
          if (e.code !== "conflict") throw e;
          if (!(await confirmAction({ title: `Replace ${f.name}?`, body: "A file with that name already exists.", confirmText: "Replace" }))) continue;
          await api("PUT", `${base}/content`, { query: { path, overwrite: "true" }, body: f });
        }
        toast(`Uploaded ${f.name}`, "ok");
      } catch (e) { toast(`${f.name}: ${e.message}`, "error"); }
    }
    load();
  };
  load();
  return box;
}

// ---------------------------------------------------------------------------------------------
function terminalTab(ws, pane) {
  const status = h("span", { class: "muted", text: "Not connected" });
  const host = h("div", { class: "term-wrap" });
  const connectBtn = h("button", { class: "primary", text: "Connect" });
  const note = ws.isolation === "none"
    ? h("div", { class: "banner warn", text: "This shell runs on the host as your own user, without isolation. Only administrators can open it." })
    : null;
  let sock = null, term = null, ping = null;

  const setStatus = (t) => { status.textContent = t; };
  const disconnect = () => { if (sock) sock.close(); };
  const connect = () => {
    if (sock) return;
    if (!["running", "idle"].includes(ws.status)) { toast("Start the workspace first.", "error"); return; }
    clear(host);
    term = new Terminal(host, {
      onData: (s) => sock?.readyState === 1 && sock.send(new TextEncoder().encode(s)),
      onResize: (c, r) => sock?.readyState === 1 && sock.send(JSON.stringify({ type: "resize", cols: c, rows: r })),
    });
    const [cols, rows] = term.size();
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    sock = new WebSocket(`${scheme}://${location.host}/api/v1/workspaces/${encodeURIComponent(ws.id)}/terminal?cols=${cols}&rows=${rows}`);
    sock.binaryType = "arraybuffer";
    setStatus("Connecting…");
    connectBtn.textContent = "Disconnect";
    sock.onopen = () => { ping = setInterval(() => sock?.readyState === 1 && sock.send('{"type":"ping"}'), 25000); };
    sock.onmessage = (ev) => {
      if (typeof ev.data !== "string") { term.write(new Uint8Array(ev.data)); return; }
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === "ready") { setStatus("Connected"); term.focus(); }
      else if (m.type === "exit") { setStatus(`Shell exited${m.code != null ? ` (code ${m.code})` : ""}`); }
      else if (m.type === "closed") { setStatus(`Closed: ${m.reason}`); }
      else if (m.type === "error") { setStatus(m.message); toast(m.message, "error"); }
    };
    sock.onclose = () => {
      clearInterval(ping); sock = null; connectBtn.textContent = "Connect";
      if (status.textContent === "Connected" || status.textContent === "Connecting…") setStatus("Disconnected");
    };
  };
  connectBtn.addEventListener("click", () => (sock ? disconnect() : connect()));
  pane.statusChanged = (w) => { ws = w; if (!["running", "idle"].includes(w.status) && sock) setStatus(`Workspace is ${w.status}`); };
  state.onLeave = () => { disconnect(); clearInterval(ping); term?.dispose(); };
  host.append(h("div", { class: "muted", text: "Click Connect to open a shell in this workspace." }));
  return h("div", { class: "stack" }, note, h("div", { class: "row" }, connectBtn, status), host);
}
