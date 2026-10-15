import type { ReactNode } from 'react'
import { Icons, Svg } from './icons'

// Model-authored markup stays plain React text. We never use raw HTML or javascript: links.
const TOKENS = /(\[([^\]\n]{1,150})\]\(([^)\s]{1,2048})\)|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|`([^`\n]+)`)/g
const WORDS = /^(?:async|await|class|const|def|else|except|export|false|finally|for|from|function|if|import|in|interface|let|new|None|null|return|self|static|throw|true|try|type|var|void|while|yield)$/
const CODE = /(\/\/[^\n]*|#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b[A-Za-z_]\w*\b|\b\d+(?:\.\d+)?\b)/g

function inline(value: string): ReactNode[] {
  const out: ReactNode[] = []
  let offset = 0
  for (const m of value.matchAll(TOKENS)) {
    const at = m.index ?? 0
    if (at > offset) out.push(value.slice(offset, at))
    if (m[2] !== undefined) {
      const url = m[3]
      const safe = /^(https?:\/\/|mailto:|#[\w-]+$)/i.test(url)
      out.push(safe ? <a key={at} className="markdown-link" href={url}
        target={url.startsWith('#') ? undefined : '_blank'}
        rel={url.startsWith('#') ? undefined : 'noopener noreferrer'}>{m[2]}</a> : m[0])
    } else if (m[4] !== undefined) out.push(<strong key={at}>{m[4]}</strong>)
    else if (m[5] !== undefined) out.push(<em key={at}>{m[5]}</em>)
    else if (m[6] !== undefined) out.push(<code key={at} className="markdown-inline-code">{m[6]}</code>)
    offset = at + m[0].length
  }
  if (offset < value.length) out.push(value.slice(offset))
  return out
}

function highlight(code: string): ReactNode {
  if (code.length > 100000) return code
  const result: ReactNode[] = []
  let position = 0
  for (const m of code.matchAll(CODE)) {
    const i = m.index ?? 0
    if (i > position) result.push(code.slice(position, i))
    const word = m[0]
    const kind = word.startsWith('//') || word.startsWith('#') ? 'comment'
      : /^["']/.test(word) ? 'string'
        : /^\d/.test(word) ? 'number' : WORDS.test(word) ? 'keyword' : ''
    result.push(kind ? <span key={i} className={'syntax-' + kind}>{word}</span> : word)
    position = i + word.length
  }
  if (position < code.length) result.push(code.slice(position))
  return result
}

function CodeBlock({ code, language }: { code: string; language: string }) {
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

function Block({ lines }: { lines: string[] }) {
  const first = lines[0]
  const heading = first.match(/^(#{1,6})\s+(.*)/)
  if (heading) {
    const level = heading[1].length
    const Tag = ('h' + level) as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'
    return <Tag className="markdown-heading">{inline(heading[2])}</Tag>
  }
  if (lines.every(x => /^\s*[-*+] /.test(x))) {
    return <ul className="markdown-list">{lines.map((s, i) =>
      <li key={i}>{inline(s.replace(/^\s*[-*+] /, ''))}</li>)}</ul>
  }
  if (lines.every(x => /^\s*\d+[.)] /.test(x))) {
    return <ol className="markdown-list list-decimal">{lines.map((s, i) =>
      <li key={i}>{inline(s.replace(/^\s*\d+[.)] /, ''))}</li>)}</ol>
  }
  if (lines.every(x => /^>\s?/.test(x))) {
    return <blockquote className="markdown-quote">{inline(lines.map(x => x.replace(/^>\s?/, '')).join('\n'))}</blockquote>
  }
  if (/^[-*_]{3,}\s*$/.test(first)) return <hr className="markdown-rule" />
  return <p className="markdown-paragraph">{inline(lines.join('\n'))}</p>
}

export default function MessageContent({ content, streaming = false }: { content: string; streaming?: boolean }) {
  const lines = content.replace(/\r\n?/g, '\n').split('\n')
  const output: ReactNode[] = []
  let i = 0
  while (i < lines.length) {
    if (!lines[i].trim()) { i++; continue }
    const opening = lines[i].match(/^\s{0,3}(`{3,}|~{3,})([\w.+-]*)\s*$/)
    if (opening) {
      const symbol = opening[1][0]
      const count = opening[1].length
      const closing = new RegExp('^\\s{0,3}' + symbol + '{' + count + ',}\\s*$')
      let end = i + 1
      while (end < lines.length && !closing.test(lines[end])) end++
      if (end < lines.length) {
        output.push(<CodeBlock key={i} language={opening[2]} code={lines.slice(i + 1, end).join('\n')} />)
        i = end + 1
        continue
      }
      // Unfinished streaming fences are rendered as literal paragraphs.
    }
    const first = lines[i]
    const category = /^\s*[-*+] /.test(first) ? 'list'
      : /^\s*\d+[.)] /.test(first) ? 'number'
      : /^>\s?/.test(first) ? 'quote' : 'paragraph'
    let end = i + 1
    if (!/^#{1,6} /.test(first) && !/^[-*_]{3,}\s*$/.test(first)) {
      while (end < lines.length && lines[end].trim() && !/^\s{0,3}(`{3,}|~{3,})/.test(lines[end])) {
        const s = lines[end]
        const next = /^\s*[-*+] /.test(s) ? 'list' : /^\s*\d+[.)] /.test(s) ? 'number'
          : /^>\s?/.test(s) ? 'quote' : 'paragraph'
        if (next !== category || /^#{1,6} /.test(s)) break
        end++
      }
    }
    output.push(<Block key={i} lines={lines.slice(i, end)} />)
    i = end
  }
  return <div className={streaming ? 'markdown-answer cursor-blink' : 'markdown-answer'}>{output}</div>
}
