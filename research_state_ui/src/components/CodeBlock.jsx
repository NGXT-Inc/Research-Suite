import { Highlight, themes } from 'prism-react-renderer';

/**
 * Syntax-highlighted code block.
 *
 * Uses prism-react-renderer's bundled Prism, which supports python / json /
 * yaml / javascript / typescript / jsx / tsx / bash / css / markup / sql etc.
 * out of the box. Theme is the built-in github light to match the paper-cream
 * palette of the rest of the app.
 */
export default function CodeBlock({ code, language, showLineNumbers = true }) {
  const text = code ?? '';
  return (
    <Highlight code={text} language={language || 'plaintext'} theme={themes.github}>
      {({ className, style, tokens, getLineProps, getTokenProps }) => (
        <pre className={`code-block ${className || ''}`} style={style}>
          {tokens.map((line, i) => {
            const lineProps = getLineProps({ line });
            return (
              <div key={i} {...lineProps} className={`code-line ${lineProps.className || ''}`}>
                {showLineNumbers && (
                  <span className="code-line-num" aria-hidden="true">{i + 1}</span>
                )}
                <span className="code-line-body">
                  {line.map((token, j) => <span key={j} {...getTokenProps({ token })} />)}
                </span>
              </div>
            );
          })}
        </pre>
      )}
    </Highlight>
  );
}
