/**
 * Status pill. Maps every experiment/claim/review value to the
 * semantic colour family defined in global.css (.status.{token}).
 *
 * Accepts either a plain status ("running") or a nested status
 * ("submitting.acquiring_sandbox", "queued.waiting_sandbox"). For nested
 * values the head drives the colour and the tail renders as a faint suffix
 * inside the same pill — same shape as before, no extra chip.
 */
export default function StatusPill({ value, pill = true }) {
  if (!value) return null;
  const raw = String(value);
  const dot = raw.indexOf('.');
  const head = dot === -1 ? raw : raw.slice(0, dot);
  const tail = dot === -1 ? '' : raw.slice(dot + 1);
  const cls = ['status'];
  if (pill) cls.push('status--pill');
  cls.push(head.toLowerCase());
  return (
    <span className={cls.join(' ')}>
      {head.replace(/_/g, ' ')}
      {tail && <span className="status-phase">{tail.replace(/_/g, ' ')}</span>}
    </span>
  );
}
