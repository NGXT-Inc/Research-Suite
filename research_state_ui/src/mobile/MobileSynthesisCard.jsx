import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';
import { useProjectHref } from '../store/useProjectStore';

/**
 * MobileSynthesisCard — the project synthesis at a glance on the Now screen.
 *
 * One summary card: the current wave's status (or a coverage line), a glance at
 * how many waves exist, and a tap target that pushes the full reflection-wave
 * screen (/synthesis) — graph, reflection doc, lens reflections, and history.
 * When there is no wave at all (only a nudge to run one) it stays a static,
 * non-navigating card. docs/MOBILE_UX_REVIEW.md §4.2.
 */
export default function MobileSynthesisCard({ projectId }) {
  const px = useProjectHref();
  const [meta, setMeta] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api.getSyntheses(projectId).then(d => { if (!cancelled) setMeta(d); }).catch(() => {});
    return () => { cancelled = true; };
  }, [projectId]);

  const waves = meta?.syntheses || [];
  const signal = meta?.signal || null;
  const openWave = meta?.open_synthesis || null;
  const hasAnyWave = waves.length > 0 || Boolean(openWave);

  // Render nothing until there's a wave or a hint worth a glance.
  if (!hasAnyWave && !signal?.hint) return null;

  const headline = openWave
    ? `Reflection ${String(openWave.status || 'in progress').replace(/_/g, ' ')}`
    : 'Project synthesis';

  const coverage = signal?.last_published_at && signal.terminal_experiments > 0
    ? `covers ${signal.covered_terminal_experiments} of ${signal.terminal_experiments} finished`
    : null;
  const waveCount = waves.length
    ? `${waves.length} wave${waves.length > 1 ? 's' : ''}`
    : null;

  const inner = (
    <>
      <div className="mcard-head">
        <div className="mcard-title">{headline}</div>
        {hasAnyWave && <span className="mcard-glyph" style={{ color: 'var(--mcp)' }} aria-hidden="true">◆</span>}
      </div>
      {signal?.hint && <div className="mcard-sub">{signal.hint}</div>}
      {hasAnyWave && (
        <div className="mcard-meta">
          {coverage && <span>{coverage}</span>}
          {waveCount && <span>{waveCount}</span>}
          <span>tap to open →</span>
        </div>
      )}
    </>
  );

  return (
    <section className="section">
      <div className="section-title">Synthesis</div>
      {hasAnyWave ? (
        <Link to={px('/synthesis')} className="mcard">{inner}</Link>
      ) : (
        <div className="mcard" aria-disabled="true">{inner}</div>
      )}
    </section>
  );
}
