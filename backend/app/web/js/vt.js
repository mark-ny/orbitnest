// A small VT100/xterm-style screen model (no DOM). Enough for bash, readline, less, nano-style
// full-screen programs; not a complete xterm (no mouse, no charsets, no scroll regions beyond
// the common DECSTBM case). Pure logic so it can be unit-tested under Node.

const DEFAULT = { fg: -1, bg: -1, bold: false, underline: false, inverse: false };

export class VT {
  constructor(cols = 80, rows = 24, scrollback = 1000) {
    this.scrollbackLimit = scrollback;
    this.scrollback = [];
    this.title = "";
    this.resize(cols, rows, true);
  }

  blankRow() {
    return Array.from({ length: this.cols }, () => ({ ch: " ", ...DEFAULT }));
  }

  resize(cols, rows, fresh = false) {
    cols = Math.max(2, Math.min(cols | 0, 500));
    rows = Math.max(2, Math.min(rows | 0, 200));
    const old = fresh ? null : this.grid;
    this.cols = cols;
    this.rows = rows;
    this.grid = Array.from({ length: rows }, (_, y) => {
      const row = this.blankRow();
      if (old && old[y]) for (let x = 0; x < Math.min(cols, old[y].length); x++) row[x] = old[y][x];
      return row;
    });
    if (fresh) {
      this.x = 0;
      this.y = 0;
      this.attr = { ...DEFAULT };
      this.saved = null;
      this.state = "ground";
      this.params = "";
      this.altGrid = null;
      this.top = 0;
      this.bottom = rows - 1;
      this.cursorVisible = true;
      this.wrapPending = false;
    } else {
      this.x = Math.min(this.x, cols - 1);
      this.y = Math.min(this.y, rows - 1);
      this.top = 0;
      this.bottom = rows - 1;
    }
  }

  // -- output ------------------------------------------------------------------------------
  write(text) {
    for (const ch of text) this.put(ch);
  }

  put(ch) {
    const c = ch.codePointAt(0);
    switch (this.state) {
      case "esc":
        return this.escape(ch);
      case "csi":
        if ((c >= 0x30 && c <= 0x3f) || c === 0x20) { this.params += ch; return; }
        this.state = "ground";
        return this.csi(ch, this.params);
      case "osc":
        if (c === 7) { this.state = "ground"; this.setOsc(); }
        else if (c === 0x1b) this.state = "oscesc";
        else if (this.params.length < 512) this.params += ch;
        return;
      case "oscesc":
        this.state = "ground";
        this.setOsc();
        return;
      case "charset":
        this.state = "ground";
        return;
    }
    if (c === 0x1b) { this.state = "esc"; return; }
    if (c < 0x20 || c === 0x7f) return this.control(c);
    this.print(ch);
  }

  setOsc() {
    const m = /^[012];(.*)$/s.exec(this.params);
    if (m) this.title = m[1].slice(0, 200);
    this.params = "";
  }

  control(c) {
    switch (c) {
      case 0x07: break; // bell
      case 0x08: if (this.x > 0) this.x--; this.wrapPending = false; break;
      case 0x09: this.x = Math.min(this.cols - 1, (this.x + 8) & ~7); this.wrapPending = false; break;
      case 0x0a: case 0x0b: case 0x0c: this.lineFeed(); break;
      case 0x0d: this.x = 0; this.wrapPending = false; break;
    }
  }

  print(ch) {
    if (this.wrapPending) { this.x = 0; this.lineFeed(); this.wrapPending = false; }
    this.grid[this.y][this.x] = { ch, ...this.attr };
    if (this.x === this.cols - 1) this.wrapPending = true;
    else this.x++;
  }

  lineFeed() {
    if (this.y === this.bottom) this.scrollUp(1);
    else if (this.y < this.rows - 1) this.y++;
  }

  scrollUp(n) {
    for (let i = 0; i < n; i++) {
      const gone = this.grid.splice(this.top, 1)[0];
      if (this.top === 0 && !this.altGrid) {
        this.scrollback.push(gone);
        if (this.scrollback.length > this.scrollbackLimit) this.scrollback.shift();
      }
      this.grid.splice(this.bottom, 0, this.blankRow());
    }
  }

  scrollDown(n) {
    for (let i = 0; i < n; i++) {
      this.grid.splice(this.bottom, 1);
      this.grid.splice(this.top, 0, this.blankRow());
    }
  }

  escape(ch) {
    this.state = "ground";
    switch (ch) {
      case "[": this.state = "csi"; this.params = ""; break;
      case "]": this.state = "osc"; this.params = ""; break;
      case "(": case ")": case "*": case "+": this.state = "charset"; break;
      case "7": this.saved = { x: this.x, y: this.y, attr: { ...this.attr } }; break;
      case "8": if (this.saved) { ({ x: this.x, y: this.y } = this.saved); this.attr = { ...this.saved.attr }; } break;
      case "D": this.lineFeed(); break;
      case "E": this.x = 0; this.lineFeed(); break;
      case "M": if (this.y === this.top) this.scrollDown(1); else if (this.y > 0) this.y--; break;
      case "c": this.resize(this.cols, this.rows, true); break;
    }
  }

  nums(params, fallback) {
    const clean = params.replace(/^[?>=!]/, "");
    if (clean === "") return [fallback];
    return clean.split(";").map((p) => (p === "" ? fallback : Math.min(parseInt(p, 10) || 0, 100000)));
  }

  clampCursor() {
    this.x = Math.max(0, Math.min(this.x, this.cols - 1));
    this.y = Math.max(0, Math.min(this.y, this.rows - 1));
    this.wrapPending = false;
  }

  eraseCells(row, from, to) {
    for (let x = from; x < to; x++) row[x] = { ch: " ", ...DEFAULT, bg: this.attr.bg };
  }

  csi(final, params) {
    const priv = params.startsWith("?");
    const n = this.nums(params, 0);
    const a = n[0] || 1;
    const rowsN = Math.min(a, this.rows); // bound loops: hostile output must not stall the tab
    const colsN = Math.min(a, this.cols);
    switch (final) {
      case "A": this.y = Math.max(this.top, this.y - a); this.wrapPending = false; break;
      case "B": this.y = Math.min(this.bottom, this.y + a); this.wrapPending = false; break;
      case "C": this.x = Math.min(this.cols - 1, this.x + a); this.wrapPending = false; break;
      case "D": this.x = Math.max(0, this.x - a); this.wrapPending = false; break;
      case "E": this.y = Math.min(this.bottom, this.y + a); this.x = 0; this.wrapPending = false; break;
      case "F": this.y = Math.max(this.top, this.y - a); this.x = 0; this.wrapPending = false; break;
      case "G": this.x = a - 1; this.clampCursor(); break;
      case "d": this.y = a - 1; this.clampCursor(); break;
      case "H": case "f": this.y = (n[0] || 1) - 1; this.x = (n[1] || 1) - 1; this.clampCursor(); break;
      case "J": {
        const mode = n[0] || 0;
        if (mode === 0) { this.eraseCells(this.grid[this.y], this.x, this.cols); for (let y = this.y + 1; y < this.rows; y++) this.eraseCells(this.grid[y], 0, this.cols); }
        else if (mode === 1) { for (let y = 0; y < this.y; y++) this.eraseCells(this.grid[y], 0, this.cols); this.eraseCells(this.grid[this.y], 0, this.x + 1); }
        else if (mode === 2 || mode === 3) for (let y = 0; y < this.rows; y++) this.eraseCells(this.grid[y], 0, this.cols);
        break;
      }
      case "K": {
        const mode = n[0] || 0;
        const row = this.grid[this.y];
        if (mode === 0) this.eraseCells(row, this.x, this.cols);
        else if (mode === 1) this.eraseCells(row, 0, this.x + 1);
        else if (mode === 2) this.eraseCells(row, 0, this.cols);
        break;
      }
      case "L": if (this.y >= this.top && this.y <= this.bottom) { const t = this.top; this.top = this.y; this.scrollDown(rowsN); this.top = t; } break;
      case "M": if (this.y >= this.top && this.y <= this.bottom) { const t = this.top; this.top = this.y; const keep = this.altGrid; this.altGrid = this.altGrid || {}; this.scrollUp(rowsN); this.altGrid = keep; this.top = t; } break;
      case "P": { const row = this.grid[this.y]; row.splice(this.x, colsN); while (row.length < this.cols) row.push({ ch: " ", ...DEFAULT }); break; }
      case "@": { const row = this.grid[this.y]; for (let i = 0; i < colsN; i++) row.splice(this.x, 0, { ch: " ", ...DEFAULT }); row.length = this.cols; break; }
      case "X": this.eraseCells(this.grid[this.y], this.x, Math.min(this.cols, this.x + a)); break;
      case "S": this.scrollUp(rowsN); break;
      case "T": this.scrollDown(rowsN); break;
      case "r": { const t = (n[0] || 1) - 1; const b = (n[1] || this.rows) - 1; if (t < b && b < this.rows) { this.top = t; this.bottom = b; this.x = 0; this.y = 0; } break; }
      case "s": this.saved = { x: this.x, y: this.y, attr: { ...this.attr } }; break;
      case "u": if (this.saved) { this.x = this.saved.x; this.y = this.saved.y; this.wrapPending = false; } break;
      case "m": this.sgr(params === "" ? [0] : this.nums(params, 0)); break;
      case "h": case "l": if (priv) this.privateMode(n, final === "h"); break;
    }
  }

  privateMode(codes, on) {
    for (const code of codes) {
      if (code === 25) this.cursorVisible = on;
      else if (code === 1049 || code === 1047 || code === 47) {
        if (on && !this.altGrid) {
          this.altGrid = this.grid;
          this.savedMain = { x: this.x, y: this.y };
          this.grid = Array.from({ length: this.rows }, () => this.blankRow());
        } else if (!on && this.altGrid) {
          this.grid = this.altGrid;
          this.altGrid = null;
          if (this.savedMain) ({ x: this.x, y: this.y } = this.savedMain);
        }
      }
    }
  }

  sgr(codes) {
    for (let i = 0; i < codes.length; i++) {
      const c = codes[i];
      if (c === 0) this.attr = { ...DEFAULT };
      else if (c === 1) this.attr.bold = true;
      else if (c === 22) this.attr.bold = false;
      else if (c === 4) this.attr.underline = true;
      else if (c === 24) this.attr.underline = false;
      else if (c === 7) this.attr.inverse = true;
      else if (c === 27) this.attr.inverse = false;
      else if (c >= 30 && c <= 37) this.attr.fg = c - 30;
      else if (c >= 90 && c <= 97) this.attr.fg = c - 90 + 8;
      else if (c === 39) this.attr.fg = -1;
      else if (c >= 40 && c <= 47) this.attr.bg = c - 40;
      else if (c >= 100 && c <= 107) this.attr.bg = c - 100 + 8;
      else if (c === 49) this.attr.bg = -1;
      else if (c === 38 || c === 48) {
        const key = c === 38 ? "fg" : "bg";
        if (codes[i + 1] === 5 && codes[i + 2] !== undefined) { this.attr[key] = Math.min(codes[i + 2], 255); i += 2; }
        else if (codes[i + 1] === 2) i += 4; // truecolor: skipped (approximated by default colour)
      }
    }
  }

  // -- reading back ------------------------------------------------------------------------
  lineText(y) {
    return this.grid[y].map((c) => c.ch).join("").replace(/\s+$/, "");
  }

  text() {
    return this.grid.map((_, y) => this.lineText(y)).join("\n").replace(/\n+$/, "");
  }
}

// Keyboard -> bytes the pty expects.
export function keyToSequence(e) {
  const k = e.key;
  const map = {
    Enter: "\r", Backspace: "\x7f", Tab: "\t", Escape: "\x1b",
    ArrowUp: "\x1b[A", ArrowDown: "\x1b[B", ArrowRight: "\x1b[C", ArrowLeft: "\x1b[D",
    Home: "\x1b[H", End: "\x1b[F", Delete: "\x1b[3~", PageUp: "\x1b[5~", PageDown: "\x1b[6~", Insert: "\x1b[2~",
  };
  if (e.shiftKey && k === "Tab") return "\x1b[Z";
  if (map[k] !== undefined) return map[k];
  if (e.ctrlKey && !e.altKey && !e.metaKey && k.length === 1) {
    const c = k.toLowerCase().charCodeAt(0);
    if (c >= 97 && c <= 122) return String.fromCharCode(c - 96);
    if (k === " " || k === "@") return "\x00";
    if (k === "[") return "\x1b";
    if (k === "\\") return "\x1c";
    if (k === "]") return "\x1d";
  }
  if (e.altKey && !e.ctrlKey && !e.metaKey && k.length === 1) return "\x1b" + k;
  if (!e.ctrlKey && !e.metaKey && k.length >= 1 && [...k].length === 1) return k;
  return null;
}
