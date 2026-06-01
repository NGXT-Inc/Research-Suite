import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import CodeBlock from './CodeBlock';

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
 */
export default function MarkdownView({ text }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
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
