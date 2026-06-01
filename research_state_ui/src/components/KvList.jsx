export default function KvList({ rows }) {
  const valid = (rows || []).filter(r => r && r.value !== undefined && r.value !== null && r.value !== '');
  if (valid.length === 0) return null;
  return (
    <dl className="kv-list">
      {valid.map((r, i) => (
        <div key={r.key + ':' + i} className="kv-row">
          <dt className="kv-key">{r.key}</dt>
          <dd className="kv-value">{r.value}</dd>
        </div>
      ))}
    </dl>
  );
}
