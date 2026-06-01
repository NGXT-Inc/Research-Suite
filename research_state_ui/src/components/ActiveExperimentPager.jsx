import { parseIntent } from '../utils/intent';

/**
 * Pager for the "What's going on now" spotlight when there are multiple
 * active experiments. Renders chevrons flanking a row of status-tinted dots:
 *
 *     ‹ • • ○ ○ ›
 *
 * Hidden when items.length <= 1. The selected dot is filled; others outlined.
 * Each dot picks up its experiment's status color so the user can scan the
 * shape of the queue at a glance (running vs awaiting review vs planned).
 *
 * Props:
 *   items          Array of active experiments (each with id, intent, status)
 *   index          Currently-selected index
 *   onChange       (newIndex) => void
 */
export default function ActiveExperimentPager({ items, index, onChange }) {
  if (!items || items.length <= 1) return null;
  const total = items.length;
  const safeIdx = Math.max(0, Math.min(index, total - 1));
  const atStart = safeIdx === 0;
  const atEnd = safeIdx === total - 1;

  return (
    <div className="exp-pager" role="tablist" aria-label="Active experiments">
      <button
        type="button"
        className="exp-pager-chevron"
        onClick={() => onChange(safeIdx - 1)}
        disabled={atStart}
        aria-label="Previous active experiment"
      >‹</button>

      <div className="exp-pager-dots">
        {items.map((exp, i) => {
          const status = String(exp?.status || '').toLowerCase();
          const { title } = parseIntent(exp?.intent);
          const cls = [
            'exp-pager-dot',
            `exp-pager-dot--${status || 'unknown'}`,
            i === safeIdx ? 'exp-pager-dot--selected' : '',
          ].filter(Boolean).join(' ');
          return (
            <button
              key={exp.id || i}
              type="button"
              className={cls}
              onClick={() => onChange(i)}
              aria-label={`${title || exp.id} — ${status}`}
              aria-selected={i === safeIdx}
              role="tab"
              title={`${title || exp.id} · ${status}`}
            />
          );
        })}
      </div>

      <button
        type="button"
        className="exp-pager-chevron"
        onClick={() => onChange(safeIdx + 1)}
        disabled={atEnd}
        aria-label="Next active experiment"
      >›</button>

      <span className="exp-pager-count">{safeIdx + 1} / {total}</span>
    </div>
  );
}
