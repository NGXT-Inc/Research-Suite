/**
 * WaveSelector — the reflection-wave history strip.
 *
 * Reflection waves are few and chronological, so a horizontal timeline of
 * pills (newest-first) reads as "history" better than a dropdown. The current
 * wave (open, else latest published) is the default selection; picking a past
 * pill pins it, picking the current pill resumes following the live wave.
 * Abandoned waves stay in the timeline (so history is complete) but are faded.
 */

function shortDate(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' });
  } catch { return ''; }
}

export default function WaveSelector({ waves, selectedId, currentId, onSelect }) {
  // Only worth a selector when there is more than one wave to choose between.
  if (!waves || waves.length <= 1) return null;
  // Newest-first: reflections arrive ordered by created_at ascending.
  const ordered = [...waves].reverse();
  return (
    <div className="refl-waves" role="tablist" aria-label="Reflection waves">
      {ordered.map((w, i) => {
        const n = waves.length - i; // wave number in chronological order
        const status = String(w.status || '');
        const abandoned = status === 'abandoned';
        const isSelected = w.id === selectedId;
        const isCurrent = w.id === currentId;
        const date = shortDate(w.published_at || w.created_at);
        const cls = [
          'refl-wave-pill',
          isSelected ? 'refl-wave-pill--selected' : '',
          abandoned ? 'refl-wave-pill--abandoned' : '',
        ].filter(Boolean).join(' ');
        return (
          <button
            key={w.id}
            type="button"
            role="tab"
            aria-selected={isSelected}
            className={cls}
            onClick={() => onSelect(w.id)}
            title={w.title || `Wave ${n}`}
          >
            <span className={`refl-wave-dot refl-wave-dot--${status}`} aria-hidden="true" />
            <span className="refl-wave-pill-label">
              Wave {n}{isCurrent ? ' · current' : ''}
            </span>
            <span className="refl-wave-pill-meta">
              {status.replace(/_/g, ' ')}{date ? ` · ${date}` : ''}
            </span>
          </button>
        );
      })}
    </div>
  );
}
