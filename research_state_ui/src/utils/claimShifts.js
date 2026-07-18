/**
 * Belief shifts reconstructed from the project event log.
 *
 * claim.updated events carry the claim's full new state, so walking the
 * window oldest→newest and diffing against each claim's last-seen state
 * yields real status/confidence transitions; text-only edits drop out.
 * Claims created before the event window have no baseline to diff against,
 * so their first in-window update is skipped rather than guessed at.
 */
export function computeClaimShifts(events) {
  const last = new Map(); // claimId → last-seen {status, confidence, statement}
  const shifts = [];
  const ordered = (events || [])
    .slice()
    .sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
  for (const ev of ordered) {
    if (ev.target_type !== 'claim') continue;
    const type = ev.event_type || ev.type;
    const p = ev.payload || {};
    if (type === 'claim.created') {
      // claim.create defaults; reflection-born claims get corrected by the
      // update that follows within the same wave.
      last.set(ev.target_id, { status: 'active', confidence: 'medium', statement: p.statement });
      continue;
    }
    if (type !== 'claim.updated') continue;
    const before = last.get(ev.target_id);
    const after = { status: p.status, confidence: p.confidence, statement: p.statement };
    last.set(ev.target_id, after);
    if (!before) continue;
    const statusMoved = before.status !== after.status;
    const confMoved = before.confidence !== after.confidence;
    if (!statusMoved && !confMoved) continue;
    shifts.push({
      claimId: ev.target_id,
      statement: p.statement || before.statement || '',
      status: statusMoved ? { from: before.status, to: after.status } : null,
      confidence: confMoved ? { from: before.confidence, to: after.confidence } : null,
      rationale: (p.rationale || '').trim(),
      at: ev.created_at,
    });
  }
  return shifts.reverse(); // newest first
}

/** "today" / "yesterday" / "Nd ago" — the calm relative stamp. */
export function relDays(iso) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '';
  const days = Math.floor((Date.now() - t) / (24 * 60 * 60 * 1000));
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  return `${days}d ago`;
}
