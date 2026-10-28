// Screen viewer for streamed workspaces (browsers, desktops, Tor Browser).
//
// The picture is VNC, relayed by the control plane over a same-origin WebSocket. The noVNC client
// library is loaded from the workspace image through an authenticated, allowlisted route, so the
// page keeps its strict CSP (no inline script, no third-party origin).
import { api } from "./api.js";
import { state } from "./core.js";
import { clear, dialog, h, icon, toast } from "./ui.js";

const ACTIVITY_EVERY_MS = 60000;

export function screenTab(ws, pane, tpl) {
  const id = encodeURIComponent(ws.id);
  const live = () => ["running", "idle"].includes(ws.status);
  const view = h("div", { class: "view" });
  const overlay = h("div", { class: "overlay" });
  const screen = h("div", { class: "screen", tabindex: "-1" }, view, overlay);
  const status = h("span", { class: "muted", role: "status", text: "Not connected" });
  let rfb = null;
  let RFB = null;
  let remoteClip = "";
  let lastActivity = 0;
  let fit = true;
  let connecting = false;

  const say = (title, detail, action) => {
    clear(overlay);
    overlay.append(h("div", { class: "stack" }, h("h2", { text: title }), detail ? h("p", { class: "muted", text: detail }) : null, action || null));
    overlay.hidden = false;
  };

  const touch = () => {
    const now = Date.now();
    if (now - lastActivity < ACTIVITY_EVERY_MS) return;
    lastActivity = now;
    api("POST", `/workspaces/${id}/activity`, { quiet401: true }).catch(() => {}); // keeps the idle timer honest
  };
  for (const ev of ["pointerdown", "keydown", "wheel"]) screen.addEventListener(ev, touch, { passive: true });

  const loadClient = async () => {
    if (!RFB) RFB = (await import(`/api/v1/workspaces/${id}/stream/assets/core/rfb.js`)).default;
    return RFB;
  };

  const disconnect = () => { if (rfb) { try { rfb.disconnect(); } catch { /* already gone */ } rfb = null; } };

  const connect = async () => {
    if (rfb || connecting) return;
    if (!live()) { say("Workspace is not running", "Start it, then the screen opens here.", null); return; }
    connecting = true;
    say("Connecting…", tpl?.id === "browser-tor" ? "Tor Browser can take a minute to connect to the Tor network on first start." : "Starting the stream.", null);
    status.textContent = "Connecting…";
    let Client;
    try { Client = await loadClient(); } catch (ex) {
      console.error("OrbitNest viewer load failed:", ex);
      connecting = false;
      status.textContent = "Viewer unavailable";
      say("The viewer could not be loaded", `The viewer script failed to load or run (${ex && ex.message ? ex.message : "unknown error"}). Open the browser console for details.`, h("button", { onclick: connect, text: "Try again" }));
      return;
    }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    try {
      rfb = new Client(view, `${scheme}://${location.host}/api/v1/workspaces/${id}/stream/ws`, { wsProtocols: ["binary"] });
    } catch (e) {
      connecting = false;
      say("Could not start the viewer", e.message, h("button", { onclick: connect, text: "Try again" }));
      return;
    }
    rfb.scaleViewport = fit;
    rfb.resizeSession = false;
    // Lower server-side compression effort: the server's CPU is the scarce resource, not the link.
    const local = ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);
    rfb.compressionLevel = local ? 0 : 2;
    rfb.qualityLevel = local ? 6 : 5;
    rfb.background = "#03050a";
    rfb.addEventListener("connect", () => { connecting = false; overlay.hidden = true; status.textContent = "Connected"; rfb.focus(); });
    rfb.addEventListener("disconnect", (ev) => {
      connecting = false; rfb = null;
      const clean = ev?.detail?.clean;
      status.textContent = clean ? "Disconnected" : "Connection lost";
      if (live()) say(clean ? "Disconnected" : "Connection lost", clean ? "" : "The stream ended unexpectedly. The workspace may still be starting.", h("button", { class: "primary", onclick: connect, text: "Reconnect" }));
      else say(`Workspace is ${ws.status}`, "Start it to open the screen again.", null);
    });
    rfb.addEventListener("securityfailure", (ev) => { say("Connection refused", ev?.detail?.reason || "The screen server refused the connection.", null); });
    rfb.addEventListener("clipboard", (ev) => { remoteClip = ev?.detail?.text || ""; copyBtn.disabled = !remoteClip; });
  };

  const pasteBtn = h("button", { type: "button", onclick: async () => {
    if (!rfb) { toast("Connect first.", "error"); return; }
    const v = await dialog({ title: "Paste text to the remote screen", confirmText: "Send", fields: [{ name: "text", label: "Text", type: "textarea" }] });
    if (v?.text) { rfb.clipboardPasteFrom(v.text); toast("Sent. Press Ctrl+V inside the remote screen.", "ok"); }
  } }, icon("clipboard"), "Paste to remote");
  const copyBtn = h("button", { type: "button", disabled: true, onclick: async () => {
    try { await navigator.clipboard.writeText(remoteClip); toast("Copied the remote clipboard.", "ok"); } catch { toast("Your browser blocked clipboard access.", "error"); }
  } }, icon("clipboard"), "Copy from remote");
  const cad = h("button", { type: "button", onclick: () => rfb?.sendCtrlAltDel() }, icon("keyboard"), "Ctrl+Alt+Del");
  const fitBtn = h("button", { type: "button", "aria-pressed": "true", onclick: () => {
    fit = !fit; if (rfb) rfb.scaleViewport = fit; fitBtn.setAttribute("aria-pressed", String(fit));
    fitBtn.lastChild.textContent = fit ? "Fit to window" : "Actual size";
  } }, icon("maximize"), "Fit to window");
  const fullBtn = h("button", { type: "button", onclick: () => { if (document.fullscreenElement) document.exitFullscreen(); else screen.requestFullscreen?.(); } }, icon("maximize"), "Fullscreen");
  const stopBtn = h("button", { type: "button", class: "ghost", onclick: () => { disconnect(); status.textContent = "Disconnected"; say("Disconnected", "", h("button", { class: "primary", onclick: connect, text: "Reconnect" })); } }, icon("power"), "Disconnect");

  pane.statusChanged = (w) => {
    ws = w;
    if (!live() && rfb) { disconnect(); say(`Workspace is ${w.status}`, "Start it to open the screen again.", null); status.textContent = "Disconnected"; }
    else if (live() && !rfb && !connecting && !overlay.querySelector("button")) connect();
  };
  state.onLeave = () => { disconnect(); if (document.fullscreenElement) document.exitFullscreen(); };

  const notes = [];
  if (tpl?.privacy === "tor") {
    notes.push(h("div", { class: "banner" }, icon("onion"), h("div", {}, h("strong", { text: "Tor Browser. " }), "Web traffic from this window goes through the Tor network. Check it: open check.torproject.org in this browser and look for \"Congratulations\". Not Tails: your OrbitNest login, files you upload and accounts you sign in to are not anonymous.")));
  } else if (tpl?.privacy === "direct") {
    notes.push(h("div", { class: "banner warn" }, icon("shield"), h("div", {}, h("strong", { text: "Not anonymous. " }), "Sites see this computer's real IP address and internet provider. For privacy, create a Tor Browser workspace instead.")));
  }
  say("Ready", "", null);
  if (live()) connect(); else say("Workspace is not running", "Start it, then the screen opens here.", null);
  return h("div", { class: "stack" }, ...notes,
    h("div", { class: "toolbar" }, status, h("div", { class: "grow" }), pasteBtn, copyBtn, cad, fitBtn, fullBtn, stopBtn),
    screen,
    h("p", { class: "hint", text: "The screen is streamed from the container through the control plane; nothing is exposed on your network. Keyboard and mouse input go to the remote screen while it has focus." }));
}
