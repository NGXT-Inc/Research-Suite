/**
 * rehype plugin: rewrite bare entity ids in rendered markdown prose into
 * <entity-chip> nodes, which MarkdownView maps to an <EntityChip>. This upgrades
 * every markdown surface (report/plan spotlights, review findings, resource .md
 * views) without each having to know about ids.
 *
 * Rules (per the feature spec):
 *  - Never touch fenced code blocks (inside <pre>).
 *  - Inside inline <code>, chip only when the whole span is exactly one id — an
 *    id that is part of a longer token stays plain code.
 *  - Never chip inside an <a> (would nest anchors).
 */
import { ENTITY_ID_RE, ENTITY_ID_EXACT } from './entityResolve';

function chipNode(id) {
  return { type: 'element', tagName: 'entity-chip', properties: { dataId: id }, children: [] };
}

function textContent(node) {
  if (node.type === 'text') return node.value || '';
  return (node.children || []).map(textContent).join('');
}

// Split a prose text value into text + chip nodes, or null if it has no ids.
function splitText(value) {
  ENTITY_ID_RE.lastIndex = 0;
  let m;
  let last = 0;
  let out = null;
  while ((m = ENTITY_ID_RE.exec(value)) !== null) {
    out = out || [];
    if (m.index > last) out.push({ type: 'text', value: value.slice(last, m.index) });
    out.push(chipNode(m[0]));
    last = m.index + m[0].length;
  }
  if (out && last < value.length) out.push({ type: 'text', value: value.slice(last) });
  return out;
}

function walk(node, ctx) {
  if (!node.children || !node.children.length) return;
  const next = [];
  for (const child of node.children) {
    if (child.type === 'text') {
      if (ctx.pre || ctx.anchor) { next.push(child); continue; }
      const parts = splitText(child.value);
      if (parts) next.push(...parts);
      else next.push(child);
      continue;
    }
    if (child.type === 'element') {
      const tag = child.tagName;
      if (tag === 'entity-chip') { next.push(child); continue; }
      // Inline code (not inside a fenced block): a lone id becomes a chip and
      // sheds its code wrapper; any richer span stays untouched code.
      if (tag === 'code' && !ctx.pre && !ctx.anchor) {
        const text = textContent(child).trim();
        next.push(ENTITY_ID_EXACT.test(text) ? chipNode(text) : child);
        continue;
      }
      walk(child, {
        pre: ctx.pre || tag === 'pre',
        anchor: ctx.anchor || tag === 'a',
      });
      next.push(child);
      continue;
    }
    next.push(child);
  }
  node.children = next;
}

export default function rehypeEntityChips() {
  return (tree) => walk(tree, { pre: false, anchor: false });
}
