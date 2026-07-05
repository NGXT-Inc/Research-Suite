import { fmtAgo } from '../utils/format';

// role → the byline's human name. Anything else (e.g. legacy 'human') falls
// through to a generic "review" so the byline never renders raw snake_case.
const ROLE_LABEL = {
  design_reviewer: 'design review',
  experiment_reviewer: 'experiment review',
};

function ago(iso) {
  const t = Date.parse(iso || '');
  return Number.isFinite(t) ? fmtAgo(Date.now() - t) : null;
}

/**
 * IndependentRead — the page's lede. The reviewer's synopsis (a 1–3 sentence
 * TLDR from the independent reviewer) is the first thing a researcher reads
 * on an experiment; the full report/plan sit below as thread entries.
 *
 * Two shapes, from `pickIndependentRead`:
 *   kind 'review' — byline (who · when) + verdict pill + the synopsis as a
 *                    lede paragraph. Reuses the verdict-pill visual language
 *                    already established by ReviewCard/plan-status so a
 *                    verdict never gets a second, competing style.
 *   kind 'intent' — no reviews carry a synopsis yet (none exist, or they
 *                    predate the field): just the experiment's own intent
 *                    line, in the same lede type, no byline/pill (there's
 *                    no independent read to attribute).
 *
 * No card chrome (One-Surface): flush on the canvas, a hairline below marks
 * the end of the lede before the thread begins.
 */
export default function IndependentRead({ read }) {
  if (!read) return null;

  if (read.kind === 'review') {
    const { review } = read;
    const roleLabel = ROLE_LABEL[review.role] || 'review';
    const verdict = (review.verdict || 'pending').toLowerCase();
    const when = ago(review.created_at);
    return (
      <section id="independent-read" className="ind-read">
        <div className="ind-read-byline">
          <span>Independent read</span>
          <span className="ind-read-sep">·</span>
          <span>{roleLabel}</span>
          {when && (
            <>
              <span className="ind-read-sep">·</span>
              <span>{when}</span>
            </>
          )}
          <span className={`ind-read-verdict review-stepper-pill--${verdict}`}>
            {verdict.replace(/_/g, ' ')}
          </span>
        </div>
        <p className="ind-read-lede">{review.synopsis}</p>
      </section>
    );
  }

  // kind === 'intent'
  if (!read.text) return null;
  return (
    <section id="independent-read" className="ind-read">
      <p className="ind-read-lede">{read.text}</p>
    </section>
  );
}
