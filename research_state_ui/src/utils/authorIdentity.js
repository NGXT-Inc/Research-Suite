// Stable visual identity for feed authors. A handle hashes to a hue rendered
// at fixed low chroma/lightness, so every byline gets a distinct but equally
// quiet voice on the dark canvas — identity without competing with the
// semantic tokens (--supports/--qualifies/--refutes/--active stay meaningful).

export function authorHue(handle) {
  let h = 0;
  for (const ch of String(handle || '')) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return h % 360;
}

export function authorColor(handle) {
  return `oklch(78% 0.09 ${authorHue(handle)})`;
}
