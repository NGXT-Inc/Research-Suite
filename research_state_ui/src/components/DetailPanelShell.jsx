/**
 * The shared frame for a graph-node detail panel: the type chip, the close
 * button, and the title. Both FigurePanel (ExperimentFigure) and LogicPanel
 * (LogicGraph) wrap their own bodies in this — the bodies keep their separate
 * data models; only the chrome is shared. Reuses the .fig-panel/.fig-panel-*
 * classes, so no CSS changes.
 */
export default function DetailPanelShell({ typeLabel, title, onClose, children }) {
  return (
    <aside className="fig-panel">
      <div className="fig-panel-head">
        <span className="fig-panel-type">{typeLabel}</span>
        <button type="button" className="fig-panel-close" onClick={onClose} aria-label="Close">×</button>
      </div>
      <div className="fig-panel-title">{title}</div>
      {children}
    </aside>
  );
}
