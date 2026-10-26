// Shared state and page plumbing for all views.
import { api } from "./api.js";
import { clear, h, toast } from "./ui.js";

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

export function nav(active) {
  const link = (href, label, key) => h("a", { href, text: label, "aria-current": active === key ? "page" : null });
  return h("header", { class: "topbar" },
    h("a", { class: "brand", href: "#/" }, logoMark(), "OrbitNest"),
    h("nav", { class: "nav", "aria-label": "Main" },
      link("#/", "Workspaces", "workspaces"),
      isAdmin() ? link("#/admin/users", "Admin", "admin") : null,
      link("#/account", "Account", "account")),
    h("div", { class: "grow" }),
    h("span", { class: "muted", text: `${state.user.username} (${state.user.role})` }));
}

export function page(active, ...content) {
  leave();
  clear(root);
  root.append(nav(active), h("main", { class: "page stack" }, ...content));
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
    return h("div", { class: "banner warn" }, h("strong", { text: "No isolation. " }),
      "This device runs workspaces as ordinary processes under your own Termux user. Docker is not available in ordinary Termux, and PRoot is not a security boundary. Only run code you trust.");
  }
  const f = caps.features?.container_workspaces;
  if (f && f.state !== "available") {
    return h("div", { class: "banner warn" }, h("strong", { text: "Workspaces unavailable. " }), f.reason, f.remediation ? ` ${f.remediation}` : "");
  }
  return null;
}
