import { Highlight, themes } from 'prism-react-renderer';
import { useTheme } from '../store/useTheme';

/**
 * Syntax-highlighted code block.
 *
 * Uses prism-react-renderer's bundled Prism, which supports python / json /
 * yaml / javascript / typescript / jsx / tsx / bash / css / markup / sql etc.
 * out of the box.
 *
 * Theme-aware: GitHub light on the paper-cream palette, vsDark on the dark
 * palette. The .code-block CSS pins the surface to var(--bg-elev) in both
 * modes, so only the Prism theme's token + default-text colors apply — and
 * the light theme's near-black default text is invisible on a dark surface,
 * which is exactly why dark mode needs its own theme.
 */
export default function CodeBlock({ code, language, showLineNumbers = true }) {
  const { theme } = useTheme();
  const prismTheme = theme === 'dark' ? themes.vsDark : themes.github;
  const text = code ?? '';
  return (
    <Highlight code={text} language={language || 'plaintext'} theme={prismTheme}>
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
