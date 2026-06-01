import { useEffect, useMemo, useRef } from 'react';

/**
 * TerminalLog — renders a sandbox transcript with a real terminal feel.
 *
 * The transcript (recorded by the SSH ForceCommand wrapper in sandbox_backend.py)
 * is a flat string with three kinds of lines:
 *   - command markers:  `[<ts>] $ <command>`
 *   - exit markers:     `[<ts>] (exit N)` / `[<ts>] (interactive shell)`
 *   - output:           arbitrary stdout/stderr (often JSONL, sometimes ANSI)
 *
 * We classify each line and render it like a terminal: dim timestamps, a green
 * prompt, colored exit codes, ANSI SGR colors for real terminal output, and
 * JSON syntax-coloring for the structured event lines these scripts emit. A
 * "raw" toggle always exposes the verbatim transcript.
 *
 * Dependency-free: a small ANSI SGR parser + a JSON tokenizer, no xterm/anser.
 */

// Hard cap on pretty-rendered lines. The transcript tail is already bounded by
// the backend, but rendering tens of thousands of DOM nodes every 3s poll would
// be wasteful. Beyond this we keep the most recent lines and show a notice.
const MAX_LINES = 5000;

// xterm-ish 16-color palette tuned for the #161616 terminal background.
const FG = {
  30: '#3b3a38', 31: '#e86d6d', 32: '#8fd07a', 33: '#e0b35a',
  34: '#6aa6e0', 35: '#c08fe0', 36: '#5fc9c9', 37: '#d8d6d1',
  90: '#6b6862', 91: '#ff8a8a', 92: '#b5e8a0', 93: '#f0cf85',
  94: '#9cc7f5', 95: '#d8b6f0', 96: '#8fe3e3', 97: '#ffffff',
};

function xterm256(n) {
  if (n == null || Number.isNaN(n)) return undefined;
  if (n < 16) return FG[n < 8 ? 30 + n : 90 + (n - 8)];
  if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
  const i = n - 16;
  const r = Math.floor(i / 36), g = Math.floor((i % 36) / 6), b = i % 6;
  const lvl = (v) => (v === 0 ? 0 : 55 + v * 40);
  return `rgb(${lvl(r)},${lvl(g)},${lvl(b)})`;
}

// Apply one SGR (`\x1b[...m`) escape's codes to the running style object.
function applySgr(prev, codeStr) {
  const codes = (codeStr || '0').split(';').map((s) => (s === '' ? 0 : parseInt(s, 10)));
  const style = { ...prev };
  for (let i = 0; i < codes.length; i++) {
    const c = codes[i];
    if (c === 0) { for (const k of Object.keys(style)) delete style[k]; }
    else if (c === 1) style.fontWeight = 700;
    else if (c === 2) style.opacity = 0.7;
    else if (c === 3) style.fontStyle = 'italic';
    else if (c === 4) style.textDecoration = 'underline';
    else if (c === 22) { delete style.fontWeight; delete style.opacity; }
    else if (c === 23) delete style.fontStyle;
    else if (c === 24) delete style.textDecoration;
    else if (c === 39) delete style.color;
    else if ((c >= 30 && c <= 37) || (c >= 90 && c <= 97)) style.color = FG[c];
    else if (c === 38) {
      if (codes[i + 1] === 5) { style.color = xterm256(codes[i + 2]); i += 2; }
      else if (codes[i + 1] === 2) { style.color = `rgb(${codes[i + 2] || 0},${codes[i + 3] || 0},${codes[i + 4] || 0})`; i += 4; }
    }
  }
  return style;
}

// Split an ANSI-bearing line into styled segments. Non-SGR escape sequences
// (cursor moves, line clears) are dropped.
const CSI = /\x1b\[[0-9;?]*[A-Za-z]/g;
function parseAnsi(line) {
  const segments = [];
  let style = {};
  let last = 0;
  let m;
  CSI.lastIndex = 0;
  while ((m = CSI.exec(line))) {
    if (m.index > last) segments.push({ text: line.slice(last, m.index), style: { ...style } });
    if (m[0].endsWith('m')) style = applySgr(style, m[0].slice(2, -1));
    last = CSI.lastIndex;
  }
  if (last < line.length) segments.push({ text: line.slice(last), style: { ...style } });
  return segments;
}

// JSON token highlighter — runs over the raw line so original spacing is kept.
const JSON_RE = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
function highlightJson(text) {
  const out = [];
  let last = 0;
  let m;
  let k = 0;
  JSON_RE.lastIndex = 0;
  while ((m = JSON_RE.exec(text))) {
    if (m.index > last) out.push(<span key={k++} className="tj-punc">{text.slice(last, m.index)}</span>);
    if (m[1] !== undefined && m[2] !== undefined) {
      out.push(<span key={k++} className="tj-key">{m[1]}</span>);
      out.push(<span key={k++} className="tj-punc">{m[2]}</span>);
    } else if (m[1] !== undefined) {
      out.push(<span key={k++} className="tj-str">{m[1]}</span>);
    } else if (m[3] !== undefined) {
      out.push(<span key={k++} className="tj-lit">{m[3]}</span>);
    } else if (m[4] !== undefined) {
      out.push(<span key={k++} className="tj-num">{m[4]}</span>);
    }
    last = JSON_RE.lastIndex;
  }
  if (last < text.length) out.push(<span key={k++} className="tj-punc">{text.slice(last)}</span>);
  return out;
}

const CMD_RE = /^\[([^\]]+)\]\s\$\s([\s\S]*)$/;
const META_RE = /^\[([^\]]+)\]\s\((exit\s(\d+)|interactive shell)\)\s*$/;

function looksJson(t) {
  return (t.startsWith('{') && t.endsWith('}')) || (t.startsWith('[') && t.endsWith(']'));
}

function renderOutput(raw) {
  // Approximate carriage-return overwrite (progress bars): keep the last segment.
  let line = raw;
  if (line.includes('\r')) line = line.slice(line.lastIndexOf('\r') + 1);
  if (line.indexOf('\x1b') !== -1) {
    return parseAnsi(line).map((s, i) => <span key={i} style={s.style}>{s.text}</span>);
  }
  const t = line.trim();
  if (looksJson(t)) {
    try { JSON.parse(t); return highlightJson(line); } catch { /* not JSON, fall through */ }
  }
  return line.length ? line : ' ';
}

function TermLine({ raw }) {
  const cmd = CMD_RE.exec(raw);
  if (cmd) {
    return (
      <div className="term-line term-line--cmd">
        <span className="term-ts">{cmd[1]}</span>
        <span className="term-prompt">$</span>
        <span className="term-cmd">{cmd[2]}</span>
      </div>
    );
  }
  const meta = META_RE.exec(raw);
  if (meta) {
    const failed = meta[3] !== undefined && meta[3] !== '0';
    return (
      <div className={`term-line term-line--meta ${failed ? 'is-bad' : 'is-ok'}`}>
        <span className="term-ts">{meta[1]}</span>
        <span className="term-meta-txt">{meta[2]}</span>
      </div>
    );
  }
  return <div className="term-line">{renderOutput(raw)}</div>;
}

export default function TerminalLog({ text = '', live = false, raw = false }) {
  const bodyRef = useRef(null);
  const stickyRef = useRef(true);

  const { lines, truncated } = useMemo(() => {
    const all = text.split('\n');
    if (all.length <= MAX_LINES) return { lines: all, truncated: 0 };
    return { lines: all.slice(-MAX_LINES), truncated: all.length - MAX_LINES };
  }, [text]);

  // Auto-scroll to the bottom unless the user scrolled up.
  useEffect(() => {
    const el = bodyRef.current;
    if (el && stickyRef.current) el.scrollTop = el.scrollHeight;
  }, [text, raw]);

  function onScroll() {
    const el = bodyRef.current;
    if (!el) return;
    stickyRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }

  return (
    <div className="term">
      <div className="term-body" ref={bodyRef} onScroll={onScroll}>
        {truncated > 0 && (
          <div className="term-line term-truncated">… {truncated.toLocaleString()} earlier lines hidden — toggle “raw” for the full tail …</div>
        )}
        {raw ? (
          <pre className="term-raw">{text}</pre>
        ) : (
          lines.map((line, i) => <TermLine key={i} raw={line} />)
        )}
        {live && !raw && <span className="term-cursor" aria-hidden="true" />}
      </div>
    </div>
  );
}
