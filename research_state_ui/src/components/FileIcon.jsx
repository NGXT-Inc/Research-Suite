/**
 * FileIcon — small inline-SVG icon for a file, picked by extension.
 *
 * Kept handwritten (not a library) so the bundle stays tight. Each glyph is
 * 16×16, currentColor-driven, with a per-extension color class. Folder rows
 * use a chevron only and don't render a FileIcon.
 */

const sz = { width: 14, height: 14, viewBox: '0 0 16 16' };

// Markdown — letter "M" with a down arrow (mimics GitHub's MD glyph).
function IconMd() {
  return (
    <svg {...sz} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1" y="3" width="14" height="10" rx="1.5" />
      <path d="M3.5 11V5l2 2.4L7.5 5v6" />
      <path d="M11 5v5M9.5 8.5L11 10.5l1.5-2" />
    </svg>
  );
}

// React (.jsx/.tsx) — three crossed ellipses + nucleus.
function IconReact() {
  return (
    <svg {...sz} fill="none" stroke="currentColor" strokeWidth="1.1">
      <ellipse cx="8" cy="8" rx="6.4" ry="2.4" />
      <ellipse cx="8" cy="8" rx="6.4" ry="2.4" transform="rotate(60 8 8)" />
      <ellipse cx="8" cy="8" rx="6.4" ry="2.4" transform="rotate(120 8 8)" />
      <circle cx="8" cy="8" r="1.1" fill="currentColor" stroke="none" />
    </svg>
  );
}

// Python — two interlocking blocks (echo of the Py logo).
function IconPy() {
  return (
    <svg {...sz} fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round">
      <path d="M5 2.5h3.5a2 2 0 0 1 2 2V8H5.5a1.5 1.5 0 0 0-1.5 1.5v1A1.5 1.5 0 0 0 5.5 12H7" />
      <path d="M11 13.5H7.5a2 2 0 0 1-2-2V8h5a1.5 1.5 0 0 1 1.5 1.5v1A1.5 1.5 0 0 1 10.5 12H9" />
      <circle cx="6.5" cy="4.5" r="0.6" fill="currentColor" stroke="none" />
      <circle cx="9.5" cy="11.5" r="0.6" fill="currentColor" stroke="none" />
    </svg>
  );
}

// Plain "JS"/"TS" badge (just a letter rendered into a box).
function IconLetters({ letters }) {
  return (
    <svg {...sz} fill="none">
      <rect x="1" y="3" width="14" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.2" />
      <text x="8" y="11" textAnchor="middle" fontSize="6.5" fontWeight="700" fontFamily="-apple-system, sans-serif" fill="currentColor">{letters}</text>
    </svg>
  );
}

// JSON / YAML / TOML — curly-brace pair.
function IconCfg() {
  return (
    <svg {...sz} fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5.5 3c-1.2 0-1.7.6-1.7 1.6V7c0 .7-.3 1-1 1 .7 0 1 .3 1 1v2.4c0 1 .5 1.6 1.7 1.6" />
      <path d="M10.5 3c1.2 0 1.7.6 1.7 1.6V7c0 .7.3 1 1 1-.7 0-1 .3-1 1v2.4c0 1-.5 1.6-1.7 1.6" />
    </svg>
  );
}

// CSV / TSV — small grid.
function IconData() {
  return (
    <svg {...sz} fill="none" stroke="currentColor" strokeWidth="1.2">
      <rect x="1.5" y="3" width="13" height="10" rx="1" />
      <path d="M1.5 6.5h13M1.5 10h13M5.5 3v10M10 3v10" />
    </svg>
  );
}

// HTML — angle brackets.
function IconHtml() {
  return (
    <svg {...sz} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5.5 4 2 8l3.5 4" />
      <path d="M10.5 4 14 8l-3.5 4" />
    </svg>
  );
}

// CSS — hash mark.
function IconCss() {
  return (
    <svg {...sz} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
      <path d="M5.5 3 4.5 13M11 3l-1 10M3 6h11M2.5 10h11" />
    </svg>
  );
}

// Shell — `>` prompt.
function IconShell() {
  return (
    <svg {...sz} fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1.5" y="3" width="13" height="10" rx="1.5" />
      <path d="M4 7.5 6 9l-2 1.5M7.5 11.5h4" />
    </svg>
  );
}

// Default — document with a folded corner.
function IconDoc() {
  return (
    <svg {...sz} fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round">
      <path d="M3 1.5h6.5l3 3V14a.5.5 0 0 1-.5.5H3a.5.5 0 0 1-.5-.5V2a.5.5 0 0 1 .5-.5z" />
      <path d="M9.5 1.5v3h3" />
    </svg>
  );
}

const MAP = {
  md:       { Icon: IconMd,      cls: 'md' },
  markdown: { Icon: IconMd,      cls: 'md' },
  mdx:      { Icon: IconMd,      cls: 'md' },
  jsx:      { Icon: IconReact,   cls: 'react' },
  tsx:      { Icon: IconReact,   cls: 'react' },
  js:       { Icon: () => <IconLetters letters="JS" />, cls: 'js' },
  mjs:      { Icon: () => <IconLetters letters="JS" />, cls: 'js' },
  cjs:      { Icon: () => <IconLetters letters="JS" />, cls: 'js' },
  ts:       { Icon: () => <IconLetters letters="TS" />, cls: 'ts' },
  py:       { Icon: IconPy,      cls: 'py' },
  ipynb:    { Icon: IconPy,      cls: 'py' },
  json:     { Icon: IconCfg,     cls: 'cfg' },
  yaml:     { Icon: IconCfg,     cls: 'cfg' },
  yml:      { Icon: IconCfg,     cls: 'cfg' },
  toml:     { Icon: IconCfg,     cls: 'cfg' },
  csv:      { Icon: IconData,    cls: 'data' },
  tsv:      { Icon: IconData,    cls: 'data' },
  html:     { Icon: IconHtml,    cls: 'html' },
  htm:      { Icon: IconHtml,    cls: 'html' },
  css:      { Icon: IconCss,     cls: 'css' },
  scss:     { Icon: IconCss,     cls: 'css' },
  sh:       { Icon: IconShell,   cls: 'shell' },
  bash:     { Icon: IconShell,   cls: 'shell' },
  zsh:      { Icon: IconShell,   cls: 'shell' },
};

export default function FileIcon({ name }) {
  const ext = String(name || '').split('.').pop().toLowerCase();
  const cfg = MAP[ext] || { Icon: IconDoc, cls: 'default' };
  const { Icon } = cfg;
  return (
    <span className={`ft-fi ft-fi--${cfg.cls}`} aria-hidden="true">
      <Icon />
    </span>
  );
}
