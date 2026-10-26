import { onUnauthorized, api, setCsrf } from "./api.js";
import { adminView } from "./admin-views.js";
import { accountView, showLogin, showSetup } from "./auth-views.js";
import { bare, errorBox, loadCaps, state } from "./core.js";
import { h, toast } from "./ui.js";
import { workspaceView, workspacesView } from "./workspace-views.js";

async function route() {
  if (!state.user) return;
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  try {
    if (parts[0] === "w" && parts[1]) await workspaceView(parts[1], parts[2] || "overview");
    else if (parts[0] === "admin") {
      if (state.user.role !== "admin") { location.hash = "#/"; return; }
      await adminView(parts[1] || "users");
    } else if (parts[0] === "account") await accountView();
    else await workspacesView();
  } catch (e) {
    if (e.status === 401) return;
    bare(errorBox(e), h("p", {}, h("a", { href: "#/", text: "Back to workspaces" })));
  }
}

let listening = false;
async function start() {
  await loadCaps().catch(() => null);
  if (!listening) { window.addEventListener("hashchange", route); listening = true; }
  await route();
}

async function boot() {
  let setup = null;
  try { setup = await api("GET", "/auth/setup-status"); } catch (e) { bare(errorBox(e)); return; }
  if (setup.setup_required) { showSetup(setup, () => { location.hash = "#/"; start(); }); return; }
  try {
    const s = await api("GET", "/auth/session", { quiet401: true });
    setCsrf(s.csrf_token);
    state.user = s.user;
  } catch {
    showLogin(start);
    return;
  }
  await start();
}

let handling = false;
onUnauthorized(() => {
  if (handling) return;
  handling = true;
  state.user = null;
  toast("Your session ended. Please sign in again.", "error");
  showLogin(async () => { handling = false; await start(); });
});

boot();
