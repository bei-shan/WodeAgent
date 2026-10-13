import { useEffect, useState } from 'react'
import { files, type FileEntry } from './api'

/** Session-scoped, read-only file explorer. Never uses paths outside the session workspace. */
export default function FileBrowser({ sessionId }: { sessionId: string | null }) {
  const [path, setPath] = useState('.')
  const [entries, setEntries] = useState<FileEntry[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [truncated, setTruncated] = useState(false)

  useEffect(() => {
    setPath('.')
    setSelected(null)
    setContent('')
  }, [sessionId])

  useEffect(() => {
    if (!sessionId) { setEntries([]); return }
    let cancelled = false
    setLoading(true)
    setError('')
    files.tree(sessionId, path).then(tree => {
      if (cancelled) return
      setEntries(tree.entries)
      setTruncated(tree.truncated)
    }).catch(err => {
      if (!cancelled) { setEntries([]); setError(String(err.message || err)) }
    }).finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [sessionId, path])

  const view = async (entry: FileEntry) => {
    if (!sessionId) return
    if (entry.type === 'directory') {
      setSelected(null)
      setPath(entry.path)
      return
    }
    setSelected(entry.path)
    setError('')
    try {
      const result = await files.content(sessionId, entry.path)
      setContent(result.content + (result.truncated ? '\n…文件内容已截断' : ''))
    } catch (err: any) {
      setContent('')
      setError(err.message || '无法预览此文件')
    }
  }

  const parent = path === '.' ? '.' : path.split('/').slice(0, -1).join('/') || '.'
  return (
    <section className="flex-1 min-h-0 overflow-y-auto p-6" aria-label="工作区文件">
      <div className="max-w-5xl mx-auto space-y-4">
        <div className="flex justify-between items-center gap-4">
          <div>
            <h2 className="text-xl font-semibold">工作区文件</h2>
            <p className="text-sm text-gray-500">仅显示当前会话的文件</p>
          </div>
          <button className="text-sm px-3 py-2 rounded-lg border bg-white" onClick={() => setPath(p => p === '.' ? './' : p === './' ? '.' : p)} disabled={!sessionId}>
            刷新
          </button>
        </div>
        {!sessionId ? <p className="text-gray-500">请先创建或选择一个会话。</p> : (
          <div className="grid md:grid-cols-2 gap-4">
            <div className="bg-white border rounded-xl overflow-hidden">
              <div className="flex items-center gap-2 border-b p-3 text-sm font-mono">
                <button onClick={() => { setPath(parent); setSelected(null) }} disabled={path === '.'} className="text-blue-600 disabled:text-gray-300">上一级</button>
                <span className="truncate ml-2">{path === '.' || path === './' ? '/' : '/' + path}</span>
              </div>
              {loading && <p className="p-4 text-sm text-gray-500">加载中…</p>}
              {!loading && entries.length === 0 && <p className="p-4 text-sm text-gray-500">目录为空</p>}
              <ul>
                {entries.map(entry => (
                  <li key={entry.path}>
                    <button onClick={() => view(entry)}
                      className={`w-full flex items-center justify-between text-left px-4 py-3 border-b border-gray-50 hover:bg-blue-50 text-sm ${selected === entry.path ? 'bg-blue-50' : ''}`}>
                      <span className="truncate">{entry.type === 'directory' ? '📁 ' : '📄 '}{entry.name}</span>
                      <span className="text-gray-400">{entry.type === 'directory' ? '›' : '预览'}</span>
                    </button>
                  </li>
                ))}
              </ul>
              {truncated && <p className="p-3 text-xs text-amber-600">文件过多，仅显示前 200 项。</p>}
            </div>
            <div className="bg-white border rounded-xl min-h-[240px] overflow-hidden">
              <div className="flex items-center justify-between p-3 border-b gap-2">
                <span className="font-mono text-xs truncate">{selected || '文件预览'}</span>
                {selected && <a className="text-blue-600 text-xs shrink-0" download href={`/api/sessions/${encodeURIComponent(sessionId)}/files/download?path=${encodeURIComponent(selected)}`}>下载</a>}
              </div>
              {selected ? <pre className="p-4 overflow-auto whitespace-pre-wrap text-xs max-h-[65vh]">{content}</pre> :
                <p className="p-4 text-sm text-gray-400">选择一个文件以查看内容。</p>}
            </div>
          </div>
        )}
        {error && <p role="alert" className="text-red-600 text-sm">{error}</p>}
      </div>
    </section>
  )
}
