import { VT, keyToSequence } from "./vt.js";
import { clear, h } from "./ui.js";

const ANSI16 = ["#1b1f27", "#e5534b", "#57ab5a", "#c69026", "#539bf5", "#b083f0", "#39c5cf", "#adbac7",
  "#545d68", "#ff938a", "#6bc46d", "#daaa3f", "#6cb6ff", "#dcbdfb", "#56d4dd", "#cdd9e5"];

function color(n) {
  if (n < 0) return null;
  if (n < 16) return ANSI16[n];
  if (n < 232) {
    const v = n - 16, lv = [0, 95, 135, 175, 215, 255];
    return `rgb(${lv[Math.floor(v / 36) % 6]},${lv[Math.floor(v / 6) % 6]},${lv[v % 6]})`;
  }
  const g = 8 + (n - 232) * 10;
  return `rgb(${g},${g},${g})`;
}

// DOM front end for VT. Colours are applied through the CSSOM (element.style), which the strict
// CSP allows; inline style attributes in markup would be blocked.
export class Terminal {
  constructor(host, { onData, onResize }) {
    this.host = host;
    this.onData = onData;
    this.onResize = onResize;
    this.decoder = new TextDecoder("utf-8", { fatal: false });
    this.vt = new VT(80, 24);
    this.pending = false;
    this.el = h("div", { class: "term", tabindex: "0", role: "textbox", "aria-label": "Terminal", "aria-multiline": "true" });
    this.measure = h("span", { class: "term-measure", text: "M".repeat(50) });
    host.append(this.el, this.measure);
    this.el.addEventListener("keydown", (e) => {
      const seq = keyToSequence(e);
      if (seq !== null) { e.preventDefault(); this.onData(seq); }
    });
    this.el.addEventListener("paste", (e) => {
      e.preventDefault();
      const text = e.clipboardData?.getData("text") ?? "";
      if (text) this.onData(text.replace(/\r?\n/g, "\r"));
    });
    this.ro = new ResizeObserver(() => this.fit());
    this.ro.observe(host);
    this.fit(true);
    this.render();
  }

  write(bytes) {
    this.vt.write(this.decoder.decode(bytes, { stream: true }));
    this.schedule();
  }

  writeText(text) { this.vt.write(text); this.schedule(); }

  schedule() {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => { this.pending = false; this.render(); });
  }

  fit(initial = false) {
    const box = this.measure.getBoundingClientRect();
    const cw = box.width / 50 || 8;
    const ch = box.height || 17;
    const cols = Math.max(20, Math.floor((this.host.clientWidth - 16) / cw));
    const rows = Math.max(5, Math.min(60, Math.floor(Math.max(this.host.clientHeight, 360) / ch)));
    if (cols !== this.vt.cols || rows !== this.vt.rows) {
      this.vt.resize(cols, rows);
      if (!initial) this.onResize?.(cols, rows);
      this.schedule();
    }
    this.el.style.height = `${rows * ch}px`;
  }

  size() { return [this.vt.cols, this.vt.rows]; }
  focus() { this.el.focus(); }

  render() {
    const vt = this.vt;
    const frag = document.createDocumentFragment();
    for (let y = 0; y < vt.rows; y++) {
      const line = h("div");
      let run = null, text = "";
      const flush = () => {
        if (!run) return;
        const span = h("span", { text });
        const fg = color(run.inverse ? (run.bg < 0 ? 7 : run.bg) : run.fg);
        const bg = color(run.inverse ? (run.fg < 0 ? 0 : run.fg) : run.bg);
        if (fg) span.style.color = fg;
        if (bg) span.style.backgroundColor = bg;
        if (run.bold) span.style.fontWeight = "700";
        if (run.underline) span.style.textDecoration = "underline";
        line.append(span);
      };
      for (let x = 0; x < vt.cols; x++) {
        const cell = vt.grid[y][x];
        const isCursor = vt.cursorVisible && y === vt.y && x === vt.x && document.activeElement === this.el;
        const key = `${cell.fg}|${cell.bg}|${cell.bold}|${cell.underline}|${cell.inverse}|${isCursor}`;
        if (!run || run.key !== key) {
          flush();
          run = { ...cell, key };
          if (isCursor) run.inverse = !run.inverse;
          text = "";
        }
        text += cell.ch;
      }
      flush();
      frag.append(line);
    }
    clear(this.el);
    this.el.append(frag);
  }

  dispose() { this.ro.disconnect(); this.el.remove(); this.measure.remove(); }
}
