import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import LogicGraph from './LogicGraph';
import ReviewCard from './ReviewCard';
import ResourceContentView from './ResourceContentView';
import FSMStrip, { SYNTHESIS_STAGES, SYNTHESIS_GATES, SYNTHESIS_TERMINAL } from './FSMStrip';
import WaveSelector from './synthesis/WaveSelector';
import LensReflectionCard from './synthesis/LensReflectionCard';
import { TERMINAL_WAVE, reflectionsByLens, secondaryDocs, resolveReflectionDoc, docVersion } from './synthesis/waveModel';

/**
 * ProjectSynthesisPanel — the reflection wave, on Home.
 *
 * Attention order, top to bottom: the project logic GRAPH (front and center),
 * the REFLECTION document directly under it (role reflection_doc, rendered
 * inline with its images like an experiment report), then the per-lens
 * REFLECTIONS that fed it. The machine
 * change-spec and the review sit below as quiet disclosures, and the wave
 * "version control" (pan back to older waves) is a muted footer that does not
 * compete with the graph and synthesis.
 *
 * The current wave (open, else latest published) shows by default; panning to a
 * past wave renders it FAITHFULLY from the bytes it pinned (the per-wave /graph
 * endpoint and `?version=` content), not the living files a later wave
 * overwrote. Everything is driven from one polled GET /syntheses call.
 */

function shortDateTime(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  } catch { return ''; }
}

// Quiet disclosure for the secondary artifacts (change_spec, review).
function Collapsible({ label, count, children }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="refl-collapsible">
      <button
        type="button"
        className="btn btn--ghost btn--sm refl-collapsible-toggle"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
      >
        {open ? '▾' : '▸'} {label}{count != null ? ` (${count})` : ''}
      </button>
      {open && <div className="refl-collapsible-body">{children}</div>}
    </div>
  );
}

export default function ProjectSynthesisPanel({ projectId }) {
  const [data, setData] = useState(null);
  const [pinnedId, setPinnedId] = useState(null); // null = follow the live wave
  const [graphAvailable, setGraphAvailable] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const toggleExpand = useCallback(() => setExpanded(v => !v), []);

  const fetchSyntheses = useCallback(async () => {
    try {
      const payload = await api.getSyntheses(projectId);
      setData(prev => (JSON.stringify(prev) === JSON.stringify(payload) ? prev : payload));
    } catch {
      // Non-fatal: Home still works without the panel's metadata.
    }
  }, [projectId]);

  useEffect(() => {
    fetchSyntheses();
    const t = setInterval(fetchSyntheses, 8000);
    return () => clearInterval(t);
  }, [fetchSyntheses]);

  // Same fullscreen affordance as the experiment graphs: Escape or the
  // backdrop collapses, page scroll locks while open.
  useEffect(() => {
    if (!expanded) return undefined;
    const onKey = e => { if (e.key === 'Escape') setExpanded(false); };
    window.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [expanded]);

  const waves = data?.syntheses || [];
  const signal = data?.signal || null;
  const hasAnyWave = waves.length > 0;
  // syntheses arrive oldest-first; current = open wave else latest published.
  const currentId = data?.current?.id || (waves.length ? waves[waves.length - 1].id : null);
  // Follow the live wave unless the user pinned a still-present past wave.
  const selectedId = (pinnedId && waves.some(w => w.id === pinnedId)) ? pinnedId : currentId;
  const selectedIndex = waves.findIndex(w => w.id === selectedId);
  const wave = selectedIndex >= 0 ? waves[selectedIndex] : null;
  const isCurrent = Boolean(wave && wave.id === currentId);
  const isOpen = Boolean(wave && !TERMINAL_WAVE.has(String(wave.status)));

  const onSelectWave = useCallback((id) => {
    // Selecting the current wave resumes "follow live"; any other pins it.
    setPinnedId(id === currentId ? null : id);
  }, [currentId]);
  const backToCurrent = useCallback(() => setPinnedId(null), []);

  const graphFetcher = useCallback(
    () => api.getSynthesisGraph(projectId, selectedId),
    [projectId, selectedId],
  );

  const reflections = useMemo(() => (wave ? reflectionsByLens(wave) : {}), [wave]);
  const roster = wave?.roster || [];
  const waveResources = wave?.current_attempt_resources || [];
  const reviews = wave?.reviews || [];
  const reflectionDoc = resolveReflectionDoc(waveResources);

  // The coverage/staleness signal rides in the graph header — empty until a
  // wave has published.
  const coverageHint = signal?.last_published_at
    ? `covers ${signal.covered_terminal_experiments} of ${signal.terminal_experiments} finished experiments`
    : '';

  if (!hasAnyWave) {
    return (
      <section className="section" id="project-synthesis">
        <div className="section-title">Project synthesis</div>
        <div className="empty-state empty-state--compact">
          <p>No synthesis yet.</p>
        </div>
        {signal?.hint && <div className="syn-hint">{signal.hint}</div>}
      </section>
    );
  }

  return (
    <section className="section" id="project-synthesis">
      <div className="cluster--between" style={{ marginBottom: 10 }}>
        <div className="section-title" style={{ marginBottom: 0 }}>Project synthesis</div>
        {wave && (
          <span className="muted" style={{ fontSize: 'var(--text-xs)' }}>
            Wave {selectedIndex + 1} of {waves.length}
          </span>
        )}
      </div>

      {wave && !isCurrent && (
        <div className="refl-hist-banner">
          <span>Viewing an earlier wave (Wave {selectedIndex + 1}).</span>
          <button type="button" className="refl-hist-back" onClick={backToCurrent}>
            Back to current →
          </button>
        </div>
      )}

      {expanded && (
        <div className="fig-backdrop" onClick={() => setExpanded(false)} aria-hidden="true" />
      )}

      {/* 1 — the graph, front and center */}
      {wave && (
        <LogicGraph
          key={selectedId}
          projectId={projectId}
          fetcher={graphFetcher}
          live={isOpen}
          attemptIndex={wave.attempt_index}
          storyHint={coverageHint}
          problemsGate="submit_synthesis"
          onAvailability={setGraphAvailable}
          expanded={expanded}
          onToggleExpand={toggleExpand}
          readableFit
        />
      )}
      {wave && !graphAvailable && (
        <div className="empty-state empty-state--compact">
          <p>
            {isOpen
              ? "Project graph isn't written yet."
              : 'This wave published no project graph.'}
          </p>
        </div>
      )}

      {/* 2 — the reflection document, prominent (with its images) */}
      {wave && reflectionDoc && (
        <div className="refl-doc">
          <div className="refl-eyebrow">Reflection</div>
          <ResourceContentView
            projectId={projectId}
            resourceId={reflectionDoc.id}
            path={reflectionDoc.path}
            version={docVersion(reflectionDoc)}
            hideSource
            stripTitle
          />
        </div>
      )}

      {/* 3 — the per-lens reflections (inputs to the reflection doc above) */}
      {wave && roster.length > 0 && (
        <div className="refl-block">
          <div className="refl-eyebrow">Lens reflections · {roster.length}</div>
          <div className="refl-roster">
            {roster.map(lens => (
              <LensReflectionCard
                key={lens.id}
                projectId={projectId}
                lens={lens}
                reflection={reflections[lens.id]}
              />
            ))}
          </div>
        </div>
      )}

      {/* secondary, quiet: change spec + other docs, then the review */}
      {wave && secondaryDocs(waveResources).map(({ role, res, label }) => (
        <Collapsible key={role} label={label}>
          <ResourceContentView
            projectId={projectId}
            resourceId={res.id}
            path={res.path}
            version={docVersion(res)}
            hideSource
          />
        </Collapsible>
      ))}
      {wave && reviews.length > 0 && (
        <Collapsible label="Synthesis review" count={reviews.length}>
          {reviews.map(r => <ReviewCard key={r.id} review={r} />)}
        </Collapsible>
      )}

      {/* version control — muted, pan back to older waves */}
      <div className="refl-versions">
        <div className="refl-eyebrow refl-eyebrow--muted">Reflection history</div>
        <WaveSelector
          waves={waves}
          selectedId={selectedId}
          currentId={currentId}
          onSelect={onSelectWave}
        />
        {wave && (
          <div className="refl-versions-meta">
            <FSMStrip
              status={wave.status}
              stages={SYNTHESIS_STAGES}
              gateStates={SYNTHESIS_GATES}
              terminal={SYNTHESIS_TERMINAL}
              ariaLabel="Synthesis lifecycle"
            />
            <div className="refl-meta">
              {wave.attempt_index > 1 && (
                <span className="refl-meta-item">attempt {wave.attempt_index}</span>
              )}
              {wave.published_at
                ? <span className="refl-meta-item">published {shortDateTime(wave.published_at)}</span>
                : wave.created_at && <span className="refl-meta-item">started {shortDateTime(wave.created_at)}</span>}
            </div>
            {wave.revision_context && (
              <div className="refl-revision">↩ {wave.revision_context}</div>
            )}
          </div>
        )}
      </div>

      {signal?.hint && <div className="syn-hint">{signal.hint}</div>}
    </section>
  );
}
