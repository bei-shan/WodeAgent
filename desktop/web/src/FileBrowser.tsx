import { useEffect, useRef, useState } from 'react'
import FileDiff from './components/FileDiff'
import { files, type FileEntry } from './api'

/** Session-scoped, read-only file explorer. Never uses paths outside the session workspace. */
export default function FileBrowser({ sessionId }: { sessionId: string | null }) {
  const [path, setPath] = useState('.')
  const [entries, setEntries] = useState<FileEntry[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [baseline, setBaseline] = useState<string | null>(null)
  const [comparison, setComparison] = useState<string | null>(null)
  const [diffMode, setDiffMode] = useState(false)
  const [gitMode, setGitMode] = useState(false)
  const [gitPatch, setGitPatch] = useState('')
  const [gitReason, setGitReason] = useState('')
  const [gitLoading, setGitLoading] = useState(false)
  const [fileTruncated, setFileTruncated] = useState(false)
  const revision = useRef(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [truncated, setTruncated] = useState(false)
  const [refresh, setRefresh] = useState(0)

  useEffect(() => {
    revision.current++
    setPath('.')
    setSelected(null)
    setContent('')
    setBaseline(null)
    setComparison(null)
    setDiffMode(false); setGitMode(false)
    setFileTruncated(false)
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
  }, [sessionId, path, refresh])

  // The API returns numbered preview lines (1\\t...); remove the display prefix
  // only for snapshot comparison. Both revisions must come from the same file.
  const normalize = (value: string) => value.split('\n')
    .map(line => line.replace(/^\d+\t/, '')).join('\n')

  const view = async (entry: FileEntry) => {
    if (!sessionId) return
    const serial = ++revision.current
    if (entry.type === 'directory') {
      setSelected(null)
      setBaseline(null)
      setComparison(null)
      setDiffMode(false); setGitMode(false)
      setPath(entry.path)
      return
    }
    setSelected(entry.path)
    setContent('')
    setBaseline(null)
    setComparison(null)
    setDiffMode(false); setGitMode(false)
    setGitPatch('')
    setGitReason('')
    setError('')
    try {
      const result = await files.content(sessionId, entry.path)
      if (serial !== revision.current) return
      setContent(result.content + (result.truncated ? '\n…文件内容已截断' : ''))
      setFileTruncated(result.truncated)
      if (!result.truncated) setBaseline(normalize(result.content))
    } catch (err: any) {
      if (serial !== revision.current) return
      setContent('')
      setError(err.message || '无法预览此文件')
    }
  }

  const compareLatest = async () => {
    if (!sessionId || !selected || baseline === null) return
    const serial = ++revision.current
    setError('')
    try {
      const latest = await files.content(sessionId, selected)
      if (serial !== revision.current) return
      if (latest.truncated) {
        setComparison(null)
        setError('文件预览已截断，不能进行完整快照对比')
        return
      }
      setComparison(normalize(latest.content))
      setContent(latest.content)
      setGitMode(false)
      setDiffMode(true)
    } catch (err: any) {
      if (serial === revision.current) setError(err.message || '获取最新快照失败')
    }
  }

  const compareGit = async () => {
    if (!sessionId || !selected) return
    const serial = ++revision.current
    setGitLoading(true)
    setError('')
    try {
      const result = await files.gitDiff(sessionId, selected)
      if (serial !== revision.current) return
      setGitPatch(result.patch || '')
      setGitReason(result.reason || '')
      setGitMode(true)
      setDiffMode(false)
    } catch (err: any) {
      if (serial === revision.current) setError(err.message || 'Git Diff 获取失败')
    } finally {
      if (serial === revision.current) setGitLoading(false)
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
          <button className="text-sm px-3 py-2 rounded-lg border bg-white" onClick={() => setRefresh(n => n + 1)} disabled={!sessionId}>
            刷新
          </button>
        </div>
        {!sessionId ? <p className="text-gray-500">请先创建或选择一个会话。</p> : (
          <div className="grid md:grid-cols-2 gap-4">
            <div className="bg-white border rounded-xl overflow-hidden">
              <div className="flex items-center gap-2 border-b p-3 text-sm font-mono">
                <button onClick={() => { setPath(parent); setSelected(null); setBaseline(null); setComparison(null); setDiffMode(false); setGitMode(false); revision.current++ }} disabled={path === '.'} className="text-blue-600 disabled:text-gray-300">上一级</button>
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
                <div className="flex items-center gap-2 shrink-0">
                  {selected && <button type="button" disabled={gitLoading}
                    onClick={() => void compareGit()} className="text-xs text-blue-600 disabled:opacity-50">Git Diff</button>}
                  {selected && baseline !== null && (
                    <button type="button" onClick={() => void compareLatest()} disabled={fileTruncated}
                      className="text-xs text-blue-600 disabled:opacity-40">对比最新</button>
                  )}
                  {selected && diffMode && (
                    <button type="button" onClick={() => setDiffMode(false); setGitMode(false)} className="text-xs text-gray-600">原文</button>
                  )}
                  {selected && comparison !== null && (
                    <button type="button" onClick={() => { setBaseline(comparison); setDiffMode(false); setGitMode(false); setComparison(null) }}
                      className="text-xs text-gray-600" title="更新此浏览器会话中的文件对比基线">更新基线</button>
                  )}
                  {selected && <a className="text-blue-600 text-xs" download
                    href={`/api/sessions/${encodeURIComponent(sessionId)}/files/download?path=${encodeURIComponent(selected)}`}>下载</a>}
                </div>
              </div>
              {selected ? (gitMode
                ? gitPatch ? <pre aria-label="Git Diff 内容" className="p-4 overflow-auto whitespace-pre-wrap text-xs max-h-[65vh]">{gitPatch}</pre>
                  : <p className="p-4 text-sm text-gray-500">{gitReason || '相对 HEAD 没有变化'}</p>
                : diffMode && baseline !== null && comparison !== null
                  ? <FileDiff before={baseline} after={comparison} />
                  : <pre className="p-4 overflow-auto whitespace-pre-wrap text-xs max-h-[65vh]">{content}</pre>)
                : <p className="p-4 text-sm text-gray-400">选择一个文件以查看内容。</p>}
              {selected && <p className="px-3 py-2 border-t text-[11px] text-gray-400">
                「对比最新」是本地快照比较；「Git Diff」仅针对会话内 Git 仓库中的已跟踪文件，对比 HEAD。
              </p>}
            </div>
          </div>
        )}
        {error && <p role="alert" className="text-red-600 text-sm">{error}</p>}
      </div>
    </section>
  )
}
