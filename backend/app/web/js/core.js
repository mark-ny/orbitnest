// Shared state and page plumbing for all views.
import { api } from "./api.js";
import { clear, h, icon, themeButton, toast } from "./ui.js";

export const state = { user: null, caps: null, timer: null, onLeave: null };
export const root = document.getElementById("app");

export const isAdmin = () => state.user?.role === "admin";
export const canWrite = () => state.user?.role !== "read_only";

export function navigate(hash) {
  if (location.hash === hash) window.dispatchEvent(new Event("hashchange"));
  else location.hash = hash;
}

export function leave() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  if (state.onLeave) state.onLeave();
  state.onLeave = null;
}

// Re-run `fn` every `ms` until the user navigates away. Errors are shown once, not every tick.
export function poll(fn, ms) {
  let failed = false;
  state.timer = setInterval(async () => {
    try { await fn(); failed = false; } catch (e) { if (!failed) toast(e.message, "error"); failed = true; }
  }, ms);
}

export function logoMark() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 32 32");
  svg.setAttribute("aria-hidden", "true");
  const c = document.createElementNS(ns, "circle");
  for (const [k, v] of Object.entries({ cx: 16, cy: 16, r: 5 })) c.setAttribute(k, v);
  c.setAttribute("fill", "currentColor");
  const e = document.createElementNS(ns, "ellipse");
  for (const [k, v] of Object.entries({ cx: 16, cy: 16, rx: 13, ry: 6, fill: "none", stroke: "currentColor", "stroke-width": 2, transform: "rotate(-25 16 16)" })) e.setAttribute(k, v);
  svg.append(c, e);
  return svg;
}

export async function loadCaps(force = false) {
  if (!state.caps || force) state.caps = await api("GET", "/system/capabilities");
  return state.caps;
}

export function brand(href = "#/") {
  return h("a", { class: "brand", href }, h("span", { class: "mark" }, logoMark()), "OrbitNest");
}

export function nav(active) {
  const link = (href, label, key, ico) => h("a", { href, "aria-current": active === key ? "page" : null }, icon(ico), label);
  const initial = (state.user.username[0] || "?").toUpperCase();
  const out = h("button", { class: "ghost iconbtn", type: "button", "aria-label": "Sign out", title: "Sign out", onclick: async () => {
    try { await api("POST", "/auth/logout"); } catch { /* already signed out */ }
    location.hash = "#/"; location.reload();
  } }, icon("logout"));
  return h("header", { class: "topbar" },
    brand(),
    h("nav", { class: "nav", "aria-label": "Main" },
      link("#/", "Workspaces", "workspaces", "grid"),
      isAdmin() ? link("#/admin/users", "Admin", "admin", "settings") : null,
      link("#/account", "Account", "account", "user")),
    h("div", { class: "grow" }),
    themeButton(),
    h("div", { class: "userbox" }, h("span", { class: "avatar", "aria-hidden": "true", text: initial }),
      h("div", { class: "who" }, state.user.username, h("small", { text: state.user.role.replace("_", " ") }))),
    out);
}

export function page(active, ...content) {
  leave();
  clear(root);
  root.append(nav(active), h("main", { class: "page stack" }, ...content));
}

export function full(node) {
  leave();
  clear(root);
  root.append(node);
}

export function bare(...content) {
  leave();
  clear(root);
  root.append(h("main", { class: "page" }, ...content));
}

export function errorBox(e) {
  return h("div", { class: "banner danger", role: "alert", text: e?.message || "Something went wrong." });
}

export function modeBanner() {
  const caps = state.caps;
  if (!caps) return null;
  if (caps.mode === "termux-local") {
    return h("div", { class: "banner warn" }, icon("alert"), h("div", {}, h("strong", { text: "No isolation. " }),
      "This device runs workspaces as ordinary processes under your own Termux user. Docker is not available in ordinary Termux, and PRoot is not a security boundary. Only run code you trust."));
  }
  const f = caps.features?.container_workspaces;
  if (f && f.state !== "available") {
    return h("div", { class: "banner warn" }, icon("alert"), h("div", {}, h("strong", { text: "Workspaces unavailable. " }), f.reason, f.remediation ? ` ${f.remediation}` : ""));
  }
  return null;
}
