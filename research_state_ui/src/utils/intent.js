/**
 * Parse the experiment.intent into { title, brief }.
 *
 * Many MCP flows pack a structured brief into the single `intent` string
 * (Title: / Hypothesis: / Design: …). Rendering all of it as a row title or
 * card heading creates walls of text. Strip the leading "Title:" line for
 * the title; the rest becomes the (optional) brief.
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
  // Single-paragraph intent crammed into one line — surface a leading slice
  // and keep the full text in brief so detail views can still show it.
  return { title: first.slice(0, 120).trim() + '…', brief: intent };
}
