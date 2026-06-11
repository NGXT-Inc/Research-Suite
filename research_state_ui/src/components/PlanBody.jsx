import MarkdownView from './MarkdownView';
import FileRenderer from './FileRenderer';
import { parsePlanSections } from '../utils/planSections';
import { isMarkdown } from '../utils/format';

/**
 * PlanBody — renders an experiment plan with PRD-style progressive disclosure.
 *
 * The plan is the face of the experiment, so the Summary is shown prominently
 * and unframed, the spine (Objective & hypothesis, Evaluation) stays open as
 * labeled sections, and the recommended sections (Method, Outputs, Risks) plus
 * the Attempt log collapse into <details> so a detailed plan stays scannable.
 *
 * Falls back to a plain markdown / file render when the plan doesn't follow the
 * schema (legacy freeform plans, or non-markdown files) — never hides content.
 */
export default function PlanBody({ text, path }) {
  const parsed = isMarkdown(path) ? parsePlanSections(text) : { structured: false };
  if (!parsed.structured) {
    return <FileRenderer text={text} path={path} />;
  }

  const { summary, spine, recommended, log } = parsed;
  return (
    <div className="plan-structured">
      {summary && (
        <div className="plan-summary">
          <MarkdownView text={summary.body} />
        </div>
      )}

      {spine.map(section => (
        <section key={section.heading} className="plan-section plan-section--spine">
          <h3 className="plan-section-head">{section.heading}</h3>
          <div className="plan-section-body">
            <MarkdownView text={section.body} />
          </div>
        </section>
      ))}

      {recommended.map(section => (
        <PlanDetails key={section.heading} section={section} />
      ))}

      {log.map(section => (
        <PlanDetails key={section.heading} section={section} variant="log" />
      ))}
    </div>
  );
}

function PlanDetails({ section, variant }) {
  const empty = !section.body || !section.body.trim();
  return (
    <details className={`plan-section plan-section--details${variant ? ` plan-section--${variant}` : ''}`}>
      <summary className="plan-section-toggle">
        <span className="plan-section-head">{section.heading}</span>
        {empty && <span className="plan-section-empty">empty</span>}
      </summary>
      {!empty && (
        <div className="plan-section-body">
          <MarkdownView text={section.body} />
        </div>
      )}
    </details>
  );
}
