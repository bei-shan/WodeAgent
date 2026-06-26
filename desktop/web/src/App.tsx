import { useState, useEffect, useRef, useCallback } from 'react'
import * as api from './api'
import type { SessionInfo, AgentEvent, FileEntry, SkillInfo, McpServerCfg, TeamInfo } from './api'

// ═══════════════════════════════════════════════════════════════════════
// Types
// ═══════════════════════════════════════════════════════════════════════

interface Message { id: string; role: 'user' | 'assistant'; content: string; step?: number; streaming?: boolean }
interface ToolEntry { id: string; name: string; input: Record<string, any>; status: 'running' | 'success' | 'error'; output?: string; step: number }
interface PermRequest { requestId: string; tool: string; path: string; action: string }
interface AskRequest { requestId: string; prompt: string }

const uid = () => Math.random().toString(36).slice(2, 10)

// ── Quick scenario tags ────────────────────────────────────────────
const SCENARIOS = [
  { label: '代码审查', prompt: '请帮我审查以下代码，关注安全性、性能和最佳实践：' },
  { label: '调研报告', prompt: '请针对以下主题进行深度调研，生成一份结构化的报告：' },
  { label: 'Bug 修复', prompt: '以下代码有一个 bug，请帮我定位并修复：' },
  { label: '架构设计', prompt: '请帮我设计以下功能的系统架构：' },
  { label: '写测试', prompt: '请为以下代码编写单元测试：' },
  { label: '代码重构', prompt: '请重构以下代码，提高可读性和可维护性：' },
]

// ═══════════════════════════════════════════════════════════════════════
// Icons (inline SVGs to avoid icon library dependency)
// ═══════════════════════════════════════════════════════════════════════

const Icons = {
  search:   '🔍', skill: '🎯', clock: '⏱️', folder: '📁',
  teams:    '👥', download: '📥', settings: '⚙️', plus: '+',
  send:     '↑', brain: '🧠', check: '✓', cross: '✗',
  dot:      '●', file: '📄', chat: '💬', agent: '🤖',
  user:     '👤', lock: '🔒', ask: '❓',
}

// ═══════════════════════════════════════════════════════════════════════
// Permission Modal
// ═══════════════════════════════════════════════════════════════════════

function PermissionModal({ req, sid, onDone }: { req: PermRequest; sid: string; onDone: () => void }) {
  const decide = async (d: string) => { await api.sessions.resolvePerm(sid, req.requestId, d); onDone() }
  return (
    <div className="fixed inset-0 bg-black/25 flex items-center justify-center z-50 animate-fade-in" onClick={() => decide('denied')}>
      <div className="bg-white rounded-2xl p-6 max-w-md w-full mx-4 shadow-xl border border-surface-border" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-2 text-amber-600 mb-3"><span className="text-xl">{Icons.lock}</span><h2 className="text-lg font-semibold text-surface-text">权限请求</h2></div>
        <div className="space-y-2 text-sm text-surface-text mb-5">
          <p><span className="text-surface-muted">工具:</span> <code className="text-brand-600 font-medium">{req.tool}</code></p>
          <p><span className="text-surface-muted">路径:</span> <code className="text-amber-700 break-all text-xs">{req.path}</code></p>
          <p><span className="text-surface-muted">操作:</span> {req.action}</p>
        </div>
        <div className="flex gap-3 justify-end">
          <button onClick={() => decide('denied')} className="px-5 py-2 rounded-xl border border-surface-border text-surface-muted hover:bg-surface-hover transition-colors text-sm font-medium">拒绝</button>
          <button onClick={() => decide('granted')} className="px-5 py-2 rounded-xl bg-brand-500 text-white hover:bg-brand-600 transition-colors text-sm font-medium">允许一次</button>
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// AskUser Modal
// ═══════════════════════════════════════════════════════════════════════

function AskUserModal({ req, sid, onDone }: { req: AskRequest; sid: string; onDone: () => void }) {
  const [answer, setAnswer] = useState('')
  const submit = async () => { await api.sessions.answerAsk(sid, req.requestId, answer || '(无回答)'); onDone() }
  return (
    <div className="fixed inset-0 bg-black/25 flex items-center justify-center z-50 animate-fade-in">
      <div className="bg-white rounded-2xl p-6 max-w-md w-full mx-4 shadow-xl border border-surface-border">
        <div className="flex items-center gap-2 text-brand-600 mb-3"><span className="text-xl">{Icons.ask}</span><h2 className="text-lg font-semibold text-surface-text">Agent 提问</h2></div>
        <pre className="text-sm text-surface-text mb-4 whitespace-pre-wrap bg-surface-hover p-3 rounded-xl">{req.prompt}</pre>
        <textarea value={answer} onChange={e => setAnswer(e.target.value)} rows={3}
          className="w-full bg-surface-hover border border-surface-border rounded-xl p-3 text-surface-text text-sm mb-4 resize-none input-focus-ring"
          placeholder="输入你的回答..."
          onKeyDown={e => { if (e.key === 'Enter' && e.ctrlKey) submit() }} />
        <div className="flex gap-3 justify-end">
          <button onClick={submit} className="px-5 py-2 rounded-xl bg-brand-500 text-white hover:bg-brand-600 transition-colors text-sm font-medium">回答 (Ctrl+Enter)</button>
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// Tool card — inline in chat
// ═══════════════════════════════════════════════════════════════════════

function ToolCard({ tool }: { tool: ToolEntry }) {
  const [expanded, setExpanded] = useState(false)
  const colors = tool.status === 'running' ? 'border-amber-300 bg-amber-50' : tool.status === 'error' ? 'border-red-300 bg-red-50' : 'border-emerald-300 bg-emerald-50'
  const dotColor = tool.status === 'running' ? 'text-amber-500 animate-pulse-dot' : tool.status === 'error' ? 'text-red-500' : 'text-emerald-500'
  return (
    <div className={`ml-10 border rounded-xl px-3 py-2 text-xs animate-slide-up ${colors}`}>
      <div className="flex items-center gap-2 cursor-pointer select-none" onClick={() => setExpanded(!expanded)}>
        <span className={dotColor}>{Icons.dot}</span>
        <span className="font-medium text-surface-text">{tool.name}</span>
        <span className="text-surface-muted ml-auto text-[10px]">{expanded ? '收起' : '展开'}</span>
      </div>
      {expanded && (
        <div className="mt-2 space-y-1">
          <div className="text-surface-muted text-[10px] uppercase tracking-wide">Input</div>
          <pre className="bg-white/70 p-2 rounded-lg text-[11px] text-surface-text overflow-x-auto max-h-24">{JSON.stringify(tool.input, null, 2)}</pre>
          {tool.output && <>
            <div className="text-surface-muted text-[10px] uppercase tracking-wide mt-1">Output</div>
            <pre className="bg-white/70 p-2 rounded-lg text-[11px] text-surface-text overflow-x-auto max-h-40 whitespace-pre-wrap">{tool.output.length > 2000 ? tool.output.slice(0, 2000) + '...(截断)' : tool.output}</pre>
          </>}
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
  const [sending, setSending] = useState(false)

  // Config state
  const [agentTeams, setAgentTeams] = useState(false)
  const [thinkingLevel, setThinkingLevel] = useState('medium')
  const [teamsList, setTeamsList] = useState<TeamInfo[]>([])
  const [sidebarTab, setSidebarTab] = useState<'sessions'|'files'|'skills'|'mcp'>('sessions')
  const [showNewTeam, setShowNewTeam] = useState(false)
  const [newTeamName, setNewTeamName] = useState('')

  // Panels
  const [skillsList, setSkillsList] = useState<SkillInfo[]>([])
  const [mcpServers, setMcpServers] = useState<McpServerCfg[]>([])
  const [fileEntries, setFileEntries] = useState<FileEntry[]>([])
  const [fileCwd, setFileCwd] = useState('.')

  const chatEndRef = useRef<HTMLDivElement>(null)
  const disconnectRef = useRef<(() => void) | null>(null)

  // ── Sessions ────────────────────────────────────────────────────
  const loadSessions = async () => { try { setSessions(await api.sessions.list()) } catch {} }
  useEffect(() => { loadSessions() }, [])

  const selectSession = async (sid: string) => {
    disconnectRef.current?.()
    setActiveSid(sid); setMessages([]); setTools([]); setPermReq(null); setAskReq(null)
    disconnectRef.current = api.connectStream(sid, handleEvent, () => {})
    try { const cfg = await api.config.get(sid); setAgentTeams(cfg.enable_agent_teams); setThinkingLevel(cfg.thinking_level) } catch {}
    try { setTeamsList(await api.teams.list(sid)) } catch {}
  }

  const createSession = async () => {
    try { const s = await api.sessions.create(); setSessions(prev => [s, ...prev]); selectSession(s.id) } catch (e: any) { alert(e.message) }
  }

  const deleteSession = async (sid: string) => {
    try { await api.sessions.delete(sid); setSessions(prev => prev.filter(s => s.id !== sid)); if (activeSid === sid) { setActiveSid(null); disconnectRef.current?.() } } catch {}
  }

  // ── Agent teams toggle ──────────────────────────────────────────
  const toggleTeams = async () => {
    if (!activeSid) return
    const next = !agentTeams
    setAgentTeams(next)
    try { await api.config.update(activeSid, { enable_agent_teams: next }); setTeamsList(await api.teams.list(activeSid)) } catch { setAgentTeams(!next) }
  }

  const createTeam = async () => {
    if (!activeSid || !newTeamName.trim()) return
    try { await api.teams.create(activeSid, newTeamName.trim()); setTeamsList(await api.teams.list(activeSid)); setNewTeamName(''); setShowNewTeam(false) } catch (e: any) { alert(e.message) }
  }

  // ── Sidebar panels data ─────────────────────────────────────────
  const loadSidebarData = async () => {
    try { setSkillsList(await api.skills.list()) } catch {}
    try { setMcpServers(await api.mcp.list()) } catch {}
  }
  useEffect(() => { loadSidebarData() }, [])

  const loadFiles = async (path: string) => {
    if (!activeSid) return
    try { const tree = await api.files.tree(activeSid, path); setFileEntries(tree.entries); setFileCwd(tree.path) } catch {}
  }
  useEffect(() => { if (activeSid) loadFiles('.') }, [activeSid])

  // ── Event handler ───────────────────────────────────────────────
  const handleEvent = (event: AgentEvent) => {
    const { type, payload } = event
    switch (type) {
      case 'tool.started':
        setTools(prev => [...prev, { id: payload.tool_call_id || uid(), name: payload.tool, input: payload.input || {}, status: 'running', step: event.step }]); break
      case 'tool.completed':
        setTools(prev => prev.map(t => t.id === payload.tool_call_id ? { ...t, status: payload.status === 'success' ? 'success' : 'error', output: payload.output } : t)); break
      case 'assistant.final':
        setMessages(prev => {
          const last = prev[prev.length - 1]
          if (last && last.role === 'assistant' && last.streaming) return prev.map((m, i) => i === prev.length - 1 ? { ...m, content: payload.content, streaming: false, step: payload.step } : m)
          return [...prev, { id: uid(), role: 'assistant', content: payload.content, streaming: false, step: payload.step }]
        }); setSending(false); break
      case 'run.finished': setSending(false); loadSessions(); break
      case 'permission.requested': setPermReq({ requestId: payload.request_id, tool: payload.tool, path: payload.path, action: payload.action }); break
      case 'ask_user.requested': setAskReq({ requestId: payload.request_id, prompt: payload.prompt }); break
      case 'turn.completed': setSending(false); loadSessions(); if (activeSid) { try { api.teams.list(activeSid).then(setTeamsList) } catch {} }; break
      case 'error': setMessages(prev => [...prev, { id: uid(), role: 'assistant', content: `❌ ${payload.message}` }]); setSending(false); break
    }
  }

  useEffect(() => { chatEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, tools])

  // ── Send ────────────────────────────────────────────────────────
  const sendMessage = async () => {
    if (!activeSid || !input.trim() || sending) return
    const content = input.trim(); setInput(''); setSending(true)
    setMessages(prev => [...prev, { id: uid(), role: 'user', content }])
    const aid = uid(); setMessages(prev => [...prev, { id: aid, role: 'assistant', content: '', streaming: true }])
    try { await api.sessions.send(activeSid, content) } catch (e: any) {
      setMessages(prev => prev.map(m => m.id === aid ? { ...m, content: `❌ ${e.message}`, streaming: false } : m)); setSending(false)
    }
  }

  const fillScenario = (prompt: string) => { setInput(prompt) }

  const handleKeyDown = (e: React.KeyboardEvent) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() } }

  // ── Model selector ──────────────────────────────────────────────
  const [models, setModels] = useState<api.ModelInfo[]>([])
  const [showModelPicker, setShowModelPicker] = useState(false)
  useEffect(() => { api.info.models().then(setModels).catch(() => {}) }, [])

  // ═══════════════════════════════════════════════════════════════════
  // Render
  // ═══════════════════════════════════════════════════════════════════

  const sidebarTabs = [
    { key: 'sessions' as const, label: '会话', icon: Icons.chat },
    { key: 'files' as const, label: '文件', icon: Icons.folder },
    { key: 'skills' as const, label: '技能', icon: Icons.skill },
    { key: 'mcp' as const, label: 'MCP', icon: Icons.settings },
  ]

  return (
    <div className="flex h-screen bg-surface-bg overflow-hidden">
      {/* ═════════════════════ LEFT SIDEBAR ═══════════════════════ */}
      <aside className="w-64 bg-surface-bg border-r border-surface-border flex flex-col shrink-0">
        {/* Brand + New Task */}
        <div className="px-4 py-4 border-b border-surface-border">
          <div className="flex items-center gap-2 mb-3">
            <span className="text-xl">🐱</span>
            <span className="font-bold text-surface-text text-lg">MyCodeAgent</span>
          </div>
          <button onClick={createSession}
            className="w-full py-2.5 rounded-xl bg-brand-500 text-white text-sm font-semibold hover:bg-brand-600 transition-colors shadow-sm">
            + 新建任务
          </button>
        </div>

        {/* Nav tabs */}
        <div className="flex border-b border-surface-border px-2">
          {sidebarTabs.map(t => (
            <button key={t.key} onClick={() => setSidebarTab(t.key)}
              className={`flex-1 py-2.5 text-xs font-medium transition-colors rounded-t-lg ${
                sidebarTab === t.key ? 'text-brand-600 border-b-2 border-brand-500' : 'text-surface-muted hover:text-surface-text'
              }`}>{t.icon} {t.label}</button>
          ))}
        </div>

        {/* Sidebar content */}
        <div className="flex-1 overflow-y-auto">
          {sidebarTab === 'sessions' && (
            <div className="p-3 space-y-1">
              <div className="text-[11px] font-semibold text-surface-muted uppercase tracking-wide px-2 mb-2">最近任务</div>
              {sessions.map(s => (
                <div key={s.id} onClick={() => selectSession(s.id)}
                  className={`flex items-center justify-between px-3 py-2 rounded-xl text-sm cursor-pointer transition-colors ${
                    s.id === activeSid ? 'bg-brand-50 text-brand-700 font-medium' : 'hover:bg-surface-hover text-surface-text'
                  }`}>
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={`w-2 h-2 rounded-full shrink-0 ${s.busy ? 'bg-amber-400 animate-pulse-dot' : 'bg-emerald-400'}`} />
                    <span className="truncate">{s.title || s.id.slice(0, 8)}</span>
                  </div>
                  <button onClick={e => { e.stopPropagation(); deleteSession(s.id) }}
                    className="text-surface-dim hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity text-sm leading-none">×</button>
                </div>
              ))}
              {sessions.length === 0 && <div className="text-surface-muted text-xs text-center py-6">暂无任务记录</div>}
            </div>
          )}

          {sidebarTab === 'files' && (
            <div className="p-3">
              <div className="flex items-center gap-1 text-xs text-surface-muted mb-2">
                <button onClick={() => loadFiles('.')} className="hover:text-brand-500">~</button><span>/</span><span className="text-surface-text">{fileCwd}</span>
              </div>
              <div className="space-y-0.5 max-h-96 overflow-y-auto">
                {fileEntries.map(e => (
                  <div key={e.path} onClick={() => e.type === 'directory' && loadFiles(e.path)}
                    className={`flex items-center gap-2 px-2 py-1.5 rounded-lg text-xs cursor-pointer transition-colors ${
                      e.type === 'directory' ? 'text-brand-600 hover:bg-brand-50' : 'text-surface-text hover:bg-surface-hover'
                    }`}>
                    <span>{e.type === 'directory' ? Icons.folder : Icons.file}</span>
                    <span className="truncate">{e.name}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {sidebarTab === 'skills' && (
            <div className="p-3 space-y-1">
              <div className="text-[11px] font-semibold text-surface-muted uppercase tracking-wide px-2 mb-2">已安装技能</div>
              {skillsList.map(s => (
                <div key={s.name} className="px-3 py-2 rounded-xl text-sm hover:bg-surface-hover transition-colors cursor-pointer text-surface-text">
                  <div className="font-medium text-brand-700 truncate">{s.name}</div>
                  <div className="text-[11px] text-surface-muted truncate">{s.description}</div>
                </div>
              ))}
              {skillsList.length === 0 && <div className="text-surface-muted text-xs text-center py-4">暂无技能</div>}
            </div>
          )}

          {sidebarTab === 'mcp' && (
            <div className="p-3 space-y-1">
              <div className="text-[11px] font-semibold text-surface-muted uppercase tracking-wide px-2 mb-2">MCP 服务器</div>
              {mcpServers.map(s => (
                <div key={s.name} className="px-3 py-2 rounded-xl text-sm hover:bg-surface-hover transition-colors cursor-pointer text-surface-text">
                  <div className="font-medium truncate">{s.name}</div>
                  <div className="text-[11px] text-surface-muted truncate font-mono">{s.command} {s.args.join(' ')}</div>
                </div>
              ))}
              {mcpServers.length === 0 && <div className="text-surface-muted text-xs text-center py-4">未配置 MCP</div>}
            </div>
          )}
        </div>

        {/* Agent Teams section */}
        <div className="border-t border-surface-border p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold text-surface-muted uppercase tracking-wide">{Icons.teams} Agent 团队</span>
            <label className="toggle"><input type="checkbox" checked={agentTeams} onChange={toggleTeams} disabled={!activeSid} /><span className="slider" /></label>
          </div>
          {agentTeams && (
            <div className="space-y-1 max-h-32 overflow-y-auto">
              {teamsList.map(t => (
                <div key={t.name} className="flex items-center justify-between px-2 py-1 text-xs text-surface-text hover:bg-surface-hover rounded-lg transition-colors">
                  <span className="font-medium">{t.name}</span>
                  <span className="text-surface-muted">{t.running}跑 {t.succeeded}成 {t.failed}败</span>
                </div>
              ))}
              <button onClick={() => setShowNewTeam(true)}
                className="w-full text-xs text-brand-500 hover:text-brand-600 py-1 text-left font-medium">+ 创建团队</button>
              {showNewTeam && (
                <div className="flex gap-1 mt-1">
                  <input value={newTeamName} onChange={e => setNewTeamName(e.target.value)}
                    placeholder="团队名称" className="flex-1 bg-surface-hover border border-surface-border rounded-lg px-2 py-1 text-xs input-focus-ring" />
                  <button onClick={createTeam} className="px-2 py-1 bg-brand-500 text-white rounded-lg text-xs font-medium hover:bg-brand-600">创建</button>
                </div>
              )}
            </div>
          )}
        </div>

        {/* User footer */}
        <div className="border-t border-surface-border px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-full bg-brand-100 flex items-center justify-center text-brand-600 text-xs font-bold">U</div>
            <div className="text-xs text-surface-text font-medium">用户</div>
          </div>
          <div className="flex gap-1">
            <button className="w-7 h-7 flex items-center justify-center rounded-lg hover:bg-surface-hover text-surface-muted text-sm" title="下载">{Icons.download}</button>
            <button className="w-7 h-7 flex items-center justify-center rounded-lg hover:bg-surface-hover text-surface-muted text-sm" title="设置">{Icons.settings}</button>
          </div>
        </div>
      </aside>

      {/* ═════════════════════ RIGHT MAIN AREA ════════════════════ */}
      <main className="flex-1 flex flex-col min-w-0 bg-surface-bg">
        {/* Top bar */}
        <header className="px-6 py-3 border-b border-surface-border flex items-center justify-end shrink-0 gap-3">
          {sending && <span className="text-xs text-brand-500 animate-pulse-dot font-medium">Agent 处理中…</span>}
          <button className="px-3 py-1.5 text-xs text-surface-muted hover:text-surface-text hover:bg-surface-hover rounded-lg transition-colors">{Icons.download} 导出</button>
          <button className="px-3 py-1.5 text-xs text-surface-muted hover:text-surface-text hover:bg-surface-hover rounded-lg transition-colors">{Icons.settings} 设置</button>
        </header>

        {/* Scrollable chat area */}
        <div className="flex-1 overflow-y-auto px-6">
          {messages.length === 0 && !sending ? (
            /* ── Empty state: hero + input center ── */
            <div className="flex flex-col items-center justify-center min-h-full max-w-2xl mx-auto py-12">
              <h1 className="text-2xl font-bold text-surface-text mb-2 tracking-tight">今天想做什么？</h1>
              <p className="text-surface-muted text-sm mb-8">描述你的需求，Agent 将自动调用工具完成任务</p>

              {/* Large input */}
              <div className="w-full relative mb-6">
                <textarea value={input} onChange={e => setInput(e.target.value)} onKeyDown={handleKeyDown}
                  disabled={!activeSid || sending} rows={3}
                  placeholder={activeSid ? '描述你的需求…' : '请先创建会话'}
                  className="w-full bg-surface-bg border border-surface-border rounded-3xl px-6 py-4 text-surface-text resize-none input-focus-ring shadow-input text-[15px] leading-relaxed disabled:opacity-40"
                />
                {/* Input accessories */}
                <div className="absolute bottom-3 left-4 flex items-center gap-2">
                  <button className="w-8 h-8 rounded-full bg-surface-hover flex items-center justify-center text-surface-muted hover:bg-surface-active hover:text-surface-text transition-colors text-sm" title="添加附件">{Icons.plus}</button>
                  {agentTeams && <span className="text-[11px] bg-brand-50 text-brand-600 px-2 py-0.5 rounded-full font-medium">{Icons.teams} Agent 团队</span>}
                </div>
                <div className="absolute bottom-3 right-4 flex items-center gap-2">
                  {/* Thinking toggle */}
                  <label className="flex items-center gap-1.5 cursor-pointer select-none">
                    <span className="text-[11px] text-surface-muted">{Icons.brain} 深度思考</span>
                    <label className="toggle"><input type="checkbox" checked={thinkingLevel === 'high'} onChange={() => setThinkingLevel(thinkingLevel === 'high' ? 'medium' : 'high')} /><span className="slider" /></label>
                  </label>
                  {/* Model selector */}
                  <div className="relative">
                    <button onClick={() => setShowModelPicker(!showModelPicker)}
                      className="px-2.5 py-1 text-[11px] text-surface-muted hover:text-surface-text bg-surface-hover rounded-lg transition-colors font-mono">
                      {models[0]?.name || 'Model'}
                    </button>
                    {showModelPicker && models.length > 0 && (
                      <div className="absolute bottom-full right-0 mb-1 bg-white border border-surface-border rounded-xl shadow-lg py-1 z-10 min-w-[140px]">
                        {models.map(m => (
                          <button key={m.name} onClick={() => setShowModelPicker(false)}
                            className="block w-full text-left px-3 py-1.5 text-xs text-surface-text hover:bg-surface-hover transition-colors">{m.name}</button>
                        ))}
                      </div>
                    )}
                  </div>
                  {/* Send button */}
                  <button onClick={sendMessage} disabled={!activeSid || sending || !input.trim()}
                    className="w-9 h-9 rounded-full bg-brand-500 text-white flex items-center justify-center hover:bg-brand-600 transition-colors disabled:opacity-30 disabled:cursor-not-allowed shadow-sm text-sm font-bold">{Icons.send}</button>
                </div>
              </div>

              {/* Quick scenario tags */}
              <div className="flex flex-wrap gap-2 justify-center">
                {SCENARIOS.map(s => (
                  <button key={s.label} onClick={() => fillScenario(s.prompt)}
                    className="px-3 py-1.5 rounded-full border border-surface-border text-xs text-surface-muted hover:text-brand-600 hover:border-brand-300 hover:bg-brand-50 transition-colors">
                    {s.label}
                  </button>
                ))}
              </div>

              {/* Mode switch */}
              <button className="mt-10 text-xs text-surface-dim hover:text-surface-muted transition-colors">切换到经典模式</button>
            </div>
          ) : (
            /* ── Chat messages ── */
            <div className="max-w-3xl mx-auto py-6 space-y-5">
              {messages.map(msg => (
                <div key={msg.id} className={`flex gap-3 animate-slide-up ${msg.role === 'user' ? 'justify-end' : ''}`}>
                  {msg.role === 'assistant' && (
                    <div className="w-8 h-8 rounded-full bg-brand-100 flex items-center justify-center text-brand-600 text-sm shrink-0 mt-0.5">{Icons.agent}</div>
                  )}
                  <div className={`max-w-[85%] rounded-2xl px-5 py-3 ${
                    msg.role === 'user' ? 'bg-brand-500 text-white' : 'bg-surface-hover text-surface-text border border-surface-border'
                  }`}>
                    <div className={`text-sm whitespace-pre-wrap break-words leading-relaxed ${msg.streaming ? 'cursor-blink' : ''}`}>
                      {msg.content || (msg.streaming ? '' : '(空)')}
                    </div>
                    {msg.step && <div className="text-[10px] opacity-50 mt-1">Step {msg.step}</div>}
                  </div>
                </div>
              ))}
              {/* Inline tool cards */}
              {tools.filter(t => !messages.find(m => m.step === t.step)).map(t => (
                <ToolCard key={t.id} tool={t} />
              ))}
              <div ref={chatEndRef} />
            </div>
          )}
        </div>

        {/* Bottom input bar (when chat has started) */}
        {messages.length > 0 && (
          <div className="px-6 py-4 border-t border-surface-border bg-surface-bg shrink-0">
            <div className="max-w-3xl mx-auto relative">
              <textarea value={input} onChange={e => setInput(e.target.value)} onKeyDown={handleKeyDown}
                disabled={!activeSid || sending} rows={2}
                placeholder="继续对话… (Enter 发送)"
                className="w-full bg-surface-hover border border-surface-border rounded-2xl px-4 py-3 text-sm text-surface-text resize-none input-focus-ring disabled:opacity-40 pr-28"
              />
              <div className="absolute bottom-2 right-3 flex items-center gap-2">
                <label className="flex items-center gap-1 cursor-pointer select-none">
                  <span className="text-[10px] text-surface-muted">{Icons.brain}</span>
                  <label className="toggle"><input type="checkbox" checked={thinkingLevel === 'high'} onChange={() => setThinkingLevel(thinkingLevel === 'high' ? 'medium' : 'high')} /><span className="slider" /></label>
                </label>
                <button onClick={sendMessage} disabled={!activeSid || sending || !input.trim()}
                  className="w-8 h-8 rounded-full bg-brand-500 text-white flex items-center justify-center hover:bg-brand-600 transition-colors disabled:opacity-30 disabled:cursor-not-allowed text-sm font-bold">{Icons.send}</button>
              </div>
            </div>
          </div>
        )}
      </main>

      {/* ── Modals ──────────────────────────────────────────────── */}
      {permReq && activeSid && <PermissionModal req={permReq} sid={activeSid} onDone={() => setPermReq(null)} />}
      {askReq && activeSid && <AskUserModal req={askReq} sid={activeSid} onDone={() => setAskReq(null)} />}
    </div>
  )
}
