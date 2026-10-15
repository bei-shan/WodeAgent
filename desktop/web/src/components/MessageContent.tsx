import { useMemo } from 'react'
import type { ReactNode } from 'react'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { Icons, Svg } from './icons'

const KEYWORDS = /^(?:async|await|break|case|catch|class|const|continue|def|do|else|enum|except|export|false|finally|for|from|function|if|import|in|interface|let|new|null|None|private|public|raise|return|self|static|switch|throw|true|try|type|var|void|while|with|yield)$/
const CODE = /(\/\/[^\n]*|#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b[A-Za-z_]\w*\b|\b\d+(?:\.\d+)?\b)/g
const MAX_MARKDOWN = 200_000

function highlight(code: string): ReactNode {
  if (code.length > 100_000) return code
  const fragments: ReactNode[] = []
  let pos = 0
  for (const match of code.matchAll(CODE)) {
    const index = match.index ?? 0
    if (index > pos) fragments.push(code.slice(pos, index))
    const value = match[0]
    const kind = value.startsWith('//') || value.startsWith('#') ? 'comment'
      : /^["']/.test(value) ? 'string'
        : /^\d/.test(value) ? 'number'
          : KEYWORDS.test(value) ? 'keyword' : ''
    fragments.push(kind ? <span key={index} className={'syntax-' + kind}>{value}</span> : value)
    pos = index + value.length
  }
  if (pos < code.length) fragments.push(code.slice(pos))
  return fragments
}

function CodeBlock({ language, code }: { language: string; code: string }) {
  return <div className="answer-code-block">
    <div className="answer-code-header">
      <span>{language || '代码'}</span>
      <button type="button" onClick={() => void navigator.clipboard?.writeText(code)}
        aria-label="复制代码" className="answer-code-copy">
        <Svg d={Icons.doc} size={13} /> 复制代码
      </button>
    </div>
    <pre><code>{highlight(code)}</code></pre>
  </div>
}

/**
 * Marked supplies CommonMark/GFM parsing. DOMPurify sanitizes every fragment:
 * never trust the model's embedded HTML, event attributes or unsafe URLs.
 * External images and embeds are forbidden to prevent remote tracking.
 */
function parse(content: string): Array<{ kind: 'code' | 'html'; value: string; language: string }> {
  const limited = content.slice(0, MAX_MARKDOWN)
  const tokens = marked.lexer(limited, { gfm: true, breaks: false })
  return tokens.map(token => {
    if (token.type === 'code') return {
      kind: 'code' as const, value: token.text, language: (token.lang || '').slice(0, 32),
    }
    const rendered = marked.parser([token], { gfm: true, breaks: false, async: false }) as string
    const safe = DOMPurify.sanitize(rendered, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ['img', 'iframe', 'svg', 'math', 'video', 'audio', 'object', 'embed', 'form'],
      FORBID_ATTR: ['style', 'src', 'srcset', 'target', 'ping'],
    })
    return { kind: 'html' as const, value: safe, language: '' }
  })
}

export default function MessageContent({ content, streaming = false }: {
  content: string; streaming?: boolean
}) {
  const blocks = useMemo(() => parse(content), [content])
  return <div className={streaming ? 'markdown-answer cursor-blink' : 'markdown-answer'}>
    {blocks.map((block, index) => block.kind === 'code'
      ? <CodeBlock key={index} code={block.value} language={block.language} />
      : <div key={index} className="markdown-html" dangerouslySetInnerHTML={{ __html: block.value }} />
    )}
    {content.length > MAX_MARKDOWN && <p className="text-xs text-amber-700">内容超过渲染上限，部分文本已省略。</p>}
  </div>
}
