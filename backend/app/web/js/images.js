// One-click image install for administrators: starts the build on the server and shows its progress.
import { api } from "./api.js";
import { dialog, h, toast } from "./ui.js";

const WHEN = { running: "Building…", done: "Finished", failed: "Failed", cancelled: "Cancelled", idle: "Starting…" };

export async function installImageDialog(tpl) {
  const status = h("div", { class: "muted", text: "Starting…" });
  const log = h("pre", { class: "buildlog mono", "aria-live": "polite", text: "" });
  const stop = h("button", { type: "button", class: "danger", text: "Stop build" });
  const node = h("div", { class: "stack" },
    h("p", { class: "muted", text: "This downloads and builds the image on the server. It can take from several minutes to over an hour on a slow connection. You can close this window; the build keeps running." }),
    status, log, h("div", { class: "row" }, stop));
  let finalState = null;
  let timer = null;
  const paint = (st) => {
    const where = st.steps ? ` (step ${st.step} of ${st.steps})` : "";
    status.textContent = `${WHEN[st.state] || st.state}${st.state === "running" ? where : ""}${st.error ? ` ${st.error}` : ""}`;
    log.textContent = (st.log || []).slice(-60).join("\n");
    log.scrollTop = log.scrollHeight;
    stop.disabled = st.state !== "running";
  };
  const poll = async () => {
    if (!node.isConnected) { clearInterval(timer); return; }
    try {
      const st = await api("GET", `/templates/${tpl.id}/build`);
      paint(st);
      if (st.state !== "running") { clearInterval(timer); finalState = st.state; }
    } catch (e) { status.textContent = e.message; clearInterval(timer); }
  };
  stop.addEventListener("click", async () => {
    try { await api("DELETE", `/templates/${tpl.id}/build`); } catch (e) { toast(e.message, "error"); }
  });
  try {
    paint(await api("POST", `/templates/${tpl.id}/build`));
  } catch (e) {
    if (e.status !== 409) { toast(e.message, "error"); return false; }
    status.textContent = e.message;
  }
  timer = setInterval(poll, 2000);
  await dialog({ title: `Install image: ${tpl.name}`, confirmText: "Close", wide: true, extra: { node, read: () => ({}) } });
  clearInterval(timer);
  return finalState === "done";
}
