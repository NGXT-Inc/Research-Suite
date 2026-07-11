/**
 * Outcome classification used by the Logic DAG.
 *
 * Every experiment is bucketed into one of:
 *
 *   supports   — passed experiment review (status complete)
 *   refutes    — last experiment review failed
 *   qualifies  — last experiment review said needs_changes
 *   inflight   — still moving through the FSM
 *   abandoned  — failed or abandoned without a conclusive experiment review
 *
 * Design-review verdicts deliberately don't count here — they gate
 * execution, not evidence.
 */
/**
 * The verdict-bearing review: the newest experiment-level review (agent or
 * human). Shared so every consumer (outcome classification, the story's
 * reviewer synopsis and verdict timestamps) picks the SAME review.
 */
export function latestExperimentReview(experiment) {
  const reviews = Array.isArray(experiment?.reviews) ? experiment.reviews : [];
  let latest = null;
  for (const r of reviews) {
    if (!r || (r.role !== 'experiment_reviewer' && r.role !== 'human')) continue;
    if (!latest || (r.created_at || '').localeCompare(latest.created_at || '') > 0) latest = r;
  }
  return latest;
}

export function classifyExperiment(experiment) {
  if (!experiment) return 'inflight';
  const status = experiment.status;
  const latestExpReview = latestExperimentReview(experiment);

  if (status === 'complete') return 'supports';
  if (status === 'failed') {
    if (latestExpReview?.verdict === 'fail') return 'refutes';
    if (latestExpReview?.verdict === 'needs_changes') return 'qualifies';
    return 'abandoned';
  }
  if (status === 'abandoned') return 'abandoned';
  if (latestExpReview?.verdict === 'needs_changes') return 'qualifies';
  return 'inflight';
}

const OUTCOME_PALETTE = {
  supports:  'var(--supports)',
  refutes:   'var(--refutes)',
  qualifies: 'var(--qualifies)',
  inflight:  'var(--active)',
  abandoned: 'var(--faint)',
};
export function outcomeColor(outcome) {
  return OUTCOME_PALETTE[outcome] || 'var(--faint)';
}

const OUTCOME_LABELS = {
  supports:  'supports',
  refutes:   'refutes',
  qualifies: 'qualifies',
  inflight:  'in flight',
  abandoned: 'abandoned',
};
export function outcomeLabel(outcome) {
  return OUTCOME_LABELS[outcome] || outcome;
}

const OUTCOME_GLYPHS = {
  supports:  '✓',
  refutes:   '✗',
  qualifies: '?',
  inflight:  '◐',
  abandoned: '·',
};
export function outcomeGlyph(outcome) {
  return OUTCOME_GLYPHS[outcome] || '·';
}

// A claim's own lifecycle color — the same palette as experiment outcomes,
// since "supported"/"contradicted"/"weakened" are the claim-side names for
// exactly the evidence buckets classifyExperiment already computes.
const CLAIM_STATUS_PALETTE = {
  supported:    'var(--supports)',
  contradicted: 'var(--refutes)',
  weakened:     'var(--qualifies)',
  active:       'var(--active)',
  abandoned:    'var(--faint)',
};
export function claimStatusColor(status) {
  return CLAIM_STATUS_PALETTE[(status || '').toLowerCase()] || 'var(--steel)'; // draft
}

/**
 * Tally a claim's linked experiments into plain-word evidence buckets for
 * the claims ledger: "2 for · 1 against · 1 running". Abandoned experiments
 * are effort, not evidence — they show on the detail page only.
 */
export function tallyOutcomes(experiments) {
  const tally = { for: 0, against: 0, unclear: 0, running: 0 };
  for (const e of experiments || []) {
    const outcome = classifyExperiment(e);
    if (outcome === 'supports') tally.for += 1;
    else if (outcome === 'refutes') tally.against += 1;
    else if (outcome === 'qualifies') tally.unclear += 1;
    else if (outcome === 'inflight') tally.running += 1;
  }
  return tally;
}
