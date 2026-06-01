import CodeBlock from './CodeBlock';
import MarkdownView from './MarkdownView';

/**
 * Dispatch a text file to the right renderer based on its extension.
 *
 * - `.md` / `.markdown` → MarkdownView (react-markdown + remark-gfm + Prism
 *   for fenced code blocks)
 * - Recognized code extensions → CodeBlock (Prism syntax highlighting with
 *   line numbers)
 * - Everything else → plain monospace <pre>
 *
 * Files without a recognized extension fall through to plain rendering rather
 * than guessing — researchers see weird files often, false-positive
 * "highlighting" is worse than no highlighting.
 */
const EXT_TO_LANG = {
  py: 'python',
  ipynb: 'json',
  json: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'toml',
  js: 'javascript',
  jsx: 'jsx',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'typescript',
  tsx: 'tsx',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  css: 'css',
  scss: 'scss',
  html: 'markup',
  xml: 'markup',
  sql: 'sql',
  rb: 'ruby',
  go: 'go',
  rs: 'rust',
  java: 'java',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  cc: 'cpp',
  hpp: 'cpp',
};

function extOf(path) {
  if (!path) return '';
  const name = path.split('/').pop() || '';
  const idx = name.lastIndexOf('.');
  if (idx < 0) return '';
  return name.slice(idx + 1).toLowerCase();
}

export default function FileRenderer({ text, path }) {
  const ext = extOf(path);
  if (ext === 'md' || ext === 'markdown' || ext === 'mdx') {
    return <MarkdownView text={text} />;
  }
  const lang = EXT_TO_LANG[ext];
  if (lang) {
    return <CodeBlock code={text || ''} language={lang} />;
  }
  return <pre className="content-preview">{text}</pre>;
}
