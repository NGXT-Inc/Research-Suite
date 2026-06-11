/**
 * Parse the experiment.intent into { title, brief }.
 *
 * Many MCP flows pack a structured brief into the single `intent` string
 * (Title: / Hypothesis: / Design: …). Rendering all of it as a row title or
 * card heading creates walls of text. Strip the leading "Title:" line for
 * the title; the rest becomes the (optional) brief.
 *
 * Title and brief never overlap: views render them stacked (heading, then
 * lede), so the brief is always the remainder *after* the title.
 */
export function parseIntent(raw) {
  const intent = (raw || '').trim();
  if (!intent) return { title: '', brief: '' };
  const lines = intent.split('\n');

  for (let i = 0; i < Math.min(3, lines.length); i++) {
    const m = lines[i].match(/^Title:\s*(.+)$/i);
    if (m) {
      let bodyStart = i + 1;
      while (bodyStart < lines.length && !lines[bodyStart].trim()) bodyStart++;
      return { title: m[1].trim(), brief: lines.slice(bodyStart).join('\n').trim() };
    }
  }
  const first = lines[0];
  if (intent.length <= 140 && lines.length === 1) {
    return { title: first.trim(), brief: '' };
  }
  if (first.length <= 140) {
    let bodyStart = 1;
    while (bodyStart < lines.length && !lines[bodyStart].trim()) bodyStart++;
    return { title: first.trim(), brief: lines.slice(bodyStart).join('\n').trim() };
  }
  // Single-paragraph intent crammed into one long line — no Title: line and
  // no short first line to lift. Prefer the first sentence as the title when
  // one ends within heading range; otherwise cut at a word boundary. Either
  // way the brief picks up where the title left off.
  const sentence = first.match(/^(.{20,160}?[.!?]["')\]]*)\s+(?=["'(]?[A-Z])/);
  if (sentence) {
    return { title: sentence[1].trim(), brief: intent.slice(sentence[0].length).trim() };
  }
  const cut = first.slice(0, 120).replace(/\s+\S*$/, '');
  return { title: cut.trim() + '…', brief: '…' + intent.slice(cut.length).trim() };
}

// A line that opens a labeled section ("Hypothesis: …", "Design: …").
// Requires a leading capital so URLs ("https:") and ids never match.
const SECTION_RE = /^([A-Z][\w /&()'-]{1,30}):\s*(.*)$/;

/**
 * Parse the experiment.intent into { lead, segments } for full single-block
 * rendering (IntentBlock): the whole intent shown ONCE, structured.
 *
 * - Lines like "Hypothesis: …" start a labeled segment; following unlabeled
 *   lines belong to it.
 * - A "Title:" segment is lifted out as the lead. Without one there is NO
 *   lead — the opening text stays whole. Never split free text mid-sentence:
 *   the block must read as one continuous piece.
 */
export function parseIntentBlock(raw) {
  const intent = (raw || '').trim();
  if (!intent) return { lead: '', segments: [] };

  const segments = [];
  let current = null;
  for (const line of intent.split('\n')) {
    const m = line.match(SECTION_RE);
    if (m) {
      if (current) segments.push(current);
      current = { label: m[1], lines: m[2] ? [m[2]] : [] };
    } else {
      if (!current) current = { label: null, lines: [] };
      current.lines.push(line);
    }
  }
  if (current) segments.push(current);

  let segs = segments
    .map(s => ({ label: s.label, text: s.lines.join('\n').trim() }))
    .filter(s => s.text);

  let lead = '';
  const titleIdx = segs.findIndex(s => (s.label || '').toLowerCase() === 'title');
  if (titleIdx >= 0) {
    lead = segs[titleIdx].text;
    segs.splice(titleIdx, 1);
  }
  return { lead, segments: segs };
}
