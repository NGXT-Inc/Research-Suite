import { parseIntentBlock } from '../utils/intent';

/**
 * IntentBlock — the experiment's intent rendered ONCE as a single readable
 * block. No separate header: the whole text sits at one intermediate size
 * (between heading and body) with the opening line slightly weighted and
 * labeled sections (Hypothesis, Design, …) as scannable paragraphs. No
 * truncation — length is fine as long as the structure carries the eye.
 *
 * `compact` is for cards (Home): smaller text, block capped with a fade —
 * the card links to the detail page where the full text lives.
 */
export default function IntentBlock({ intent, compact = false }) {
  const { lead, segments } = parseIntentBlock(intent);
  if (!lead && segments.length === 0) return null;
  return (
    <div className={`intent-block${compact ? ' intent-block--compact' : ''}`}>
      {lead && (compact
        ? <div className="intent-lead">{lead}</div>
        : <p className="intent-seg-text intent-lead-line">{lead}</p>
      )}
      {segments.map((s, i) => (
        <div key={i} className="intent-seg">
          {s.label && <div className="intent-seg-label">{s.label}</div>}
          <p className="intent-seg-text">{s.text}</p>
        </div>
      ))}
    </div>
  );
}
