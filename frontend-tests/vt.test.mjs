import test from "node:test";
import assert from "node:assert/strict";
import { VT, keyToSequence } from "../backend/app/web/js/vt.js";

const feed = (vt, s) => { vt.write(s); return vt; };

test("plain text, CRLF, wrapping and scroll", () => {
  const vt = feed(new VT(10, 3), "hello\r\nworld\r\n");
  assert.equal(vt.lineText(0), "hello");
  assert.equal(vt.lineText(1), "world");
  feed(vt, "a".repeat(12));
  // 12 letters on a 10-column screen: the line wraps and the screen scrolls once
  assert.equal(vt.lineText(1), "aaaaaaaaaa");
  assert.equal(vt.lineText(2), "aa");
  assert.equal(vt.scrollback.length, 1);
  assert.equal(vt.scrollback[0].map((c) => c.ch).join("").trim(), "hello");
});

test("readline-style editing: backspace, erase to end of line, cursor moves", () => {
  const vt = feed(new VT(20, 3), "$ lss");
  feed(vt, "\b\x1b[K"); // backspace then erase
  assert.equal(vt.lineText(0), "$ ls");
  feed(vt, "\x1b[2D\x1b[P"); // left 2, delete char
  assert.equal(vt.lineText(0), "$ s");
  feed(vt, "\r\x1b[2K> ok");
  assert.equal(vt.lineText(0), "> ok");
});

test("cursor positioning and clearing the screen", () => {
  const vt = feed(new VT(10, 4), "xxxxxxxxxx\r\nyyyyyyyyyy");
  feed(vt, "\x1b[2;3HZ");
  assert.equal(vt.lineText(1), "yyZyyyyyyy");
  feed(vt, "\x1b[2J\x1b[H");
  assert.equal(vt.text(), "");
  assert.deepEqual([vt.x, vt.y], [0, 0]);
});

test("SGR colours and resets", () => {
  const vt = feed(new VT(10, 2), "\x1b[1;31mR\x1b[0mn\x1b[38;5;200mC\x1b[48;5;21mB\x1b[0m");
  assert.deepEqual([vt.grid[0][0].fg, vt.grid[0][0].bold], [1, true]);
  assert.deepEqual([vt.grid[0][1].fg, vt.grid[0][1].bold], [-1, false]);
  assert.equal(vt.grid[0][2].fg, 200);
  assert.equal(vt.grid[0][3].bg, 21);
});

test("alternate screen is restored", () => {
  const vt = feed(new VT(10, 3), "main");
  feed(vt, "\x1b[?1049h\x1b[Hvim!");
  assert.equal(vt.lineText(0), "vim!");
  feed(vt, "\x1b[?1049l");
  assert.equal(vt.lineText(0), "main");
});

test("OSC titles are captured and never rendered; unknown sequences ignored", () => {
  const vt = feed(new VT(20, 2), "\x1b]0;my title\x07shown\x1b[?2004h\x1b(Bok\x1b[999;999Hz");
  assert.equal(vt.title, "my title");
  assert.equal(vt.lineText(0).startsWith("shownok"), true);
});

test("hostile input cannot break invariants", () => {
  const vt = new VT(5, 3);
  const junk = ["\x1b[", "\x1b]", "\x1b[99999999999A", "\x1b[;;;;;;H", "\x1b[-1;-1H", "\x1b[?", "\x1bZ", "\u0000\u001f", "💥", "\x1b[1000000L", "\x1b[1000000@"];
  for (let i = 0; i < 2000; i++) vt.write(junk[i % junk.length] + (i % 7 === 0 ? "\n" : "x"));
  assert.equal(vt.grid.length, 3);
  for (const row of vt.grid) assert.equal(row.length, 5);
  assert.ok(vt.x >= 0 && vt.x < 5 && vt.y >= 0 && vt.y < 3);
  assert.ok(vt.scrollback.length <= 1000);
});

test("resize keeps content and cursor in range", () => {
  const vt = feed(new VT(10, 4), "abc\r\ndef");
  vt.resize(5, 2);
  assert.ok(vt.x < 5 && vt.y < 2);
  vt.resize(20, 10);
  assert.equal(vt.lineText(0), "abc");
});

test("keyboard mapping", () => {
  const k = (key, o = {}) => keyToSequence({ key, ...o });
  assert.equal(k("a"), "a");
  assert.equal(k("Enter"), "\r");
  assert.equal(k("c", { ctrlKey: true }), "\x03");
  assert.equal(k("ArrowUp"), "\x1b[A");
  assert.equal(k("b", { altKey: true }), "\x1bb");
  assert.equal(k("Shift"), null);
  assert.equal(k("v", { metaKey: true }), null);
  assert.equal(k("é"), "é");
});
