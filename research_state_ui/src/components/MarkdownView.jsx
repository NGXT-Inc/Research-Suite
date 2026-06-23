import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import CodeBlock from './CodeBlock';

/**
 * Renders a single markdown image. Report figures resolve to blob-store bytes
 * that may 404 (or, in cloud mode, be unavailable) when the figure was never
 * submitted / pinned. Instead of a broken <img>, fall back to an inline
 * placeholder explaining the figure isn't in the submitted set.
 */
function FigureImg({ src, alt, title }) {
  const [failed, setFailed] = useState(false);
  const filename = ((src || alt || '').split('/').pop()) || 'figure';
  if (failed) {
    return (
      <span className="figure-missing">
        <span>Figure not available</span>
        <span className="figure-missing-name">{filename}</span>
      </span>
    );
  }
  return <img src={src} alt={alt || ''} title={title} loading="lazy" onError={() => setFailed(true)} />;
}

/**
 * Markdown renderer for .md / .markdown files.
 *
 * react-markdown v10 dropped the `inline` prop on the `code` component, so
 * fenced code blocks must be handled at the `pre` level (which is the actual
 * block element). Inline `\`foo\`` reaches the `code` component directly
 * (no `pre` ancestor), so it stays simple.
 *
 * - remark-gfm adds tables, strikethrough, task lists, autolinks.
 * - Fenced code with a language hint renders through CodeBlock (Prism).
 * - External links open in a new tab; in-document anchors stay in place.
 * - resolveImageSrc (optional): maps a relative image src (e.g. a report's
 *   `figures/loss.png`) to a fetchable URL. Absolute/data/http srcs pass
 *   through untouched.
 */
export default function MarkdownView({ text, resolveImageSrc }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          img({ src, alt, title }) {
            const passthrough = !src
              || /^(https?:|data:|blob:)/i.test(src)
              || src.startsWith('/');
            const resolved = passthrough || !resolveImageSrc ? src : resolveImageSrc(src);
            return <FigureImg src={resolved} alt={alt} title={title} />;
          },
          // Fenced code blocks land here. The child is the `code` element
          // with the language hint as className.
          pre({ children }) {
            const child = Array.isArray(children) ? children[0] : children;
            const className = child?.props?.className || '';
            const match = /language-(\w+)/.exec(className);
            const code = String(child?.props?.children ?? '').replace(/\n$/, '');
            if (match) {
              return <CodeBlock code={code} language={match[1]} showLineNumbers={false} />;
            }
            return <pre className="md-code-plain"><code>{code}</code></pre>;
          },
          // Inline code (no `pre` ancestor) lands here.
          code({ className, children, ...props }) {
            return <code className={`md-inline-code ${className || ''}`} {...props}>{children}</code>;
          },
          a({ children, ...props }) {
            const external = props.href && /^https?:\/\//i.test(props.href);
            return (
              <a
                {...props}
                target={external ? '_blank' : undefined}
                rel={external ? 'noreferrer noopener' : undefined}
              >
                {children}
              </a>
            );
          },
          table({ children, ...props }) {
            return (
              <div className="md-table-wrap">
                <table {...props}>{children}</table>
              </div>
            );
          },
        }}
      >
        {text || ''}
      </ReactMarkdown>
    </div>
  );
}
