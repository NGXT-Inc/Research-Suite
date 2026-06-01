/**
 * Small monospace ID chip. Shortens `exp_abc12345...` to a stable suffix
 * the eye can scan, with the full id on hover.
 */
export default function ObjId({ id, strong = false, accent = false, className = '' }) {
  if (!id) return null;
  const short = id.length > 14 ? id.slice(0, 4) + '…' + id.slice(-6) : id;
  const cls = ['obj-id'];
  if (strong) cls.push('obj-id--strong');
  if (accent) cls.push('obj-id--accent');
  if (className) cls.push(className);
  return <span className={cls.join(' ')} title={id}>{short}</span>;
}
