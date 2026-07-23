import { Link } from 'react-router-dom';
import { useProjectHref } from '../store/useProjectStore';

const REF_ICON = { exp: '⧉', paper: '¶', claim: '✦', res: '▣', sbx: '▣' };
// Object panels: header word + tone per satellite type.
const OBJ_WORD = { paper: 'paper', claim: 'claim', sbx: 'sandbox' };
const OBJ_TONE = { paper: 'supports', claim: 'qualifies', sbx: 'sbx' };

function lookupObject(objects, type, id) {
  if (type === 'paper') return objects.papers?.[id];
  if (type === 'claim') return objects.claims?.[id];
  return objects.sandboxes?.[id];
}

// Experiments that cite this object — via refs, satellites, or sandbox usage.
function referencedBy(cards, type, id) {
  const types = type === 'sbx' ? ['sbx', 'art'] : [type];
  return cards.filter((c) => (
    (type === 'sbx' && (c.sbxIds || []).includes(id))
    || (c.refs || []).some((r) => types.includes(r.type) && r.id === id)
    || (c.sats || []).some((s) => types.includes(s.type) && s.id === id)
  ));
}

function Eyebrow({ children }) {
  return <div className="xmap-eyebrow">{children}</div>;
}

/**
 * One reference row. A div with button semantics rather than <button> so the
 * paper rows can nest a real external link without invalid markup.
 */
function RefRow({ icon, iconClass, label, sub, action, href, onOpen }) {
  return (
    <div
      className="xmap-ref"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen?.(); } }}
    >
      <span className={`xmap-ref-ic ${iconClass}`} aria-hidden="true">{icon}</span>
      <span className="xmap-ref-main">
        <span className="xmap-ref-label">{label}</span>
        {sub ? <span className="xmap-ref-sub">{sub}</span> : null}
      </span>
      <span className="xmap-ref-actions">
        {action ? <span className="xmap-ref-action">{action}</span> : null}
        {href ? (
          <a
            className="xmap-ref-ext"
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
          >
            open ↗
          </a>
        ) : null}
      </span>
    </div>
  );
}

function ExpRow({ card, onTransport }) {
  return (
    <RefRow
      icon="⧉"
      iconClass="xmap-ic--exp"
      label={card.id}
      sub={card.title}
      action="go →"
      onOpen={() => onTransport(card.id)}
    />
  );
}

function PanelShell({ id, idHref, tone, word, pulse, onClose, children }) {
  return (
    <div className="xmap-panel">
      <div className="xmap-panel-head">
        {idHref
          ? <Link className="xmap-panel-id" to={idHref}>{id}</Link>
          : <span className="xmap-panel-id">{id}</span>}
        <span className={`xmap-panel-status xmap-tone--${tone}`}>
          <span className={`xmap-dot${pulse ? ' xmap-dot--pulse' : ''}`} />
          {word}
        </span>
        <button type="button" className="xmap-panel-close" onClick={onClose} aria-label="Close panel">✕</button>
      </div>
      <div className="xmap-panel-body">{children}</div>
    </div>
  );
}

function ExperimentPanel({ card, cards, objects, citedBy, onClose, onTransport, onSelectObject }) {
  const px = useProjectHref();
  const cited = (citedBy[card.id] || [])
    .map((id) => cards.find((c) => c.id === id))
    .filter(Boolean);
  const meta = [
    card.artifacts != null ? `${card.artifacts} artifacts` : null,
    card.agent || null,
    card.computeStr || null,
    // Count, not uids — prod uids are 32-hex; the sandbox panel shows the full id.
    card.sbxIds?.length ? `${card.sbxIds.length} sandbox${card.sbxIds.length === 1 ? '' : 'es'}` : null,
  ].filter(Boolean);

  return (
    <PanelShell
      id={card.id}
      idHref={px(`/experiments/${card.id}`)}
      tone={card.status}
      word={card.status}
      pulse={card.status === 'running'}
      onClose={onClose}
    >
      <div>
        <div className="xmap-panel-title">{card.title}</div>
        {card.tldr ? <div className="xmap-panel-text">{card.tldr}</div> : null}
      </div>
      {(card.metrics || []).length > 0 && (
        <div>
          <Eyebrow>Result</Eyebrow>
          <div className="xmap-metrics">
            {card.metrics.map((m, i) => (
              <div className="xmap-metric" key={`${m.label}:${i}`}>
                <div className="xmap-metric-value">{m.value}</div>
                <div className="xmap-metric-label">{m.label}</div>
              </div>
            ))}
          </div>
        </div>
      )}
      {(card.gates || []).length > 0 && (
        <div>
          <Eyebrow>Gates</Eyebrow>
          <div className="xmap-gates">
            {card.gates.map((g, i) => (
              <div className="xmap-gate" key={`${g.label}:${i}`}>
                <span className="xmap-gate-label">{g.label}</span>
                <span className="xmap-gate-leader" />
                <span className={`xmap-tone--${g.tone}`}>{g.result}</span>
              </div>
            ))}
          </div>
        </div>
      )}
      {(card.refs || []).length > 0 && (
        <div>
          <Eyebrow>References</Eyebrow>
          <div className="xmap-refs">
            {card.refs.map((r, i) => {
              if (r.type === 'exp') {
                // Prototype grammar: id as the label, title as the sub line.
                return (
                  <RefRow
                    key={`${r.type}:${r.id}:${i}`}
                    icon="⧉"
                    iconClass="xmap-ic--exp"
                    label={r.id}
                    sub={r.label || r.sub}
                    action="go →"
                    onOpen={() => onTransport(r.id)}
                  />
                );
              }
              const obj = lookupObject(objects, r.type, r.id);
              return (
                <RefRow
                  key={`${r.type}:${r.id}:${i}`}
                  icon={REF_ICON[r.type] || '▣'}
                  iconClass={`xmap-ic--${r.type}`}
                  label={r.label || obj?.title || r.id}
                  sub={r.sub || obj?.sub}
                  action={obj ? 'view →' : null}
                  href={r.type === 'paper' ? obj?.url : null}
                  onOpen={obj ? () => onSelectObject(r.type, r.id) : undefined}
                />
              );
            })}
          </div>
        </div>
      )}
      {cited.length > 0 && (
        <div>
          <Eyebrow>Cited by</Eyebrow>
          <div className="xmap-refs">
            {cited.map((c) => <ExpRow key={c.id} card={c} onTransport={onTransport} />)}
          </div>
        </div>
      )}
      {meta.length > 0 && <div className="xmap-panel-meta">{meta.join(' · ')}</div>}
    </PanelShell>
  );
}

function ObjectPanel({ sel, cards, objects, onClose, onTransport }) {
  const obj = lookupObject(objects, sel.type, sel.id);
  if (!obj) return null;
  const refBy = referencedBy(cards, sel.type, sel.id);
  return (
    <PanelShell
      id={sel.type === 'paper' ? `arXiv ${sel.id}` : sel.id}
      tone={OBJ_TONE[sel.type] || 'sbx'}
      word={OBJ_WORD[sel.type] || sel.type}
      onClose={onClose}
    >
      <div>
        <div className="xmap-panel-title">{obj.title}</div>
        <div className="xmap-panel-text">{[obj.detail, obj.sub].filter(Boolean).join(' — ')}</div>
        {obj.url ? (
          <a className="xmap-ref-ext xmap-panel-ext" href={obj.url} target="_blank" rel="noopener noreferrer">
            open ↗
          </a>
        ) : null}
      </div>
      {refBy.length > 0 && (
        <div>
          <Eyebrow>Referenced by</Eyebrow>
          <div className="xmap-refs">
            {refBy.map((c) => <ExpRow key={c.id} card={c} onTransport={onTransport} />)}
          </div>
        </div>
      )}
    </PanelShell>
  );
}

/**
 * MapPanel — the 380px detail panel docked to the right of the map area.
 * Experiment selections show tldr, metrics, gates, references, citations,
 * and the compute footer; object selections (paper/claim/sandbox) show
 * their detail plus every experiment that references them.
 */
export default function MapPanel({ sel, cards, objects, citedBy, onClose, onTransport, onSelectObject }) {
  if (sel.type === 'exp') {
    const card = cards.find((c) => c.id === sel.id);
    if (!card) return null;
    return (
      <ExperimentPanel
        card={card}
        cards={cards}
        objects={objects}
        citedBy={citedBy}
        onClose={onClose}
        onTransport={onTransport}
        onSelectObject={onSelectObject}
      />
    );
  }
  return (
    <ObjectPanel
      sel={sel}
      cards={cards}
      objects={objects}
      onClose={onClose}
      onTransport={onTransport}
    />
  );
}
