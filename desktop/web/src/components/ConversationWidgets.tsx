import { useState } from 'react'
import * as api from '../api'
import type { SessionInfo } from '../api'
import type { ToolEntry, PermRequest, AskRequest } from './types'
import { Icons, Svg } from './icons'

export function PermissionModal({ req, sid, onDone }: { req: PermRequest; sid: string; onDone: () => void }) {
  const decide = async (d: string) => { await api.sessions.resolvePerm(sid, req.requestId, d); onDone() }
  return (
    <div className="fixed inset-0 bg-black/20 flex items-center justify-center z-50 animate-fade-in" onClick={() => decide('denied')}>
      <div className="bg-white rounded-2xl p-6 max-w-md w-full mx-4 shadow-xl border border-gray-100" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <div className="w-9 h-9 rounded-full bg-amber-50 flex items-center justify-center text-amber-600"><Svg d={Icons.lock} /></div>
          <h2 className="text-lg font-semibold text-gray-800">权限请求</h2>
        </div>
        <div className="space-y-2 text-sm text-gray-700 mb-5 bg-gray-50 rounded-xl p-3">
          <p className="flex justify-between"><span className="text-gray-500">工具</span> <code className="text-blue-600 font-medium">{req.tool}</code></p>
          <p className="flex justify-between"><span className="text-gray-500">路径</span> <code className="text-amber-700 text-xs max-w-[200px] truncate">{req.path}</code></p>
          <p className="flex justify-between"><span className="text-gray-500">操作</span> {req.action}</p>
        </div>
        <div className="flex gap-3 justify-end">
          <button onClick={() => decide('denied')} className="px-5 py-2.5 rounded-xl border border-gray-200 text-gray-500 hover:bg-gray-50 transition-colors text-sm font-medium">拒绝</button>
          <button onClick={() => decide('granted')} className="px-5 py-2.5 rounded-xl bg-blue-500 text-white hover:bg-blue-600 transition-colors text-sm font-medium">允许一次</button>
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// AskUser Modal
// ═══════════════════════════════════════════════════════════════════════

export function AskUserModal({ req, sid, onDone }: { req: AskRequest; sid: string; onDone: () => void }) {
  const [answer, setAnswer] = useState('')
  const submit = async () => { await api.sessions.answerAsk(sid, req.requestId, answer || '无回答'); onDone() }
  return (
    <div className="fixed inset-0 bg-black/20 flex items-center justify-center z-50 animate-fade-in">
      <div className="bg-white rounded-2xl p-6 max-w-md w-full mx-4 shadow-xl border border-gray-100">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-9 h-9 rounded-full bg-blue-50 flex items-center justify-center text-blue-600"><Svg d={Icons.ask} /></div>
          <h2 className="text-lg font-semibold text-gray-800">Agent 提问</h2>
        </div>
        <pre className="text-sm text-gray-700 mb-4 whitespace-pre-wrap bg-gray-50 p-3 rounded-xl">{req.prompt}</pre>
        <textarea value={answer} onChange={e => setAnswer(e.target.value)} rows={3}
          className="w-full bg-gray-50 border border-gray-200 rounded-xl p-3 text-sm text-gray-800 resize-none input-focus-ring mb-4"
          placeholder="输入你的回答..." onKeyDown={e => { if (e.key === 'Enter' && e.ctrlKey) submit() }} />
        <div className="flex gap-3 justify-end">
          <button onClick={submit} className="px-5 py-2.5 rounded-xl bg-blue-500 text-white hover:bg-blue-600 transition-colors text-sm font-medium">回答</button>
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// Tool card
// ═══════════════════════════════════════════════════════════════════════

export function ToolCard({ tool, sid }: { tool: ToolEntry; sid: string | null }) {
  const [expanded, setExpanded] = useState(false)
  const colors = tool.status === 'running' ? 'border-amber-200 bg-amber-50/50' : tool.status === 'error' ? 'border-red-200 bg-red-50/50' : 'border-emerald-200 bg-emerald-50/50'
  const dot = tool.status === 'running' ? 'bg-amber-400 animate-pulse' : tool.status === 'error' ? 'bg-red-400' : 'bg-emerald-400'
  return (
    <div className={`ml-10 border rounded-xl px-3 py-2 text-xs ${colors} animate-slide-up`}>
      <button type="button" className="w-full flex items-center gap-2 text-left select-none"
        onClick={() => setExpanded(!expanded)} aria-expanded={expanded}
        aria-label={`${tool.name} 工具详情`}>
        <span className={`w-2 h-2 rounded-full ${dot}`} />
        <span className="font-medium text-gray-700">{tool.name}</span>
        <span className="text-gray-400 ml-auto text-[10px]">{expanded ? '收起' : '展开'}</span>
      </button>
      {expanded && (
        <div className="mt-2 space-y-1">
          <div className="text-gray-400 text-[10px] uppercase tracking-wide">Input</div>
          <pre className="bg-white/70 p-2 rounded-lg text-[11px] text-gray-700 overflow-x-auto max-h-24">{JSON.stringify(tool.input, null, 2)}</pre>
          {tool.output && <>
            <div className="text-gray-400 text-[10px] uppercase tracking-wide mt-1">Output</div>
            <pre className="bg-white/70 p-2 rounded-lg text-[11px] text-gray-700 overflow-x-auto max-h-40 whitespace-pre-wrap">{tool.output.length > 2000 ? tool.output.slice(0, 2000) + '...' : tool.output}</pre>
          </>}
        </div>
      )}
      {tool.filePath && tool.status === 'success' && sid && (
        <a href={`/api/sessions/${sid}/files/download?path=${encodeURIComponent(tool.filePath)}`}
          download className="ml-2 text-[11px] text-blue-500 hover:text-blue-600 font-medium inline-flex items-center gap-1 mt-1">
          <Svg d={Icons.download} size={12} /> 下载 {tool.filePath.split('/').pop()?.split('\\').pop()}
        </a>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// Sidebar navigation item
// ═══════════════════════════════════════════════════════════════════════

export function NavItem({ icon, label, active, onClick }: { icon: string; label: string; active?: boolean; onClick?: () => void }) {
  return (
    <button onClick={onClick}
      className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm transition-colors ${
        active ? 'bg-blue-50 text-blue-600 font-medium' : 'text-gray-600 hover:bg-gray-50 hover:text-gray-800'
      }`}>
      <Svg d={icon} size={18} />
      <span>{label}</span>
    </button>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// Session list item with dropdown menu
// ═══════════════════════════════════════════════════════════════════════

export function SessionItem({ session, active, onSelect, onDelete, onRename, onTogglePin }: {
  session: SessionInfo; active: boolean; onSelect: () => void;
  onDelete: () => void; onRename: (title: string) => void; onTogglePin: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [name, setName] = useState(session.title)

  const finishRename = () => {
    const title = name.trim()
    if (title && title !== session.title) onRename(title)
    setRenaming(false)
  }

  return (
    <div className={`session-row group flex items-center justify-between ${active ? 'active' : ''}`}>
      {renaming ? (
        <input aria-label="重命名会话" value={name} onChange={event => setName(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter') { event.preventDefault(); finishRename() }
            if (event.key === 'Escape') { setName(session.title); setRenaming(false) }
          }}
          onBlur={finishRename} autoFocus
          className="mx-2 my-1 min-w-0 flex-1 rounded-md border border-gray-300 bg-white px-2 py-1 text-[13px] outline-hidden" />
      ) : (
        <button type="button" onClick={onSelect} aria-current={active ? 'page' : undefined}
          className="session-row-main flex items-center gap-2" title={session.title || session.id}>
          {session.pinned && <span className="shrink-0 text-xs" aria-label="置顶">📌</span>}
          <span className="truncate">{session.title || session.id.slice(0, 8)}</span>
          {session.busy && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400 animate-pulse" aria-label="执行中" />}
        </button>
      )}
      <div className="relative shrink-0 pr-1">
        <button type="button" onClick={() => setMenuOpen(value => !value)}
          aria-label={`管理会话 ${session.title || session.id}`} aria-expanded={menuOpen}
          className="flex h-7 w-7 items-center justify-center rounded-md text-gray-400 hover:bg-gray-200 hover:text-gray-700 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus:opacity-100">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="8" cy="3" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="8" cy="13" r="1.5"/></svg>
        </button>
        {menuOpen && (
          <div className="absolute right-0 top-full z-20 mt-1 w-32 rounded-xl border border-gray-200 bg-white py-1 shadow-lg">
            <button type="button" onClick={() => { setName(session.title); setRenaming(true); setMenuOpen(false) }}
              className="w-full px-3 py-2 text-left text-xs text-gray-600 hover:bg-gray-50">重命名</button>
            <button type="button" onClick={() => { onTogglePin(); setMenuOpen(false) }}
              className="w-full px-3 py-2 text-left text-xs text-gray-600 hover:bg-gray-50">
              {session.pinned ? '取消置顶' : '置顶'}
            </button>
            <button type="button" onClick={() => { onDelete(); setMenuOpen(false) }}
              className="w-full px-3 py-2 text-left text-xs text-red-600 hover:bg-red-50">删除</button>
          </div>
        )}
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// Skills management panel
// ═══════════════════════════════════════════════════════════════════════

