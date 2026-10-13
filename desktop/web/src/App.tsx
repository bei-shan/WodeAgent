import { useState, useEffect, useRef } from 'react'
import * as api from './api'
import FileBrowser from './FileBrowser'
import { createSessionEventHandler } from './state/sessionEvents'
import { useSessionStream } from './state/useSessionStream'
import { Icons, Svg } from './components/icons'
import { PermissionModal, AskUserModal, ToolCard, NavItem, SessionItem } from './components/ConversationWidgets'
import { SkillsPanel, McpPanel, HooksPanel } from './components/ManagementPanels'
import type { Message, ToolEntry, PermRequest, AskRequest } from './components/types'
import AuthGate from './AuthGate'
import type { SessionInfo, SkillInfo, McpServerCfg, TeamInfo } from './api'

// ═══════════════════════════════════════════════════════════════════════
// Types
// ═══════════════════════════════════════════════════════════════════════


const uid = () => Math.random().toString(36).slice(2, 10)
const GITHUB_URL = 'https://github.com/bei-shan/WodeAgent'

const SCENARIOS = [
  { label: '代码审查', prompt: '请帮我审查以下代码，关注安全性、性能和最佳实践：' },
  { label: '调研报告', prompt: '请针对以下主题进行深度调研，生成一份结构化的报告：' },
  { label: 'Bug 修复', prompt: '以下代码有一个 bug，请帮我定位并修复：' },
  { label: '架构设计', prompt: '请帮我设计以下功能的系统架构：' },
  { label: '写测试', prompt: '请为以下代码编写单元测试：' },
  { label: '代码重构', prompt: '请重构以下代码，提高可读性和可维护性：' },
]

// ═══════════════════════════════════════════════════════════════════════
// Inline SVG icons (zero dependency)
// ═══════════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════════
// Permission Modal
// ═══════════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════════
// Main App
// ═══════════════════════════════════════════════════════════════════════

function AppCore() {
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [activeSid, setActiveSid] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [tools, setTools] = useState<ToolEntry[]>([])
  const [permReq, setPermReq] = useState<PermRequest | null>(null)
  const [askReq, setAskReq] = useState<AskRequest | null>(null)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [generatedFiles, setGeneratedFiles] = useState<{name: string; path: string}[]>([])
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [uploadedFiles, setUploadedFiles] = useState<{name: string; path: string}[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [sidebarWidth, setSidebarWidth] = useState(260)
  const sidebarRef = useRef<HTMLDivElement>(null)

  // Config
  const [agentTeams, setAgentTeams] = useState(false)
  const [planMode, setPlanMode] = useState(false)
  const [thinkingLevel, setThinkingLevel] = useState('medium')
  const [teamsList, setTeamsList] = useState<TeamInfo[]>([])
  const [showNewTeam, setShowNewTeam] = useState(false)
  const [newTeamName, setNewTeamName] = useState('')

  // Sidebar state
  const [sidebarTab, setSidebarTab] = useState<'sessions'|'files'|'skills'|'mcp'|'hooks'>('sessions')
  const [moreOpen, setMoreOpen] = useState(false)
  const [skillsList, setSkillsList] = useState<SkillInfo[]>([])
  const [mcpServers, setMcpServers] = useState<McpServerCfg[]>([])
  const [subagentStatus, setSubagentStatus] = useState('')
  const [selectedModel, setSelectedModel] = useState('')
  const activeSidRef = useRef<string | null>(null)

  // Models
  const [models, setModels] = useState<api.ModelInfo[]>([])
  const [showModelPicker, setShowModelPicker] = useState(false)

  const chatEndRef = useRef<HTMLDivElement>(null)

  // ── Init ────────────────────────────────────────────────────────
  const loadSessions = async () => { try { setSessions(await api.sessions.list()) } catch {} }
  useEffect(() => { loadSessions() }, [])
  useEffect(() => { api.info.models().then(setModels).catch(() => {}) }, [])
  useEffect(() => { api.skills.list().then(setSkillsList).catch(() => {}) }, [])
  useEffect(() => { api.mcp.list().then(setMcpServers).catch(() => {}) }, [])

  const handleEvent = createSessionEventHandler({
    activeSidRef, setSubagentStatus, setMessages, setGeneratedFiles, setTools,
    setPermReq, setAskReq, setSending, setTeamsList, loadSessions, uid,
  })
  const { connected: streamConnected, connect: connectSession, disconnect: disconnectSession } =
    useSessionStream({
      activeSidRef,
      onEvent: handleEvent,
      onBusy: setSending,
      onRestore: messages => setMessages(messages.map(m => ({
        id: uid(), role: m.role, content: m.content,
      }))),
    })
  const selectionVersion = useRef(0)

  const selectSession = async (sid: string, fresh = false) => {
    const version = ++selectionVersion.current
    disconnectSession()
    activeSidRef.current = sid
    const isCurrent = () => activeSidRef.current === sid && selectionVersion.current === version
    setActiveSid(sid); setMessages([]); setTools([]); setPermReq(null); setAskReq(null)
    setGeneratedFiles([]); setSubagentStatus('')
    try {
      await api.sessions.activate(sid)
      const data = await api.sessions.history(sid)
      if (isCurrent()) {
        setMessages(data.messages.map(m => ({ id: uid(), role: m.role, content: m.content })))
        connectSession(sid, fresh)
      }
      const meta = await api.sessions.get(sid)
      if (isCurrent()) setSending(meta.busy)
    } catch (e: any) { alert('打开会话失败：' + e.message) }
    try {
      const cfg = await api.config.get(sid)
      if (isCurrent()) {
        setAgentTeams(cfg.enable_agent_teams); setPlanMode(cfg.plan_mode)
        setThinkingLevel(cfg.thinking_level); setSelectedModel(cfg.model)
      }
    } catch {}
    try {
      const teams = await api.teams.list(sid)
      if (isCurrent()) setTeamsList(teams)
    } catch {}
  }

  const createSession = async () => {
    try { const session = await api.sessions.create(); setSessions(prev => [session, ...prev]); await selectSession(session.id, true) }
    catch (e: any) { alert(e.message) }
  }

  const deleteSession = async (sid: string) => {
    const prevSessions = sessions
    setSessions(prev => prev.filter(s => s.id !== sid))
    if (activeSidRef.current === sid) {
      ++selectionVersion.current
      activeSidRef.current = null
      setActiveSid(null)
      disconnectSession()
    }
    try { await api.sessions.delete(sid) } catch (e: any) {
      setSessions(prevSessions)
      if (activeSidRef.current === null) { activeSidRef.current = sid; setActiveSid(sid); connectSession(sid) }
      alert('删除失败: ' + (e.message || '未知错误'))
    }
  }

  const toggleTeams = async () => {
    const next = !agentTeams; setAgentTeams(next)
    if (activeSid) {
      try { await api.config.update(activeSid, { enable_agent_teams: next }); setTeamsList(await api.teams.list(activeSid)) } catch { setAgentTeams(!next) }
    }
  }

  const createTeam = async () => {
    if (!activeSid || !newTeamName.trim()) return
    try { await api.teams.create(activeSid, newTeamName.trim()); setTeamsList(await api.teams.list(activeSid)); setNewTeamName(''); setShowNewTeam(false) } catch (e: any) { alert(e.message) }
  }

  // ── Events ──────────────────────────────────────────────────────
  useEffect(() => { chatEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, tools])

  // ── Send ────────────────────────────────────────────────────────
  const sendMessage = async () => {
    if (!input.trim() || sending) return
    // Auto-create session if none active
    let sid = activeSidRef.current
    if (!sid) {
      try {
        const session = await api.sessions.create()
        sid = session.id
        setSessions(prev => [session, ...prev])
        activeSidRef.current = sid
        setActiveSid(sid)
        connectSession(sid, true)
      } catch (e: any) { alert('创建会话失败：' + e.message); return }
    }
    // Append @file references for uploaded files
    let content = input.trim()
    if (uploadedFiles.length > 0) {
      const refs = uploadedFiles.map(f => `@${f.path}`).join(' ')
      content = content ? `${content}\n${refs}` : refs
      setUploadedFiles([])
    }
    setInput(''); setSending(true); setSubagentStatus('')
    setMessages(prev => [...prev, { id: uid(), role: 'user', content }])
    const aid = uid(); setMessages(prev => [...prev, { id: aid, role: 'assistant', content: '', streaming: true }])
    try { await api.sessions.send(sid, content) } catch (e: any) {
      setMessages(prev => prev.map(m => m.id === aid ? { ...m, content: `${e.message}`, streaming: false } : m)); setSending(false)
    }
  }

  const fillScenario = (prompt: string) => { setInput(prompt) }
  const chooseModel = async (profile: api.ModelInfo) => {
    try {
      let sid = activeSidRef.current
      if (!sid) {
        const session = await api.sessions.create()
        sid = session.id; setSessions(prev => [session, ...prev])
        await selectSession(sid, true)
      }
      await api.config.update(sid, { model_profile: profile.name })
      setSelectedModel(profile.model)
      setShowModelPicker(false)
    } catch (e: any) { alert('切换模型失败：' + e.message) }
  }
  const interrupt = async () => {
    if (!activeSidRef.current) return
    try { await api.sessions.interrupt(activeSidRef.current) }
    catch (e: any) { alert('中断失败：' + e.message) }
  }
  const handleKeyDown = (e: React.KeyboardEvent) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() } }

  // File upload
  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files; if (!files || !activeSid) return
    for (let i = 0; i < files.length; i++) {
      const form = new FormData(); form.append('file', files[i])
      try {
        const res = await fetch(`/api/sessions/${activeSid}/upload`, { method: 'POST', body: form })
        if (!res.ok) throw new Error('HTTP ' + res.status)
        const data = await res.json()
        setUploadedFiles(prev => [...prev, { name: data.name, path: data.path }])
      } catch (e: any) { alert('上传失败：' + e.message) }
    }
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  // ═══════════════════════════════════════════════════════════════════
  // Render
  // ═══════════════════════════════════════════════════════════════════

  const navItems = [
    { key: 'sessions' as const, icon: Icons.chat, label: '会话' },
    { key: 'files' as const, icon: Icons.folder, label: '文件' },
    { key: 'skills' as const, icon: Icons.skill, label: '技能' },
    { key: 'mcp' as const, icon: Icons.settings, label: 'MCP' },
    { key: 'hooks' as const, icon: Icons.clock, label: 'Hooks' },
  ]

  return (
    <div className="fixed inset-0 flex bg-[#F9FAFB] text-gray-800 overflow-hidden">
      {/* ═══════════════ LEFT SIDEBAR ═══════════════ */}
      <aside ref={sidebarRef} className={`relative flex flex-col border-r border-gray-100 bg-white shrink-0 transition-all duration-200 overflow-hidden ${sidebarCollapsed ? 'w-0 border-r-0' : ''}`}
        style={{ width: sidebarCollapsed ? 0 : sidebarWidth }}>
        <div className="flex-1 flex flex-col min-w-[220px]">
        {/* Logo */}
        <div className="px-4 pt-4 pb-2">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-blue-500 flex items-center justify-center">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
            </div>
            <span className="font-bold text-gray-800 text-lg">Ridge</span>
          </div>
        </div>

        {/* New task button */}
        <div className="px-4 pb-3">
          <button onClick={createSession}
            className="w-full py-2.5 rounded-xl bg-blue-500 text-white text-sm font-semibold hover:bg-blue-600 transition-colors">
            + 新建任务
          </button>
        </div>

        {/* Agent teams status (toggle moved below input) */}
        <div className="px-4 pb-3">
          {agentTeams && (
            <div className="mt-2 space-y-0.5">
              {teamsList.map(t => (
                <div key={t.name} className="flex items-center justify-between px-2 py-1.5 text-xs text-gray-600 hover:bg-gray-50 rounded-lg transition-colors">
                  <div className="flex items-center gap-2">
                    <Svg d={Icons.teams} size={13} />
                    <span className="font-medium">{t.name}</span>
                  </div>
                  <span className="text-gray-400 text-[10px]">{t.running}跑 {t.succeeded}成</span>
                </div>
              ))}
              <button onClick={() => setShowNewTeam(true)}
                className="w-full text-xs text-blue-500 hover:text-blue-600 py-1 text-left font-medium flex items-center gap-1">
                <Svg d={Icons.plus} size={11} /> 创建团队
              </button>
              {showNewTeam && (
                <div className="flex gap-1 mt-1">
                  <input value={newTeamName} onChange={e => setNewTeamName(e.target.value)}
                    placeholder="团队名称" className="flex-1 bg-gray-100 border border-gray-200 rounded-lg px-2 py-1.5 text-xs input-focus-ring" />
                  <button onClick={createTeam} className="px-3 py-1.5 bg-blue-500 text-white rounded-lg text-xs font-medium hover:bg-blue-600 whitespace-nowrap">创建</button>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Navigation menu */}
        <nav className="flex-1 overflow-y-auto px-3 space-y-0.5">
          {navItems.map(item => (
            <NavItem key={item.key} icon={item.icon} label={item.label}
              active={sidebarTab === item.key}
              onClick={() => setSidebarTab(item.key)} />
          ))}

          {/* Collapsible "More" group */}
          <div className="pt-2">
            <button onClick={() => setMoreOpen(!moreOpen)}
              className="w-full flex items-center justify-between px-3 py-2 rounded-xl text-sm text-gray-500 hover:bg-gray-50 transition-colors">
              <div className="flex items-center gap-3">
                <Svg d={Icons.chevron} size={14} className={`transition-transform duration-200 ${moreOpen ? 'rotate-180' : ''}`} />
                <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">更多</span>
              </div>
            </button>
            {moreOpen && (
              <div className="ml-2 mt-0.5 animate-slide-up">
                <div className="px-3 py-2 rounded-xl text-xs text-gray-400">
                  更多功能开发中…
                </div>
              </div>
            )}
          </div>

          {/* Recent tasks group */}
          <div className="pt-4">
            <div className="px-3 pb-1.5 text-xs font-medium text-gray-400 uppercase tracking-wide">最近</div>
            <div className="space-y-0.5">
              {sessions.slice(0, 15).map(s => (
                <SessionItem key={s.id} session={s} active={s.id === activeSid}
                  onSelect={() => selectSession(s.id)}
                  onDelete={() => setConfirmDelete(s.id)}
                  onRename={async (title) => { try { await api.sessions.rename(s.id, title); loadSessions() } catch {} }}
                  onTogglePin={async () => { try { await api.sessions.togglePin(s.id); loadSessions() } catch {} }}
                />
              ))}
              {sessions.length === 0 && (
                <div className="px-3 py-4 text-center">
                  <div className="text-xs text-gray-400">暂无任务记录</div>
                  <div className="text-[11px] text-gray-300 mt-1">经典版对话记录请在用户菜单中切换查看</div>
                </div>
              )}
            </div>
          </div>
        </nav>

        {/* Bottom: download + user */}
        <div className="border-t border-gray-100 px-4 py-3 space-y-2">
          <a href={GITHUB_URL} target="_blank" rel="noopener"
            className="w-full flex items-center gap-2.5 px-2 py-2 text-sm text-gray-500 hover:text-gray-700 hover:bg-gray-50 rounded-lg transition-colors no-underline">
            <Svg d={Icons.download} size={16} />
            <span>下载 CLI</span>
          </a>
          <div className="flex items-center gap-2.5 px-2">
            <div className="w-6 h-6 rounded-full bg-gray-200 flex items-center justify-center">
              <Svg d={Icons.user} size={12} />
            </div>
            <span className="text-sm text-gray-600">sam</span>
          </div>
        </div>
        </div>{/* close inner flex wrapper */}
        {/* Resize handle */}
        {!sidebarCollapsed && (
          <div className="absolute right-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-blue-300 transition-colors z-10"
            onMouseDown={e => {
              e.preventDefault()
              const startX = e.clientX
              const startW = sidebarWidth
              const onMove = (ev: MouseEvent) => setSidebarWidth(Math.max(180, Math.min(500, startW + ev.clientX - startX)))
              const onUp = () => { document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp) }
              document.addEventListener('mousemove', onMove); document.addEventListener('mouseup', onUp)
            }}
          />
        )}
      </aside>

      {/* ═══════════════ RIGHT MAIN ═══════════════ */}
      <main className="flex-1 flex flex-col min-w-0 bg-[#F9FAFB]">
        {/* Top bar */}
        <header className="px-4 py-3 flex items-center justify-between shrink-0 gap-3">
          <button onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
            className="w-7 h-7 rounded-lg hover:bg-gray-100 flex items-center justify-center text-gray-400 hover:text-gray-600 transition-colors">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              {sidebarCollapsed
                ? <><path d="M4 6h16M4 12h16M4 18h16"/></>
                : <><path d="M3 12h18M3 6h12m-12 12h12"/></>}
            </svg>
          </button>
          <div className="flex items-center gap-3">
          {activeSid && <span className={`text-xs mr-2 ${streamConnected ? 'text-emerald-600' : 'text-amber-600'}`}>
            {streamConnected ? '实时连接正常' : '连接中 / 自动重连…'}
          </span>}
          {sending && <button onClick={interrupt} className="text-xs text-red-600 border rounded-lg px-3 py-1.5">停止生成</button>}
            <a href={GITHUB_URL} target="_blank" rel="noopener"
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors no-underline">
              <Svg d={Icons.doc} size={16} />
              <span>文档</span>
            </a>
            <a href={GITHUB_URL} target="_blank" rel="noopener"
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors no-underline">
              <Svg d={Icons.download} size={16} />
              <span>下载 CLI</span>
            </a>
          </div>
        </header>

        {/* ── Skills management panel ── */}
        {sidebarTab === 'skills' && (
          <SkillsPanel skills={skillsList} onRefresh={async () => { try { setSkillsList(await api.skills.list()) } catch {} }} />
        )}

        {/* ── MCP management panel ── */}
        {sidebarTab === 'mcp' && (
          <McpPanel servers={mcpServers} onRefresh={async () => { try { setMcpServers(await api.mcp.list()) } catch {} }} />
        )}

        {/* ── Hooks management panel ── */}
        {sidebarTab === 'hooks' && <HooksPanel />}
        {sidebarTab === 'files' && <FileBrowser sessionId={activeSid} />}

        {/* ── Chat / empty state ── */}
        {sidebarTab === 'sessions' && (messages.length === 0 && !sending ? (
          <div className="flex-1 flex flex-col items-center justify-center px-8">
            <h1 className="text-[28px] font-bold text-gray-800 mb-12 tracking-tight">Hi, 我是 Ridge</h1>

            {/* Input card — white, shadow, accessories bar above textarea */}
            <div className="w-full max-w-4xl bg-white rounded-2xl border border-gray-200 shadow-sm">
              {/* Accessories bar — left/right groups, above textarea */}
              <div className="flex items-center justify-between px-5 pt-4 pb-2">
                <div className="flex items-center gap-3">
                  {/* Model selector */}
                  <div className="relative">
                    <button onClick={() => setShowModelPicker(!showModelPicker)}
                      className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100 rounded-lg transition-colors">
                      <Svg d={Icons.brain} size={16} />
                      <span>{selectedModel || models[0]?.model || 'Model'}</span>
                      <Svg d={Icons.chevron} size={12} className="text-gray-400" />
                    </button>
                    {showModelPicker && models.length > 0 && (
                      <div className="absolute top-full left-0 mt-1 bg-white border border-gray-200 rounded-xl shadow-lg py-1 z-10 min-w-[180px]">
                        {models.map(m => (
                          <button key={m.name} onClick={() => chooseModel(m)}
                            className="block w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-50 transition-colors">{m.model}</button>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => fileInputRef.current?.click()} disabled={!activeSid} title={activeSid ? '上传文件' : '请先创建会话'}
                    className="w-8 h-8 rounded-full bg-gray-100 flex items-center justify-center text-gray-400 hover:bg-gray-200 hover:text-blue-500 transition-colors">
                    <Svg d={Icons.plus} size={16} />
                  </button>
                  <input ref={fileInputRef} type="file" multiple onChange={handleFileUpload} className="hidden" />
                </div>
              </div>
              {/* Textarea */}
              <textarea value={input} onChange={e => setInput(e.target.value)} onKeyDown={handleKeyDown}
                disabled={sending} rows={2}
                placeholder="问任何问题"
                className="w-full bg-transparent px-5 text-gray-800 resize-none text-[15px] leading-relaxed disabled:opacity-40 placeholder:text-gray-400 border-0 focus:outline-none"
              />
              {/* Bottom bar: toggles left, send right */}
              <div className="flex items-center justify-between px-5 pb-4">
                <div className="flex items-center gap-3">
                  <label className="flex items-center gap-1.5 cursor-pointer select-none">
                    <span className="text-[11px] text-gray-400" title="模型推理参数尚未接入，暂不可用">深度思考（待接入）</span>
                    <label className="toggle"><input type="checkbox" disabled title="功能暂未接入" /><span className="slider" /></label>
                  </label>
                  <label className="flex items-center gap-1.5 cursor-pointer select-none">
                    <span className="text-[11px] text-gray-400">Agent 团队</span>
                    <label className="toggle"><input type="checkbox" checked={agentTeams} onChange={toggleTeams} disabled={planMode} /><span className="slider" /></label>
                  </label>
                  <label className="flex items-center gap-1.5 cursor-pointer select-none">
                    <span className="text-[11px] text-gray-400">Plan</span>
                    <label className="toggle"><input type="checkbox" checked={planMode} onChange={async () => {
                      const next = !planMode; setPlanMode(next);
                      if (activeSid) { try { await api.config.update(activeSid, { plan_mode: next }) } catch { setPlanMode(!next) } }
                    }} /><span className="slider" /></label>
                  </label>
                </div>
                <button onClick={sendMessage} disabled={sending || !input.trim()}
                  className="w-9 h-9 rounded-full bg-gray-200 text-gray-400 flex items-center justify-center hover:bg-blue-500 hover:text-white transition-all disabled:opacity-30 disabled:cursor-not-allowed">
                  <Svg d={Icons.send} size={17} />
                </button>
              </div>
            </div>

            {/* Uploaded files preview */}
            {uploadedFiles.length > 0 && (
              <div className="flex flex-wrap gap-2 justify-center mt-3">
                {uploadedFiles.map((f, i) => (
                  <span key={i} className="inline-flex items-center gap-1 px-3 py-1 rounded-full bg-blue-50 text-blue-600 text-xs">
                    <Svg d={Icons.file} size={12} /> {f.name}
                    <button onClick={() => setUploadedFiles(prev => prev.filter(x => x.path !== f.path))}
                      className="ml-1 text-blue-400 hover:text-red-500">×</button>
                  </span>
                ))}
              </div>
            )}

            {/* Quick tags */}
            <div className="flex flex-wrap gap-2 justify-center mt-5">
              {SCENARIOS.map(s => (
                <button key={s.label} onClick={() => fillScenario(s.prompt)}
                  className="px-3.5 py-1.5 rounded-full border border-gray-200 text-xs text-gray-500 hover:text-blue-600 hover:border-blue-300 hover:bg-blue-50/50 transition-colors bg-white">
                  {s.label}
                </button>
              ))}
            </div>

          </div>
        ) : (
          <div className="flex-1 overflow-y-auto bg-[#F9FAFB]">
            <div className="max-w-4xl mx-auto py-6 px-6 space-y-5">
              {messages.map(msg => (
                <div key={msg.id} className={`flex gap-3 animate-slide-up ${msg.role === 'user' ? 'justify-end' : ''}`}>
                  {msg.role === 'assistant' && (
                    <div className="w-8 h-8 rounded-full bg-blue-50 flex items-center justify-center text-blue-500 shrink-0 mt-0.5">
                      <Svg d={Icons.chat} size={16} />
                    </div>
                  )}
                  <div className={`max-w-[85%] rounded-2xl px-5 py-3 ${
                    msg.role === 'user' ? 'bg-blue-500 text-white' : 'bg-gray-50 text-gray-800 border border-gray-100'
                  }`}>
                    <div className={`text-sm whitespace-pre-wrap break-words leading-relaxed ${msg.streaming ? 'cursor-blink' : ''}`}>
                      {msg.content || (msg.streaming ? '' : '(空)')}
                    </div>
                    {msg.step && <div className="text-[10px] opacity-40 mt-1">Step {msg.step}</div>}
                  </div>
                </div>
              ))}
              {tools.length > 0 && (
                <div className="space-y-2" aria-label="工具执行记录">
                  <h3 className="text-xs text-gray-500 ml-10">工具执行记录</h3>
                  {tools.map(tool => <ToolCard key={tool.id} tool={tool} sid={activeSid} />)}
                </div>
              )}
              {subagentStatus && <div className="ml-10 text-xs text-blue-600">{subagentStatus}</div>}
              {/* Generated files download */}
              {generatedFiles.length > 0 && (
                <div className="border-t border-gray-100 pt-3 mt-2">
                  <div className="text-[11px] text-gray-400 mb-1.5">生成的文件</div>
                  <div className="flex flex-wrap gap-2">
                    {generatedFiles.map((f, i) => (
                      <a key={i} href={`/api/sessions/${activeSid}/files/download?path=${encodeURIComponent(f.path)}`}
                        download className="inline-flex items-center gap-1 px-3 py-1.5 rounded-full bg-blue-50 text-blue-600 text-xs font-medium hover:bg-blue-100 transition-colors">
                        <Svg d={Icons.download} size={12} /> {f.name}
                      </a>
                    ))}
                  </div>
                </div>
              )}
              <div ref={chatEndRef} />
            </div>
          </div>
        ))}

        {/* Bottom input (when chat started) */}
        {sidebarTab === 'sessions' && messages.length > 0 && (
          <div className="px-6 py-4 shrink-0">
            <div className="max-w-4xl mx-auto bg-white rounded-2xl border border-gray-200 shadow-sm">
              <div className="flex items-center justify-between px-5 pt-4 pb-2">
                <div className="flex items-center gap-3">
                  <button onClick={() => fileInputRef.current?.click()}
                    className="w-7 h-7 rounded-full bg-gray-100 flex items-center justify-center text-gray-400 hover:bg-gray-200 hover:text-blue-500 transition-colors">
                    <Svg d={Icons.plus} size={14} />
                  </button>
                  <label className="flex items-center gap-2 cursor-pointer select-none">
                    <span className="text-sm text-gray-500" title="模型推理参数尚未接入">深度思考（待接入）</span>
                    <label className="toggle"><input type="checkbox" disabled title="功能暂未接入" /><span className="slider" /></label>
                  </label>
                </div>
                <button onClick={sendMessage} disabled={sending || !input.trim()}
                  className="w-8 h-8 rounded-full bg-gray-200 text-gray-400 flex items-center justify-center hover:bg-blue-500 hover:text-white transition-all disabled:opacity-30 disabled:cursor-not-allowed">
                  <Svg d={Icons.send} size={15} />
                </button>
              </div>
              {uploadedFiles.length > 0 && (
                <div className="flex flex-wrap gap-1.5 px-5 pb-1">
                  {uploadedFiles.map((f, i) => (
                    <span key={i} className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full bg-blue-50 text-blue-600 text-[11px]">
                      <Svg d={Icons.file} size={11} /> {f.name}
                      <button onClick={() => setUploadedFiles(prev => prev.filter(x => x.path !== f.path))}
                        className="ml-0.5 text-blue-400 hover:text-red-500">×</button>
                    </span>
                  ))}
                </div>
              )}
              <textarea value={input} onChange={e => setInput(e.target.value)} onKeyDown={handleKeyDown}
                disabled={sending} rows={2}
                placeholder="继续对话… (Enter 发送)"
                className="w-full bg-transparent px-5 pb-5 text-sm text-gray-800 resize-none disabled:opacity-40 placeholder:text-gray-400 border-0 focus:outline-none"
              />
            </div>
          </div>
        )}
      </main>

      {/* Confirm delete modal */}
      {confirmDelete && (
        <div className="fixed inset-0 bg-black/25 flex items-center justify-center z-50 animate-fade-in" onClick={() => setConfirmDelete(null)}>
          <div className="bg-white rounded-2xl p-6 max-w-sm w-full mx-4 shadow-xl border border-gray-100" onClick={e => e.stopPropagation()}>
            <div className="text-center mb-4">
              <div className="w-10 h-10 rounded-full bg-red-50 flex items-center justify-center mx-auto mb-3 text-red-500">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2M10 11v6M14 11v6"/></svg>
              </div>
              <h3 className="text-lg font-semibold text-gray-800">删除对话</h3>
              <p className="text-sm text-gray-500 mt-1">删除后不可恢复，确定要删除此对话吗？</p>
            </div>
            <div className="flex gap-3">
              <button onClick={() => setConfirmDelete(null)}
                className="flex-1 py-2.5 rounded-xl border border-gray-200 text-gray-600 hover:bg-gray-50 transition-colors text-sm font-medium">取消</button>
              <button onClick={() => { const sid = confirmDelete; setConfirmDelete(null); deleteSession(sid) }}
                className="flex-1 py-2.5 rounded-xl bg-red-500 text-white hover:bg-red-600 transition-colors text-sm font-medium">删除</button>
            </div>
          </div>
        </div>
      )}

      {/* Modals */}
      {permReq && activeSid && <PermissionModal req={permReq} sid={activeSid} onDone={() => setPermReq(null)} />}
      {askReq && activeSid && <AskUserModal req={askReq} sid={activeSid} onDone={() => setAskReq(null)} />}
    </div>
  )
}

export default function App() {
  return <AuthGate><AppCore /></AuthGate>
}
