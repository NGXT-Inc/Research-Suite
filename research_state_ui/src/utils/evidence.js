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
export function classifyExperiment(experiment) {
  if (!experiment) return 'inflight';
  const status = experiment.status;
  const reviews = Array.isArray(experiment.reviews) ? experiment.reviews : [];
  const latestExpReview = reviews
    .filter(r => r && (r.role === 'experiment_reviewer' || r.role === 'human'))
    .slice()
    .sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''))
    .pop();

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
