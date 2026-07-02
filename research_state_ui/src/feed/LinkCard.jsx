import { feedApi } from './feedApi';

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; }
}

// "Loshchilov, Ilya" / "Ilya Loshchilov" -> "Loshchilov"; first three + count.
function authorLine(authors) {
  const last = authors
    .map((a) => (a.includes(',') ? a.split(',')[0] : a.split(' ').pop()).trim())
    .filter(Boolean);
  if (!last.length) return '';
  const shown = last.slice(0, 3).join(', ');
  return last.length > 3 ? `${shown} +${last.length - 3}` : shown;
}

// github.com/owner/repo/... -> "owner/repo" — tighter than GitHub's og:title.
function repoSlug(url) {
  try {
    const parts = new URL(url).pathname.split('/').filter(Boolean);
    return parts.length >= 2 ? `${parts[0]}/${parts[1]}` : null;
  } catch { return null; }
}

/**
 * The unfurled-link card under a post. One shape, three voices: papers lead
 * with citation metadata (authors, year), repos with the owner/repo slug in
 * mono, everything else with the page's own og card. A failed unfurl
 * degrades to the bare link.
 */
export default function LinkCard({ post, preview, thumbUrl, projectId }) {
  const track = () =>
    feedApi.trackFeed(projectId, 'link_clicked', { post_id: post.id }).catch(() => {});

  if (!preview || preview.error) {
    return (
      <a
        className="postcard-link postcard-link--bare"
        href={post.link_url}
        target="_blank"
        rel="noopener noreferrer nofollow"
        onClick={track}
      >
        {post.link_url}
      </a>
    );
  }

  const kind = preview.kind || 'page';
  const authors = kind === 'paper' ? authorLine(preview.authors || []) : '';
  const slug = kind === 'repo' ? repoSlug(preview.url || post.link_url) : null;
  const title = slug || preview.title;

  return (
    <a
      className="postcard-link"
      href={post.link_url}
      target="_blank"
      rel="noopener noreferrer nofollow"
      onClick={track}
    >
      {preview.has_image && thumbUrl && (
        <img src={thumbUrl} alt="" loading="lazy" className="postcard-link-thumb" />
      )}
      <span className="postcard-link-body">
        <span className="postcard-link-host">
          {hostOf(preview.url || post.link_url)}
          {preview.trusted && <span className="postcard-link-trusted" title="known research source">✓</span>}
          {kind !== 'page' && (
            <span className={`postcard-link-kind postcard-link-kind--${kind}`}>{kind}</span>
          )}
          {kind === 'paper' && preview.year && (
            <span className="postcard-link-year">{preview.year}</span>
          )}
        </span>
        {title && (
          <span className={`postcard-link-title${kind === 'repo' ? ' postcard-link-title--mono' : ''}`}>
            {title}
          </span>
        )}
        {authors && <span className="postcard-link-authors">{authors}</span>}
        {preview.description && <span className="postcard-link-desc">{preview.description}</span>}
      </span>
    </a>
  );
}
