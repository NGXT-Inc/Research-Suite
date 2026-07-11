// Shared display formatters. Keep these dumb and dependency-free.

// Up to 4 significant digits; integers stay integers; non-numbers pass through.
export function fmtNum(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v ?? '');
  if (Number.isInteger(v)) return String(v);
  return Number(v.toPrecision(4)).toString();
}

export function formatBytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  let v = n / 1024;
  for (const u of ['KB', 'MB', 'GB', 'TB']) {
    if (v < 1024 || u === 'TB') return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${u}`;
    v /= 1024;
  }
}

// Parse an ISO stamp to epoch ms, or null when absent/unparsable.
export function parseTs(iso) {
  const t = Date.parse(iso || '');
  return Number.isFinite(t) ? t : null;
}

// Compact absolute stamp for chronological scanning ("Jul 1, 21:05").
// 24-hour on purpose: fixed width, sorts visually.
export function fmtStamp(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return '';
  return new Date(ms).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
}

export function tsToTime(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch { return iso; }
}

export function fmtAgo(ms) {
  if (ms == null || !Number.isFinite(ms)) return '—';
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function fmtDuration(ms) {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '—';
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

// Split timestamp for compact two-line table cells: "Jun 11" over "1:36 PM".
export function fmtDayTime(iso) {
  if (!iso) return null;
  try {
    const d = new Date(iso);
    const sameYear = d.getFullYear() === new Date().getFullYear();
    return {
      day: d.toLocaleDateString([], {
        month: 'short',
        day: 'numeric',
        ...(sameYear ? {} : { year: 'numeric' }),
      }),
      time: d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }),
    };
  } catch { return null; }
}

export function isMarkdown(path) {
  const ext = (path || '').split('.').pop().toLowerCase();
  return ext === 'md' || ext === 'markdown' || ext === 'mdx';
}
