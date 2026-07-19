/**
 * Parse a PRD-style experiment plan (plan.md) into classified sections so the
 * UI can render it with progressive disclosure: the Summary becomes the face,
 * the spine (Objective & hypothesis, Evaluation) stays expanded, and the
 * recommended sections (Method, Outputs, Risks) plus the Attempt log collapse.
 *
 * Mirrors the brain schema in merv/src/merv/brain/research_core/experiments.py
 * (REQUIRED_PLAN_SECTIONS) and skills/research-workflow/plan-template.md.
 *
 * Splitting is on H2 (`## `) headings, which is the level the template uses.
 * Plans that don't follow the schema (no H2s, or no recognized sections) are
 * reported as `structured: false` so the caller can fall back to a plain
 * markdown render — this must never throw away content.
 */

const H2_RE = /^##[ \t]+(.+?)[ \t]*#*[ \t]*$/gm;
const H1_RE = /^#[ \t]+(.+?)[ \t]*#*[ \t]*$/m;

// Ordered: classify a heading by the first key its normalized text starts with.
const SECTION_ROLES = [
  { key: 'summary', role: 'summary' },
  { key: 'objective', role: 'spine' },
  { key: 'evaluation', role: 'spine' },
  { key: 'method', role: 'recommended' },
  { key: 'output', role: 'recommended' },
  { key: 'risk', role: 'recommended' },
  { key: 'attempt', role: 'log' },
];

function normalize(s) {
  return (s || '')
    .replace(/&/g, ' and ')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

function classify(normHeading) {
  for (const { key, role } of SECTION_ROLES) {
    if (normHeading.startsWith(key)) return role;
  }
  return 'other';
}

export function parsePlanSections(text) {
  const src = text || '';
  const matches = [...src.matchAll(H2_RE)];
  if (matches.length === 0) {
    return { structured: false };
  }

  const preamble = src.slice(0, matches[0].index);
  const h1 = preamble.match(H1_RE);
  const title = h1 ? h1[1].trim() : '';

  const sections = matches.map((m, i) => {
    const heading = m[1].trim();
    const bodyStart = m.index + m[0].length;
    const bodyEnd = i + 1 < matches.length ? matches[i + 1].index : src.length;
    const norm = normalize(heading);
    return { heading, norm, role: classify(norm), body: src.slice(bodyStart, bodyEnd).trim() };
  });

  const summary = sections.find(s => s.role === 'summary') || null;
  const spine = sections.filter(s => s.role === 'spine');
  // Recommended + any unrecognized author sections, kept in document order.
  const recommended = sections.filter(s => s.role === 'recommended' || s.role === 'other');
  const log = sections.filter(s => s.role === 'log');

  // Only claim "structured" when this actually looks like a schema plan;
  // otherwise the caller renders plain markdown so nothing is hidden.
  const structured = !!summary || spine.length > 0;
  return { structured, title, summary, spine, recommended, log, sections };
}
