import { useCallback, useEffect, useState } from 'react';
import ExperimentFigure from './ExperimentFigure';
import LogicGraph from './LogicGraph';

/**
 * ExperimentGraphs — one canvas slot, two graphs.
 *
 * The derived figure (experiment state) and the agent-authored logic graph
 * (the experiment's story) share the same space on the page. The section
 * title IS the toggle: the active view's name renders as the title, the
 * other sits beside it muted and clickable. Both children stay mounted so
 * each keeps its own polling and reports availability — only the active one
 * renders DOM, which also re-runs react-flow's measure pass on every
 * switch. If the chosen graph has nothing to show, the other one is
 * displayed instead (the tab for an empty graph disables).
 */
export default function ExperimentGraphs({ projectId, experimentId, experimentStatus, attemptIndex }) {
  const [chosen, setChosen] = useState('figure');
  const [avail, setAvail] = useState({ figure: false, logic: false });
  // Expanded (near-fullscreen) mode lives here so it survives switching
  // between the two graphs while fullscreen. Escape or the backdrop closes
  // it; page scroll is locked while it is open.
  const [expanded, setExpanded] = useState(false);
  const toggleExpand = useCallback(() => setExpanded(v => !v), []);

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

  const report = useCallback((key, value) => {
    setAvail(prev => (prev[key] === value ? prev : { ...prev, [key]: value }));
  }, []);
  const reportFigure = useCallback(v => report('figure', v), [report]);
  const reportLogic = useCallback(v => report('logic', v), [report]);

  const view = avail[chosen]
    ? chosen
    : (chosen === 'figure' ? (avail.logic ? 'logic' : null) : (avail.figure ? 'figure' : null));

  const titleTabs = (
    <span className="fig-title-tabs" role="tablist" aria-label="Graph view">
      <button
        type="button"
        role="tab"
        aria-selected={view === 'figure'}
        className={`fig-title-tab${view === 'figure' ? ' fig-title-tab--on' : ''}`}
        disabled={!avail.figure}
        onClick={() => setChosen('figure')}
      >
        Figure
      </button>
      <span className="fig-title-tab-sep" aria-hidden="true">/</span>
      <button
        type="button"
        role="tab"
        aria-selected={view === 'logic'}
        className={`fig-title-tab${view === 'logic' ? ' fig-title-tab--on' : ''}`}
        disabled={!avail.logic}
        onClick={() => setChosen('logic')}
      >
        Logic
      </button>
    </span>
  );

  const shared = {
    projectId, experimentId, experimentStatus, attemptIndex,
    titleTabs, expanded, onToggleExpand: toggleExpand,
  };
  return (
    <>
      {expanded && (
        <div
          className="fig-backdrop"
          onClick={() => setExpanded(false)}
          aria-hidden="true"
        />
      )}
      <ExperimentFigure
        {...shared}
        active={view === 'figure'}
        onAvailability={reportFigure}
      />
      <LogicGraph
        {...shared}
        active={view === 'logic'}
        onAvailability={reportLogic}
      />
    </>
  );
}
