import { api, filesUrl } from "./api.js";
import { canWrite, errorBox, loadCaps, modeBanner, navigate, page, poll, state } from "./core.js";
import { Terminal } from "./terminal.js";
import { screenTab } from "./stream.js";
import { installImageDialog } from "./images.js";
import { add, ago, btn, busy, clear, confirmAction, dialog, fmtBytes, fmtTime, h, icon, pill, toast } from "./ui.js";

const TRANSITIONAL = new Set(["provisioning", "starting", "stopping", "deleting"]);
const PROFILE_HELP = { small: "0.5 CPU, 512 MB RAM", medium: "1 CPU, 1 GB RAM", large: "2 CPU, 2 GB RAM" };

const run = async (label, fn) => {
  try { const r = await fn(); return r ?? true; } catch (e) { toast(`${label}: ${e.message}`, "error"); return false; }
};

// ---------------------------------------------------------------------------------------------
const TPL_ICON = { "browser-tor": ["onion", "tor"], browser: ["globe", "browser"], desktop: ["monitor", "desktop"], terminal: ["terminal", "terminal"], developer: ["code", "developer"], security: ["shield", "security"], custom: ["layers", ""] };
export function tplIcon(t) {
  const [name, cls] = TPL_ICON[t?.id] || TPL_ICON[t?.category] || ["layers", ""];
  return h("span", { class: `tplicon ${cls}` }, icon(name));
}

export async function workspacesView() {
  const [caps, templates, list] = await Promise.all([
    loadCaps(true), api("GET", "/templates"), api("GET", "/workspaces", { query: { limit: 200 } }),
  ]);
  const byId = Object.fromEntries(templates.items.map((t) => [t.id, t]));
  let items = list.items;
  let filter = "all";
  let query = "";
  const grid = h("div", { class: "grid" });
  const stats = h("div", { class: "stats" });
  const create = h("button", { class: "primary", disabled: !canWrite(), onclick: () => createDialog(templates.items) }, icon("plus"), "New workspace");

  const quick = (w, verb, ico, label) => h("button", { class: "ghost", title: label, "aria-label": `${label} ${w.name}`, disabled: !canWrite(), onclick: async (e) => {
    await busy(e.currentTarget, () => run(`${label} failed`, () => api("POST", `/workspaces/${w.id}/${verb}`)));
    refresh();
  } }, icon(ico), label);

  const draw = () => {
    clear(grid);
    const visible = items.filter((w) => (filter === "all" || (filter === "running" ? ["running", "idle", "starting", "provisioning"].includes(w.status) : ["stopped", "failed"].includes(w.status)))
      && (!query || `${w.name} ${w.template_id}`.toLowerCase().includes(query)));
    if (!items.length) {
      grid.append(h("div", { class: "card empty", style: null },
        h("div", { class: "orbit-art", "aria-hidden": "true" }, h("i"), h("i"), h("b")),
        h("h2", { text: "Launch your first workspace" }),
        h("p", { class: "muted", text: "Pick a browser, a Linux desktop, Tor Browser or a developer environment. Each one runs in its own isolated container." }),
        h("button", { class: "primary", onclick: () => createDialog(templates.items), disabled: !canWrite() }, icon("plus"), "New workspace")));
    } else if (!visible.length) {
      grid.append(h("div", { class: "card muted", text: "No workspaces match." }));
    }
    for (const w of visible) {
      const t = byId[w.template_id];
      const live = ["running", "idle"].includes(w.status);
      grid.append(h("article", { class: "card ws-card" },
        h("div", { class: "row", style: null }, tplIcon(t), h("div", { class: "grow" }, h("h3", {}, h("a", { href: `#/w/${w.id}`, text: w.name })), h("div", { class: "muted", text: t?.name || w.template_id })), pill(w.status)),
        h("div", { class: "chiprow" },
          h("span", { class: `tag ${w.isolation === "container" ? "iso" : "noiso"}`, text: w.isolation === "container" ? "Container" : "No isolation" }),
          h("span", { class: "tag", text: w.resource_profile }), h("span", { class: "tag", text: w.persistence })),
        w.status_reason ? h("div", { class: "banner warn", text: w.status_reason }) : null,
        h("div", { class: "row actions" },
          live && t?.access === "stream" ? h("a", { class: "btn", href: `#/w/${w.id}/screen` }, icon("monitor"), "Open screen") : null,
          live && t?.access !== "stream" ? h("a", { class: "btn", href: `#/w/${w.id}/terminal` }, icon("terminal"), "Terminal") : null,
          ["stopped", "failed"].includes(w.status) ? quick(w, "start", "play", "Start") : null,
          live ? quick(w, "stop", "stop", "Stop") : null)));
    }
    const running = items.filter((w) => ["running", "idle"].includes(w.status)).length;
    clear(stats);
    add(stats,
      h("div", { class: "card stat" }, h("div", { class: "label", text: "Running" }), h("div", { class: "value", text: String(running) }), h("div", { class: "sub", text: "workspaces live now" })),
      h("div", { class: "card stat" }, h("div", { class: "label", text: "Total" }), h("div", { class: "value", text: String(items.length) }), h("div", { class: "sub", text: "of your workspaces" })),
      h("div", { class: "card stat" }, h("div", { class: "label", text: "Platform" }), h("div", { class: "value", style: null, text: caps.mode.replace(/-/g, " ") }), h("div", { class: "sub", text: caps.features?.container_workspaces?.state === "available" ? "container isolation active" : "limited, see notice" })));
  };
  const refresh = async () => { items = (await api("GET", "/workspaces", { query: { limit: 200 } })).items; draw(); };

  const chips = h("div", { class: "chips", role: "group", "aria-label": "Filter" }, ...[["all", "All"], ["running", "Running"], ["stopped", "Stopped"]].map(([k, label]) => {
    const c = h("button", { class: "chip", type: "button", text: label, "aria-pressed": k === filter ? "true" : "false", onclick: () => { filter = k; for (const x of chips.children) x.setAttribute("aria-pressed", x === c ? "true" : "false"); draw(); } });
    return c;
  }));
  const search = h("div", { class: "search" }, icon("search"), h("input", { type: "search", placeholder: "Search workspaces", "aria-label": "Search workspaces", oninput: (e) => { query = e.target.value.trim().toLowerCase(); draw(); } }));
  draw();
  page("workspaces", h("div", { class: "row between" }, h("div", {}, h("h1", { text: "Workspaces" }), h("p", { class: "muted", style: null, text: "Isolated environments that run on this host." })), create), modeBanner(), stats,
    h("div", { class: "row between" }, search, chips), grid);
  poll(refresh, 4000);
}

const RANK = { small: 0, medium: 1, large: 2 };

function privacyTag(t) {
  if (t.privacy === "tor") return h("span", { class: "tag tor", text: "Tor Browser" });
  if ((t.allowed_egress || []).includes("tor")) return h("span", { class: "tag tor", text: t.default_egress === "tor" ? "Routed through Tor" : "Tor available" });
  if (t.privacy === "direct") return h("span", { class: "tag direct", text: "Uses your real IP" });
  return null;
}

async function createDialog(templates) {
  let list = templates;
  const isAdmin = state.user?.role === "admin";
  let chosen = list.find((t) => t.availability.creatable)?.id || list[0]?.id;
  const current = () => list.find((t) => t.id === chosen);
  const cards = [];
  const grid = h("div", { class: "tplgrid", role: "radiogroup", "aria-label": "Template" });
  const detail = h("div", { class: "tpldetail" });
  const sizeSel = h("select", { id: "f-size" });
  const sizeWarn = h("div", { class: "banner warn", hidden: true });
  const rulesBox = h("input", { type: "checkbox", id: "f-rules" });
  const capBoxes = new Map();
  const egressSel = h("select", { id: "f-egress" });
  const egressWarn = h("div", { class: "banner warn", hidden: true });
  const netBanner = h("div", {});
  const EGRESS_LABEL = { tor: "Through Tor (hides your IP from sites you visit)", direct: "Direct (this computer's real IP, no Tor)" };

  const paintDetail = () => {
    const t = current();
    clear(detail);
    capBoxes.clear();
    rulesBox.checked = false;
    if (!t) return;
    clear(sizeSel);
    for (const [k, d] of Object.entries(PROFILE_HELP)) {
      sizeSel.append(h("option", { value: k, text: `${k} - ${d}`, disabled: !!t.min_profile && RANK[k] < RANK[t.min_profile] }));
    }
    sizeSel.value = t.default_profile;
    const checkSize = () => {
      const low = t.recommended_profile && RANK[sizeSel.value] < RANK[t.recommended_profile];
      sizeWarn.hidden = !low;
      sizeWarn.textContent = low ? `This may feel slow. ${t.name} runs best at "${t.recommended_profile}" (${PROFILE_HELP[t.recommended_profile]}) or above.` : "";
    };
    sizeSel.onchange = checkSize;
    checkSize();
    add(detail, h("div", { class: "field" }, h("label", { for: "f-size", text: "Size" }), sizeSel), sizeWarn);
    clear(egressSel);
    clear(netBanner);
    egressWarn.hidden = true;
    const choices = t.allowed_egress || [];
    const paintNetBanner = () => {
      clear(netBanner);
      if (t.privacy === "tor") {
        // Tor Browser runs its own Tor process; it never joins the shared gateway.
        add(netBanner, h("div", { class: "banner" }, icon("onion"), h("div", {}, h("strong", { text: "Tor Browser. " }), "Web traffic goes through Tor, so sites see a Tor exit instead of your IP. Data is erased when the workspace stops. This is not Tails: your internet provider can see that you use Tor, and accounts you sign in to identify you.")));
      } else if (egressSel.value === "tor") {
        add(netBanner, h("div", { class: "banner" }, icon("onion"), h("div", {}, h("strong", { text: "Routed through Tor. " }), "This workspace's network goes through a per-workspace Tor gateway, so sites it reaches see a Tor exit, not this computer's IP. It is an ordinary browser otherwise, not Tor Browser: its fingerprint can still differ, and anything you sign in to still identifies you.")));
      } else {
        add(netBanner, h("div", { class: "banner warn" }, icon("shield"), h("div", {}, h("strong", { text: "Not anonymous. " }), "Sites you visit here see this computer's real IP address and internet provider, exactly as in a normal browser. This only separates the browser from your files. For privacy, choose \"Through Tor\" above or use the Tor Browser template.")));
      }
    };
    if (choices.length > 1) {
      for (const e of choices) egressSel.append(h("option", { value: e, text: EGRESS_LABEL[e] || e }));
      egressSel.value = t.default_egress || choices[0];
      const checkEgress = () => {
        const noGateway = egressSel.value === "tor" && t.tor_gateway_installed === false;
        egressWarn.hidden = !noGateway;
        egressWarn.textContent = noGateway ? "The Tor gateway image is not installed. An administrator can install it from Admin, Templates; until then this workspace will fail to start on Tor." : "";
        paintNetBanner();
      };
      egressSel.onchange = checkEgress;
      checkEgress();
      add(detail, h("div", { class: "field" }, h("label", { for: "f-egress", text: "Network" }), egressSel), egressWarn);
    } else if (choices.length === 1) {
      egressSel.append(h("option", { value: choices[0] }));
      egressSel.value = choices[0];
      paintNetBanner();
    } else {
      paintNetBanner();
    }
    add(detail, netBanner);
    if (t.authorized_use_only) {
      add(detail, h("div", { class: "card stack" },
        h("strong", { text: "Authorized security testing only" }),
        h("ol", { class: "rules" }, (t.rules || []).map((r) => h("li", { text: r }))),
        h("label", { class: "capopt", for: "f-rules" }, rulesBox, h("span", { text: "I have read and accept these rules." })),
        (t.optional_capabilities || []).length ? h("div", { class: "stack" },
          h("strong", { text: "Extra Linux capabilities (optional)" }),
          h("small", { class: "muted", text: "Tick only what you need. Privileged mode, host networking and device access are never available." }),
          t.optional_capabilities.map((c) => {
            const box = h("input", { type: "checkbox", id: `cap-${c}` });
            capBoxes.set(c, box);
            return h("label", { class: "capopt", for: `cap-${c}` }, box, h("span", {}, h("code", { text: c }), h("small", { text: t.capability_help?.[c] || "" })));
          })) : null));
    }
  };

  const drawGrid = () => {
    clear(grid);
    cards.length = 0;
    for (const t of list) {
      const ok = t.availability.creatable;
      const needsBuild = !ok && t.buildable && /not built/i.test(t.availability.reason || "");
      const c = h("button", { type: "button", class: "tplcard", role: "radio", "aria-checked": String(t.id === chosen), "aria-disabled": String(!ok), title: ok ? "" : t.availability.reason || "Unavailable",
        onclick: async () => {
          if (!ok) {
            if (needsBuild && isAdmin) { if (await installImageDialog({ ...t, buildPath: `/templates/${t.id}/build` })) { await reloadTemplates(); } else { await reloadTemplates(); } return; }
            toast(needsBuild ? "This image is not installed yet. Ask an administrator to install it (Admin, Templates)." : t.availability.reason || "Unavailable", "error");
            return;
          }
          chosen = t.id;
          for (const x of cards) x.setAttribute("aria-checked", String(x === c));
          paintDetail();
        } },
        tplIcon(t), h("span", {}, h("span", { class: "t", text: t.name }),
          h("span", { class: "d", text: ok ? t.description : (needsBuild ? (isAdmin ? "Image not installed. Click to install it." : "Image not installed. Ask an administrator.") : `Unavailable: ${(t.availability.reason || "").slice(0, 90)}`) }),
          ok ? h("span", { class: "tags" }, privacyTag(t), t.authorized_use_only ? h("span", { class: "tag lab", text: "Authorized use" }) : null) : null));
      cards.push(c);
      grid.append(c);
    }
  };
  const reloadTemplates = async () => {
    list = (await api("GET", "/templates")).items;
    if (!current()?.availability.creatable) chosen = list.find((t) => t.availability.creatable)?.id || list[0]?.id;
    drawGrid();
    paintDetail();
  };
  drawGrid();
  paintDetail();
  const node = h("div", {}, grid, detail);

  const v = await dialog({
    title: "New workspace", confirmText: "Create", wide: true,
    extra: {
      node,
      validate: () => {
        const t = current();
        if (!t) return "Choose a template.";
        if (t.authorized_use_only && !rulesBox.checked) return "Accept the lab rules to continue.";
        return "";
      },
      read: () => ({
        template_id: chosen, profile: sizeSel.value, rules: rulesBox.checked,
        capabilities: [...capBoxes].filter(([, b]) => b.checked).map(([c]) => c),
        egress: egressSel.value || "",
      }),
    },
    fields: [
      { name: "name", label: "Name", maxlength: 64, required: true, hint: "Letters, digits, spaces, . _ -" },
      { name: "persistence", label: "Data", type: "select", value: "", options: [{ value: "", label: "Template default" }, { value: "persistent", label: "Persistent (kept when deleted unless you choose otherwise)" }, { value: "disposable", label: "Disposable (erased on stop)" }] },
      { name: "idle", label: "Stop after idle (minutes)", type: "number", value: "", hint: "Empty = template default, 0 = never." },
    ],
  });
  if (!v) return;
  const body = { template_id: v.template_id, name: v.name.trim(), profile: v.profile };
  if (v.persistence) body.persistence = v.persistence;
  if (v.idle !== "") body.idle_timeout_minutes = Number(v.idle);
  if (v.rules) body.acknowledge_authorized_use = true;
  if (v.capabilities.length) body.capabilities = v.capabilities;
  if (v.egress) body.egress = v.egress;
  const created = await run("Create failed", () => api("POST", "/workspaces", { json: body }));
  if (created && created !== true) navigate(`#/w/${created.id}`);
}

// ---------------------------------------------------------------------------------------------
export async function workspaceView(id, tab = "overview") {
  let ws = await api("GET", `/workspaces/${encodeURIComponent(id)}`);
  const tpl = await api("GET", `/templates/${encodeURIComponent(ws.template_id)}`).catch(() => null);
  const streamed = tpl?.access === "stream";
  const body = h("div", { class: "stack" });
  const head = h("div", { class: "stack" });
  let lastStatus = ws.status;

  const actions = () => {
    const s = ws.status;
    const w = canWrite() && s !== "deleted";
    const act = (label, fn, cls = "", enabled = true) => h("button", { class: cls, text: label, disabled: !w || !enabled, onclick: fn });
    const call = (verb) => async () => { await run(`${verb} failed`, () => api("POST", `/workspaces/${id}/${verb}`)); refresh(); };
    const act2 = (label, ico, fn, cls, enabled) => { const b = act(label, fn, cls, enabled); b.prepend(icon(ico)); return b; };
    return h("div", { class: "row" },
      act2("Start", "play", call("start"), "primary", ["stopped", "failed"].includes(s)),
      act2("Stop", "stop", call("stop"), "", ["running", "idle", "starting", "failed"].includes(s)),
      act2("Restart", "refresh", call("restart"), "", ["running", "idle"].includes(s)),
      act2("Reset", "power", () => resetFlow(), "", ["stopped", "failed"].includes(s)),
      act2("Delete", "trash", () => deleteFlow(), "danger", !["deleting"].includes(s)));
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
    ...[["overview", "Overview", "layers"], ...(streamed ? [["screen", "Screen", "monitor"]] : []), ["files", "Files", "folder"], ["terminal", "Terminal", "terminal"]].map(([k, label, ico]) =>
      h("a", { href: `#/w/${id}/${k === "overview" ? "" : k}`, "aria-current": tab === k ? "page" : null }, icon(ico), label)));

  const drawHead = () => {
    clear(head);
    add(head,
      h("div", { class: "row between" }, h("div", { class: "row" }, h("a", { class: "btn ghost", href: "#/", "aria-label": "Back to workspaces" }, icon("grid")), tplIcon(tpl), h("div", {}, h("h1", { text: ws.name }), h("div", { class: "muted", text: tpl?.name || ws.template_id })), pill(ws.status)), actions()),
      ws.status_reason ? h("div", { class: `banner ${ws.status === "failed" ? "danger" : "warn"}`, text: ws.status_reason }) : null,
      ws.isolation === "none" ? h("div", { class: "banner warn", text: "This workspace runs as a local process with no isolation." }) : null);
  };

  const refresh = async () => {
    ws = await api("GET", `/workspaces/${encodeURIComponent(id)}`);
    drawHead();
    if (tab === "overview") await drawOverview(); // status-dependent panes re-render; files/terminal keep state
    else if (lastStatus !== ws.status && (tab === "terminal" || tab === "screen")) terminalPane.statusChanged?.(ws);
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
        h("dt", { text: "Network" }), h("dd", { text: ws.egress ? `${ws.network_policy} - ${ws.egress === "tor" ? "routed through Tor" : "direct (real IP)"}` : ws.network_policy }),
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
  if (tab === "screen" && streamed) { body.append(screenTab(ws, terminalPane, tpl)); }
  else if (tab === "files") { body.append(filesPane(ws)); }
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
      h("button", { disabled: !writable, onclick: mkdir }, icon("folder"), "New folder"),
      h("button", { class: "primary", disabled: !writable, onclick: () => fileInput.click() }, icon("upload"), "Upload"), fileInput);
    const rows = data.entries.map((e) => h("tr", {},
      h("td", {}, e.type === "dir" ? h("button", { class: "link", text: `${e.name}/`, onclick: () => load(join(e.name)) }) : h("span", { text: e.name }),
        e.type === "symlink" ? h("span", { class: "muted", text: " (symbolic link - not followed)" }) : null),
      h("td", { text: e.type === "file" ? fmtBytes(e.size) : "" }),
      h("td", { text: e.modified ? fmtTime(new Date(e.modified * 1000).toISOString()) : "" }),
      h("td", {}, h("div", { class: "file-actions row" },
        e.type === "file" ? h("a", { class: "btn", href: filesUrl(ws.id, join(e.name)), download: e.name }, icon("download"), "Download") : null,
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
