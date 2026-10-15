import { Icons, Svg } from './icons'

/**
 * Small dependency-free renderer for code-agent replies.
 * React escapes all content, including untrusted tool/model output.
 * Code fences are only promoted to blocks after the closing fence arrives.
 */
export default function MessageContent({ content, streaming = false }: { content: string; streaming?: boolean }) {
  const blocks: Array<{ kind: 'text' | 'code'; value: string; language?: string }> = []
  const fence = /(?:^|\n)```([^\n`]*)\n([\s\S]*?)\n```(?=\n|$)/g
  let cursor = 0
  for (const match of content.matchAll(fence)) {
    const position = match.index ?? 0
    if (position > cursor) blocks.push({ kind: 'text', value: content.slice(cursor, position) })
    blocks.push({ kind: 'code', value: match[2], language: match[1].trim().slice(0, 40) })
    cursor = position + match[0].length
  }
  if (cursor < content.length || blocks.length === 0) {
    blocks.push({ kind: 'text', value: content.slice(cursor) })
  }

  return (
    <div className={streaming ? 'cursor-blink' : ''}>
      {blocks.map((block, index) => block.kind === 'code' ? (
        <div key={index} className="answer-code-block">
          <div className="answer-code-header">
            <span>{block.language || '代码'}</span>
            <button type="button" onClick={() => navigator.clipboard?.writeText(block.value)}
              aria-label="复制代码" className="answer-code-copy">
              <Svg d={Icons.doc} size={13} /> 复制代码
            </button>
          </div>
          <pre><code>{block.value}</code></pre>
        </div>
      ) : (
        <div key={index} className="chat-message-text">{block.value}</div>
      ))}
    </div>
  )
}
