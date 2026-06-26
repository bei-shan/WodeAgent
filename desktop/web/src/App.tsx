import { useState, useEffect, useRef, useCallback, Fragment } from 'react'
import * as api from './api'
import type { SessionInfo, AgentEvent, FileEntry, SkillInfo, McpServerCfg } from './api'

// ═══════════════════════════════════════════════════════════════════════
// Types
// ═══════════════════════════════════════════════════════════════════════

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  step?: number
  streaming?: boolean
}

interface ToolEntry {
  id: string
  name: string
  input: Record<string, any>
  status: 'running' | 'success' | 'error'
  output?: string
  step: number
}

interface PermRequest {
  requestId: string
  tool: string
  path: string
  action: string
}

interface AskRequest {
  requestId: string
  prompt: string
}

type SidebarTab = 'sessions' | 'files' | 'skills' | 'mcp'

// ═══════════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════════

const uid = () => Math.random().toString(36).slice(2, 10)

function toolIcon(name: string): string {
  const map: Record<string, string> = {
    Read: '📖', Write: '✍️', Edit: '✏️', MultiEdit: '📝',
    LS: '📁', Glob: '🔍', Grep: '🔎', Bash: '💻',
    Skill: '🎯', TodoWrite: '📋', AskUser: '❓',
  }
  for (const [k, v] of Object.entries(map)) {
    if (name.includes(k)) return v
  }
  return '⚙️'
}

// ═══════════════════════════════════════════════════════════════════════
// Modal: Permission
// ═══════════════════════════════════════════════════════════════════════

function PermissionModal({ req, sid, onDone }: { req: PermRequest; sid: string; onDone: () => void }) {
  const decide = async (d: string) => {
    await api.sessions.resolvePerm(sid, req.requestId, d)
    onDone()
  }
  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => decide('denied')}>
      <div className="bg-codex-surface border border-codex-border rounded-lg p-6 max-w-md w-full mx-4 shadow-2xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-2 text-codex-yellow mb-3">
          <span className="text-xl">🔒</span>
          <h2 className="text-lg font-semibold">Permission Required</h2>
        </div>
        <div className="space-y-2 text-sm text-codex-text mb-5">
          <p><span className="text-codex-dim">Tool:</span> <code className="text-codex-accent">{req.tool}</code></p>
          <p><span className="text-codex-dim">Path:</span> <code className="text-codex-yellow break-all">{req.path}</code></p>
          <p><span className="text-codex-dim">Action:</span> {req.action}</p>
        </div>
        <div className="flex gap-3 justify-end">
          <button onClick={() => decide('denied')} className="px-4 py-2 rounded border border-codex-border text-codex-dim hover:bg-codex-border/30 transition-colors text-sm">
            Deny
          </button>
          <button onClick={() => decide('granted')} className="px-4 py-2 rounded bg-codex-green text-white hover:bg-green-600 transition-colors text-sm font-medium">
            Allow Once
          </button>
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// Modal: AskUser
// ═══════════════════════════════════════════════════════════════════════

function AskUserModal({ req, sid, onDone }: { req: AskRequest; sid: string; onDone: () => void }) {
  const [answer, setAnswer] = useState('')
  const submit = async () => {
    await api.sessions.answerAsk(sid, req.requestId, answer || '(no answer)')
    onDone()
  }
  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="bg-codex-surface border border-codex-border rounded-lg p-6 max-w-md w-full mx-4 shadow-2xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-2 text-codex-purple mb-3">
          <span className="text-xl">❓</span>
          <h2 className="text-lg font-semibold">Agent asks</h2>
        </div>
        <pre className="text-sm text-codex-text mb-4 whitespace-pre-wrap bg-codex-bg p-3 rounded border border-codex-border">{req.prompt}</pre>
        <textarea value={answer} onChange={e => setAnswer(e.target.value)} rows={3}
          className="w-full bg-codex-bg border border-codex-border rounded p-2 text-codex-text text-sm mb-4 resize-none focus:border-codex-accent focus:outline-none"
          placeholder="Type your answer..."
          onKeyDown={e => { if (e.key === 'Enter' && e.ctrlKey) submit() }}
        />
        <div className="flex gap-3 justify-end">
          <button onClick={submit} className="px-4 py-2 rounded bg-codex-accent text-white hover:bg-blue-600 transition-colors text-sm font-medium">
            Answer (Ctrl+Enter)
          </button>
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// Chat message bubble
// ═══════════════════════════════════════════════════════════════════════

function ChatMessage({ msg }: { msg: Message }) {
  return (
    <div className={`flex gap-3 animate-slide-in ${msg.role === 'user' ? 'justify-end' : ''}`}>
      {msg.role === 'assistant' && (
        <div className="w-7 h-7 rounded bg-codex-purple/20 flex items-center justify-center text-xs shrink-0 mt-1">🤖</div>
      )}
      <div className={`max-w-[80%] rounded-lg px-4 py-2.5 ${
        msg.role === 'user'
          ? 'bg-codex-accent/15 border border-codex-accent/30 text-codex-text'
          : 'bg-codex-surface border border-codex-border text-codex-text'
      }`}>
        <div className={`text-sm whitespace-pre-wrap break-words leading-relaxed ${msg.streaming ? 'cursor-blink' : ''}`}>
          {msg.content || (msg.streaming ? '' : '(empty)')}
        </div>
        {msg.step && <div className="text-[10px] text-codex-dim mt-1">Step {msg.step}</div>}
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// Tool call inline card
// ═══════════════════════════════════════════════════════════════════════

function ToolCard({ tool }: { tool: ToolEntry }) {
  const [expanded, setExpanded] = useState(false)
  const statusColor = tool.status === 'running' ? 'text-codex-yellow' : tool.status === 'error' ? 'text-codex-red' : 'text-codex-green'
  const statusBg = tool.status === 'running' ? 'bg-codex-yellow/10 border-codex-yellow/30' : tool.status === 'error' ? 'bg-codex-red/10 border-codex-red/30' : 'bg-codex-green/10 border-codex-green/30'

  return (
    <div className={`ml-10 border rounded-lg px-3 py-2 text-xs animate-slide-in ${statusBg}`}>
      <div className="flex items-center gap-2 cursor-pointer select-none" onClick={() => setExpanded(!expanded)}>
        <span>{toolIcon(tool.name)}</span>
        <span className="font-medium text-codex-text">{tool.name}</span>
        {tool.status === 'running' && <span className={`${statusColor} animate-pulse`}>●</span>}
        {tool.status === 'error' && <span className={statusColor}>✗</span>}
        {tool.status === 'success' && <span className={statusColor}>✓</span>}
        <span className="text-codex-dim ml-auto">{expanded ? '▾' : '▸'}</span>
      </div>
      {expanded && (
        <div className="mt-2 space-y-1">
          <div className="text-codex-dim">Input:</div>
          <pre className="bg-codex-bg p-2 rounded text-[11px] text-codex-text overflow-x-auto max-h-24">
            {JSON.stringify(tool.input, null, 2)}
          </pre>
          {tool.output && (
            <>
              <div className="text-codex-dim mt-1">Output:</div>
              <pre className="bg-codex-bg p-2 rounded text-[11px] text-codex-text overflow-x-auto max-h-40 whitespace-pre-wrap">
                {tool.output.length > 2000 ? tool.output.slice(0, 2000) + '...(truncated)' : tool.output}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// Sidebar panels
// ═══════════════════════════════════════════════════════════════════════

function SessionsPanel({ sessions, activeId, onSelect, onCreate, onDelete }: {
  sessions: SessionInfo[]
  activeId: string | null
  onSelect: (id: string) => void
  onCreate: () => void
  onDelete: (id: string) => void
}) {
  return (
    <div className="p-3">
      <button onClick={onCreate} className="w-full py-2 rounded bg-codex-accent/10 border border-codex-accent/30 text-codex-accent text-sm font-medium hover:bg-codex-accent/20 transition-colors mb-3">
        + New Session
      </button>
      <div className="space-y-1">
        {sessions.map(s => (
          <div key={s.id}
            onClick={() => onSelect(s.id)}
            className={`flex items-center justify-between px-3 py-2 rounded text-sm cursor-pointer transition-colors ${
              s.id === activeId ? 'bg-codex-accent/15 text-codex-accent border border-codex-accent/30' : 'hover:bg-codex-surface border border-transparent text-codex-text'
            }`}
          >
            <div className="flex items-center gap-2 min-w-0">
              <span className={`w-2 h-2 rounded-full shrink-0 ${s.busy ? 'bg-codex-yellow animate-pulse' : 'bg-codex-green'}`} />
              <span className="truncate">{s.title || s.id.slice(0, 8)}</span>
            </div>
            <button onClick={e => { e.stopPropagation(); onDelete(s.id) }}
              className="text-codex-dim hover:text-codex-red shrink-0 ml-1 opacity-0 group-hover:opacity-100 transition-opacity text-lg leading-none">
              ×
            </button>
          </div>
        ))}
        {sessions.length === 0 && <div className="text-codex-dim text-xs text-center py-4">No sessions yet</div>}
      </div>
    </div>
  )
}

function FilesPanel({ sid }: { sid: string | null }) {
  const [entries, setEntries] = useState<FileEntry[]>([])
  const [cwd, setCwd] = useState('.')
  const [loading, setLoading] = useState(false)

  const load = useCallback(async (path: string) => {
    if (!sid) return
    setLoading(true)
    try {
      const tree = await api.files.tree(sid, path)
      setEntries(tree.entries)
      setCwd(tree.path)
    } catch { /* offline */ }
    finally { setLoading(false) }
  }, [sid])

  useEffect(() => { load('.') }, [load])

  if (!sid) return <div className="p-3 text-codex-dim text-xs">Select a session</div>

  return (
    <div className="p-3">
      <div className="flex items-center gap-1 text-xs text-codex-dim mb-2">
        <button onClick={() => load('.')} className="hover:text-codex-accent">~</button>
        <span>/</span>
        <span className="text-codex-text">{cwd}</span>
        {loading && <span className="animate-pulse ml-1">…</span>}
      </div>
      <div className="space-y-0.5 max-h-96 overflow-y-auto">
        {entries.map(e => (
          <div key={e.path}
            onClick={() => e.type === 'directory' && load(e.path)}
            className={`flex items-center gap-2 px-2 py-1 rounded text-xs cursor-pointer transition-colors ${
              e.type === 'directory' ? 'text-codex-accent hover:bg-codex-accent/10' : 'text-codex-text hover:bg-codex-surface'
            }`}
          >
            <span>{e.type === 'directory' ? '📁' : '📄'}</span>
            <span className="truncate">{e.name}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function SkillsPanel() {
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [editing, setEditing] = useState<SkillInfo | null>(null)
  const [detail, setDetail] = useState<api.SkillDetail | null>(null)
  const [form, setForm] = useState({ name: '', description: '', content: '' })
  const [creating, setCreating] = useState(false)

  const reload = async () => {
    try { setSkills(await api.skills.list()) } catch { /* offline */ }
  }
  useEffect(() => { reload() }, [])

  const openEditor = async (s: SkillInfo) => {
    try {
      const d = await api.skills.get(s.name)
      setDetail(d)
      setForm({ name: d.name, description: d.description, content: d.content })
      setEditing(s)
      setCreating(false)
    } catch { /* offline */ }
  }

  const saveSkill = async () => {
    try {
      if (creating) {
        await api.skills.create(form.name, form.description, form.content)
      } else if (editing) {
        await api.skills.update(editing.name, { description: form.description, content: form.content })
      }
      setEditing(null); setCreating(false); reload()
    } catch (e: any) { alert(e.message) }
  }

  const deleteSkill = async (name: string) => {
    if (!confirm(`Delete skill "${name}"?`)) return
    try { await api.skills.delete(name); reload() } catch (e: any) { alert(e.message) }
  }

  return (
    <div className="p-3">
      <button onClick={() => { setCreating(true); setEditing(null); setForm({ name: '', description: '', content: '# My Skill\n\nInstructions here...\n' }); setDetail(null) }}
        className="w-full py-2 rounded bg-codex-purple/10 border border-codex-purple/30 text-codex-purple text-sm font-medium hover:bg-codex-purple/20 transition-colors mb-3">
        + New Skill
      </button>
      <div className="space-y-1 max-h-64 overflow-y-auto">
        {skills.map(s => (
          <div key={s.name} onClick={() => openEditor(s)}
            className="flex items-center justify-between px-3 py-2 rounded text-sm cursor-pointer hover:bg-codex-surface border border-transparent hover:border-codex-border transition-colors text-codex-text">
            <div className="min-w-0">
              <div className="font-medium text-codex-purple truncate">{s.name}</div>
              <div className="text-[11px] text-codex-dim truncate">{s.description}</div>
            </div>
            <button onClick={e => { e.stopPropagation(); deleteSkill(s.name) }}
              className="text-codex-dim hover:text-codex-red shrink-0 ml-2 text-lg leading-none">×</button>
          </div>
        ))}
      </div>

      {/* Editor modal */}
      {(editing || creating) && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
          <div className="bg-codex-surface border border-codex-border rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[90vh] overflow-y-auto shadow-2xl">
            <h2 className="text-lg font-semibold text-codex-purple mb-4">{creating ? 'New Skill' : `Edit: ${editing?.name}`}</h2>
            {creating && (
              <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
                placeholder="skill-name (lowercase, hyphens)"
                className="w-full bg-codex-bg border border-codex-border rounded p-2 text-codex-text text-sm mb-3 focus:border-codex-accent focus:outline-none" />
            )}
            <input value={form.description} onChange={e => setForm({ ...form, description: e.target.value })}
              placeholder="Description"
              className="w-full bg-codex-bg border border-codex-border rounded p-2 text-codex-text text-sm mb-3 focus:border-codex-accent focus:outline-none" />
            <textarea value={form.content} onChange={e => setForm({ ...form, content: e.target.value })}
              rows={15}
              className="w-full bg-codex-bg border border-codex-border rounded p-3 text-codex-text text-sm mb-4 resize-none focus:border-codex-accent focus:outline-none font-mono" />
            <div className="flex gap-3 justify-end">
              <button onClick={() => { setEditing(null); setCreating(false) }}
                className="px-4 py-2 rounded border border-codex-border text-codex-dim hover:bg-codex-border/30 transition-colors text-sm">Cancel</button>
              <button onClick={saveSkill}
                className="px-4 py-2 rounded bg-codex-purple text-white hover:bg-purple-600 transition-colors text-sm font-medium">Save</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function McpPanel() {
  const [servers, setServers] = useState<McpServerCfg[]>([])
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({ name: '', command: '', args: '' })
  const [editingName, setEditingName] = useState<string | null>(null)

  const reload = async () => {
    try { setServers(await api.mcp.list()) } catch { /* offline */ }
  }
  useEffect(() => { reload() }, [])

  const openNew = () => {
    setEditingName(null); setForm({ name: '', command: 'npx', args: '-y @anthropic/mcp-server' }); setEditing(true)
  }

  const openEdit = (s: McpServerCfg) => {
    setEditingName(s.name); setForm({ name: s.name, command: s.command, args: s.args.join(' ') }); setEditing(true)
  }

  const save = async () => {
    try {
      const argsList = form.args.split(/\s+/).filter(Boolean)
      if (editingName) {
        await api.mcp.update(editingName, form.command, argsList)
      } else {
        await api.mcp.create(form.name, form.command, argsList)
      }
      setEditing(false); reload()
    } catch (e: any) { alert(e.message) }
  }

  const remove = async (name: string) => {
    if (!confirm(`Remove MCP server "${name}"?`)) return
    try { await api.mcp.delete(name); reload() } catch (e: any) { alert(e.message) }
  }

  return (
    <div className="p-3">
      <button onClick={openNew}
        className="w-full py-2 rounded bg-codex-green/10 border border-codex-green/30 text-codex-green text-sm font-medium hover:bg-codex-green/20 transition-colors mb-3">
        + Add MCP Server
      </button>
      <div className="space-y-1 max-h-64 overflow-y-auto">
        {servers.map(s => (
          <div key={s.name} onClick={() => openEdit(s)}
            className="flex items-center justify-between px-3 py-2 rounded text-sm cursor-pointer hover:bg-codex-surface border border-transparent hover:border-codex-border transition-colors text-codex-text">
            <div className="min-w-0">
              <div className="font-medium truncate">{s.name}</div>
              <div className="text-[11px] text-codex-dim truncate font-mono">{s.command} {s.args.join(' ')}</div>
            </div>
            <button onClick={e => { e.stopPropagation(); remove(s.name) }}
              className="text-codex-dim hover:text-codex-red shrink-0 ml-2 text-lg leading-none">×</button>
          </div>
        ))}
        {servers.length === 0 && <div className="text-codex-dim text-xs text-center py-4">No MCP servers configured</div>}
      </div>

      {/* Editor modal */}
      {editing && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
          <div className="bg-codex-surface border border-codex-border rounded-lg p-6 max-w-lg w-full mx-4 shadow-2xl">
            <h2 className="text-lg font-semibold text-codex-green mb-4">{editingName ? `Edit: ${editingName}` : 'Add MCP Server'}</h2>
            {!editingName && (
              <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
                placeholder="Server name"
                className="w-full bg-codex-bg border border-codex-border rounded p-2 text-codex-text text-sm mb-3 focus:border-codex-accent focus:outline-none" />
            )}
            <input value={form.command} onChange={e => setForm({ ...form, command: e.target.value })}
              placeholder="Command (e.g. npx, uvx, python)"
              className="w-full bg-codex-bg border border-codex-border rounded p-2 text-codex-text text-sm mb-3 focus:border-codex-accent focus:outline-none font-mono" />
            <input value={form.args} onChange={e => setForm({ ...form, args: e.target.value })}
              placeholder="Arguments (space-separated)"
              className="w-full bg-codex-bg border border-codex-border rounded p-2 text-codex-text text-sm mb-4 focus:border-codex-accent focus:outline-none font-mono" />
            <div className="flex gap-3 justify-end">
              <button onClick={() => setEditing(false)}
                className="px-4 py-2 rounded border border-codex-border text-codex-dim hover:bg-codex-border/30 transition-colors text-sm">Cancel</button>
              <button onClick={save}
                className="px-4 py-2 rounded bg-codex-green text-white hover:bg-green-600 transition-colors text-sm font-medium">Save</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// Main App
// ═══════════════════════════════════════════════════════════════════════

export default function App() {
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [activeSid, setActiveSid] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [tools, setTools] = useState<ToolEntry[]>([])
  const [permReq, setPermReq] = useState<PermRequest | null>(null)
  const [askReq, setAskReq] = useState<AskRequest | null>(null)
  const [input, setInput] = useState('')
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>('sessions')
  const [sending, setSending] = useState(false)
  const chatEndRef = useRef<HTMLDivElement>(null)
  const disconnectRef = useRef<(() => void) | null>(null)

  // ── Session management ──────────────────────────────────────────
  const loadSessions = async () => {
    try { setSessions(await api.sessions.list()) } catch { /* offline */ }
  }
  useEffect(() => { loadSessions() }, [])

  const selectSession = async (sid: string) => {
    // Disconnect previous
    disconnectRef.current?.()
    setActiveSid(sid)
    setMessages([])
    setTools([])
    setPermReq(null)
    setAskReq(null)

    // Connect WebSocket
    disconnectRef.current = api.connectStream(sid,
      (event: AgentEvent) => handleEvent(event, sid),
      () => { /* disconnected */ },
    )
  }

  const createSession = async () => {
    try {
      const s = await api.sessions.create()
      setSessions(prev => [s, ...prev])
      selectSession(s.id)
    } catch (e: any) { alert(e.message) }
  }

  const deleteSession = async (sid: string) => {
    try {
      await api.sessions.delete(sid)
      setSessions(prev => prev.filter(s => s.id !== sid))
      if (activeSid === sid) { setActiveSid(null); disconnectRef.current?.() }
    } catch (e: any) { alert(e.message) }
  }

  // ── Event handler ───────────────────────────────────────────────
  const handleEvent = (event: AgentEvent, sid: string) => {
    const { type, payload } = event

    switch (type) {
      case 'run.started':
        break
      case 'step.started':
        break
      case 'tool.started':
        setTools(prev => [...prev, {
          id: payload.tool_call_id || uid(),
          name: payload.tool,
          input: payload.input || {},
          status: 'running',
          step: event.step,
        }])
        break
      case 'tool.completed':
        setTools(prev => prev.map(t =>
          t.id === payload.tool_call_id ? { ...t, status: payload.status === 'success' ? 'success' : 'error', output: payload.output } : t
        ))
        break
      case 'assistant.final':
        setMessages(prev => {
          const last = prev[prev.length - 1]
          if (last && last.role === 'assistant' && last.streaming) {
            return prev.map((m, i) => i === prev.length - 1 ? { ...m, content: payload.content, streaming: false, step: payload.step } : m)
          }
          return [...prev, { id: uid(), role: 'assistant', content: payload.content, streaming: false, step: payload.step }]
        })
        setSending(false)
        break
      case 'run.finished':
        setSending(false)
        loadSessions()
        break
      case 'permission.requested':
        setPermReq({ requestId: payload.request_id, tool: payload.tool, path: payload.path, action: payload.action })
        break
      case 'ask_user.requested':
        setAskReq({ requestId: payload.request_id, prompt: payload.prompt })
        break
      case 'turn.completed':
        setSending(false)
        loadSessions()
        break
      case 'error':
        setMessages(prev => [...prev, { id: uid(), role: 'assistant', content: `❌ Error: ${payload.message}` }])
        setSending(false)
        break
    }
  }

  // ── Auto-scroll ─────────────────────────────────────────────────
  useEffect(() => { chatEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, tools])

  // ── Send message ────────────────────────────────────────────────
  const sendMessage = async () => {
    if (!activeSid || !input.trim() || sending) return
    const content = input.trim()
    setInput('')
    setSending(true)
    setMessages(prev => [...prev, { id: uid(), role: 'user', content }])
    // Add placeholder for streaming
    const assistantId = uid()
    setMessages(prev => [...prev, { id: assistantId, role: 'assistant', content: '', streaming: true }])

    try {
      await api.sessions.send(activeSid, content)
    } catch (e: any) {
      setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: `❌ ${e.message}`, streaming: false } : m))
      setSending(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
  }

  // ── Render ──────────────────────────────────────────────────────
  const tabs: { key: SidebarTab; label: string; icon: string }[] = [
    { key: 'sessions', label: 'Sessions', icon: '💬' },
    { key: 'files', label: 'Files', icon: '📁' },
    { key: 'skills', label: 'Skills', icon: '🎯' },
    { key: 'mcp', label: 'MCP', icon: '🔌' },
  ]

  return (
    <div className="flex h-screen bg-codex-bg overflow-hidden">
      {/* ── Sidebar ─────────────────────────────────────────────── */}
      <aside className="w-64 bg-codex-surface border-r border-codex-border flex flex-col shrink-0">
        <div className="px-4 py-3 border-b border-codex-border">
          <div className="flex items-center gap-2">
            <span className="text-lg">🐱</span>
            <span className="font-semibold text-codex-text">MyCodeAgent</span>
          </div>
        </div>
        <div className="flex border-b border-codex-border">
          {tabs.map(t => (
            <button key={t.key} onClick={() => setSidebarTab(t.key)}
              className={`flex-1 py-2 text-xs font-medium transition-colors ${
                sidebarTab === t.key ? 'text-codex-accent border-b-2 border-codex-accent' : 'text-codex-dim hover:text-codex-text'
              }`}
              title={t.label}>{t.icon}</button>
          ))}
        </div>
        <div className="flex-1 overflow-y-auto">
          {sidebarTab === 'sessions' && <SessionsPanel sessions={sessions} activeId={activeSid} onSelect={selectSession} onCreate={createSession} onDelete={deleteSession} />}
          {sidebarTab === 'files' && <FilesPanel sid={activeSid} />}
          {sidebarTab === 'skills' && <SkillsPanel />}
          {sidebarTab === 'mcp' && <McpPanel />}
        </div>
        <div className="px-3 py-2 border-t border-codex-border text-[10px] text-codex-dim">
          {activeSid ? `Session: ${activeSid.slice(0, 8)}` : 'No session'}
        </div>
      </aside>

      {/* ── Main chat ───────────────────────────────────────────── */}
      <main className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <header className="px-4 py-3 border-b border-codex-border bg-codex-surface flex items-center justify-between shrink-0">
          <div className="text-sm text-codex-dim">
            {activeSid ? (
              <span>Session <code className="text-codex-text">{activeSid.slice(0, 8)}</code></span>
            ) : (
              <span className="text-codex-dim">Create or select a session to start</span>
            )}
          </div>
          {sending && <span className="text-xs text-codex-yellow animate-pulse">● Agent working…</span>}
        </header>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
          {messages.map(msg => (
            <Fragment key={msg.id}>
              <ChatMessage msg={msg} />
              {/* Show tool calls that happened in this step */}
              {tools.filter(t => t.step === msg.step).map(t => (
                <ToolCard key={t.id} tool={t} />
              ))}
            </Fragment>
          ))}
          {messages.length === 0 && !sending && (
            <div className="flex items-center justify-center h-full text-codex-dim text-sm">
              {activeSid ? 'Send a message to start…' : 'Create a session →'}
            </div>
          )}
          <div ref={chatEndRef} />
        </div>

        {/* Input */}
        <div className="px-4 py-3 border-t border-codex-border bg-codex-surface shrink-0">
          <div className="flex gap-2">
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={!activeSid || sending}
              rows={2}
              placeholder={activeSid ? 'Type a message… (Enter to send, Shift+Enter for newline)' : 'Select a session first'}
              className="flex-1 bg-codex-bg border border-codex-border rounded-lg px-4 py-2 text-sm text-codex-text resize-none focus:border-codex-accent focus:outline-none disabled:opacity-50 font-mono"
            />
            <button
              onClick={sendMessage}
              disabled={!activeSid || sending || !input.trim()}
              className="px-5 py-2 rounded-lg bg-codex-accent text-white font-medium text-sm hover:bg-blue-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
            >
              {sending ? '…' : 'Send'}
            </button>
          </div>
        </div>
      </main>

      {/* ── Modals ───────────────────────────────────────────────── */}
      {permReq && activeSid && <PermissionModal req={permReq} sid={activeSid} onDone={() => setPermReq(null)} />}
      {askReq && activeSid && <AskUserModal req={askReq} sid={activeSid} onDone={() => setAskReq(null)} />}
    </div>
  )
}
