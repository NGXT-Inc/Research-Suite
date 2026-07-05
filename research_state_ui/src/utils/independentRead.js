// Selector for the experiment detail page's lede: the independent reviewer's
// TLDR (`synopsis`) is the first thing a researcher should read. Falls back
// to the experiment's own intent line when no review carries one yet (older
// reviews predate the field, or none exist).
//
// A review showing here can carry any verdict — including needs_changes/fail
// — that's the honest current read ("sent back because…"), not just the
// most flattering one.

function reviewSeq(review) {
  // Prefer an explicit sequence number when the backend provides one;
  // fall back to parsing created_at so older payloads still sort correctly.
  if (typeof review.created_seq === 'number') return review.created_seq;
  const t = Date.parse(review.created_at || '');
  return Number.isFinite(t) ? t : -Infinity;
}

/**
 * pickIndependentRead(reviews, experiment)
 *   -> { kind: 'review', review } | { kind: 'intent', text }
 *
 * `reviews` — all reviews for the experiment (any role, any attempt).
 * `experiment` — used for the intent/headline fallback.
 */
export function pickIndependentRead(reviews, experiment) {
  const withSynopsis = (reviews || [])
    .filter(r => r && typeof r.synopsis === 'string' && r.synopsis.trim().length > 0);

  if (withSynopsis.length > 0) {
    const latest = withSynopsis
      .slice()
      .sort((a, b) => reviewSeq(a) - reviewSeq(b))
      .pop();
    return { kind: 'review', review: latest };
  }

  const text = (experiment?.intent || '').trim() || expHeadline(experiment);
  return { kind: 'intent', text };
}

function expHeadline(experiment) {
  const name = (experiment?.name || '').trim();
  return name || (experiment?.id || '');
}
