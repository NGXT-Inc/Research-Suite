import { fmtNum, fmtStamp } from '../utils/format';

/**
 * LedgerCharts — dependency-free renderer primitives for the project ledger.
 *
 * All marks are HTML-positioned (percent coordinates) so responsive widths
 * never distort them; the frontier's best-so-far step line is the one SVG
 * element, and being axis-parallel it survives preserveAspectRatio="none".
 * Shared contract: colorOf(i)/sizeOf(i) style run i, onPick(i) selects it.
 */

function Dot({ x, y, color, size = 8, title, onPick }) {
  return (
    <button
      type="button"
      className="lgc-dot"
      style={{ left: `${x}%`, top: `${y}%`, width: size, height: size, background: color }}
      title={title}
      aria-label={title}
      onClick={onPick}
    />
  );
}

const pad = (min, max) => {
  const span = (max - min) || Math.abs(max) || 1;
  return { lo: min - span * 0.07, hi: max + span * 0.07 };
};

// Chronological dots on the focus metric + cumulative-best step line.
export function FrontierChart({ runs, values, direction, focusKey, colorOf, sizeOf, onPick }) {
  if (!values.length) return null;
  const { lo, hi } = pad(Math.min(...values.map(p => p.v)), Math.max(...values.map(p => p.v)));
  const xPct = (j) => (values.length === 1 ? 50 : 4 + (j / (values.length - 1)) * 92);
  const yPct = (v) => 100 - ((v - lo) / (hi - lo)) * 100;

  let best = null;
  const steps = values.map(({ v }, j) => {
    if (best == null || (direction < 0 ? v < best : v > best)) best = v;
    return [xPct(j), yPct(best)];
  });
  const d = steps.map(([x, y], j) => (j === 0 ? `M ${x} ${y}` : `H ${x}${y !== steps[j - 1][1] ? ` V ${y}` : ''}`)).join(' ');

  return (
    <div className="lgc-frontier">
      <span className="lgc-y lgc-y--max">{fmtNum(hi)}</span>
      <span className="lgc-y lgc-y--min">{fmtNum(lo)}</span>
      <div className="lgc-plot">
        {/* The best-so-far line is a directional claim — no direction, no line. */}
        {values.length > 1 && direction !== 0 && (
          <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
            <path d={d} fill="none" stroke="var(--active)" strokeWidth="1.5" opacity="0.75" vectorEffect="non-scaling-stroke" />
          </svg>
        )}
        {values.map(({ i, v }, j) => (
          <Dot
            key={i}
            x={xPct(j)}
            y={yPct(v)}
            color={colorOf(i)}
            size={sizeOf(i)}
            title={`${runs[i].expName} · ${runs[i].runName}\n${focusKey} ${fmtNum(v)}`}
            onPick={() => onPick(i)}
          />
        ))}
      </div>
      {/* The x-axis is time: stamp its ends so the plateau has a duration. */}
      <span className="lgc-xlab lgc-xlab--left">{fmtStamp(runs[values[0].i]?.start)}</span>
      <span className="lgc-xlab">{fmtStamp(runs[values[values.length - 1].i]?.start)} →</span>
    </div>
  );
}

// One metric across all runs on a shared scale; stacked values get a small
// vertical jitter so clusters read as clusters instead of one dot.
export function DotStrip({ runs, fp, colorOf, sizeOf, onPick }) {
  const span = fp.max - fp.min || 1;
  return (
    <div className="lgc-strip">
      {fp.values.map(({ i, v }, j) => (
        <Dot
          key={i}
          x={4 + ((v - fp.min) / span) * 92}
          y={50 + (((j % 3) - 1) * 22)}
          color={colorOf(i)}
          size={sizeOf(i)}
          title={`${runs[i].expName} · ${runs[i].runName}\n${fp.key} ${fmtNum(v)}`}
          onPick={() => onPick(i)}
        />
      ))}
    </div>
  );
}

// One knob (varied param) against the focus metric — numeric scatter or
// categorical columns, decided by the profiler.
export function KnobScatter({ runs, knob, focusKey, colorOf, sizeOf, onPick }) {
  const ys = knob.points.map(p => p.y);
  const { lo, hi } = pad(Math.min(...ys), Math.max(...ys));
  const yPct = (v) => 8 + (1 - (v - lo) / (hi - lo)) * 84;

  let xPct; let labels;
  if (knob.numeric) {
    const xs = knob.points.map(p => p.x);
    const xmin = Math.min(...xs); const xspan = (Math.max(...xs) - xmin) || 1;
    xPct = (p) => 6 + ((p.x - xmin) / xspan) * 88;
    labels = [{ x: 6, text: fmtNum(xmin) }, { x: 94, text: fmtNum(xmin + xspan) }];
  } else {
    const cats = [...new Set(knob.points.map(p => p.cat))].sort();
    xPct = (p) => ((cats.indexOf(p.cat) + 0.5) / cats.length) * 100;
    labels = cats.map((c, ci) => ({ x: ((ci + 0.5) / cats.length) * 100, text: c }));
  }

  return (
    <div className="lgc-knob">
      <div className="lgc-knob-head">
        <span className="lgc-knob-key">{knob.key}</span>
        <span className="lgc-knob-assoc">
          {knob.assoc != null ? `ρ ${knob.assoc.toFixed(2)}` : knob.numeric ? '' : 'categorical'}
        </span>
      </div>
      <div className="lgc-scatter">
        {knob.points.map((p) => (
          <Dot
            key={p.i}
            x={xPct(p)}
            y={yPct(p.y)}
            color={colorOf(p.i)}
            size={sizeOf(p.i)}
            title={`${runs[p.i].expName} · ${runs[p.i].runName}\n${knob.key} ${p.cat} → ${focusKey} ${fmtNum(p.y)}`}
            onPick={() => onPick(p.i)}
          />
        ))}
      </div>
      <div className="lgc-xlabels">
        {labels.map((l) => (
          <span key={l.x} style={{ left: `${l.x}%` }}>{l.text}</span>
        ))}
      </div>
    </div>
  );
}
