import { useState, useEffect, useRef } from 'react'
import * as api from './api'
import FileBrowser from './FileBrowser'
import ChatComposer from './components/ChatComposer'
import MessageContent from './components/MessageContent'
import TracePanel from './components/TracePanel'
import PluginsPanel from './components/PluginsPanel'
import { createSessionEventHandler } from './state/sessionEvents'
import { useSessionStream } from './state/useSessionStream'
import { Icons, Svg } from './components/icons'
import { PermissionModal, AskUserModal, ToolCard, SessionItem } from './components/ConversationWidgets'
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
  const [sidebarTab, setSidebarTab] = useState<'sessions'|'files'|'skills'|'plugins'|'mcp'|'hooks'|'trace'>('sessions')
  const [sessionSearch, setSessionSearch] = useState('')
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
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
    try {
      const session = await api.sessions.create()
      setSessions(prev => [session, ...prev])
      setSidebarTab('sessions')
      setMobileNavOpen(false)
      await selectSession(session.id, true)
    } catch (e: any) { alert(e.message) }
  }

  const ensureSession = async (): Promise<string> => {
    if (activeSidRef.current) return activeSidRef.current
    const session = await api.sessions.create()
    setSessions(prev => [session, ...prev])
    await selectSession(session.id, true)
    return session.id
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
    if (planMode) return
    const next = !agentTeams
    try {
      const sid = await ensureSession()
      await api.config.update(sid, { enable_agent_teams: next })
      setAgentTeams(next)
      setTeamsList(await api.teams.list(sid))
    } catch (e: any) { alert('更新 Agent 团队失败：' + e.message) }
  }

  const togglePlanMode = async () => {
    const next = !planMode
    try {
      const sid = await ensureSession()
      await api.config.update(sid, { plan_mode: next })
      setPlanMode(next)
    } catch (e: any) { alert('切换计划模式失败：' + e.message) }
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

  // File upload
  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files?.length) return
    let sid: string
    try { sid = await ensureSession() }
    catch (error: any) { alert('创建会话失败：' + error.message); e.target.value = ''; return }
    for (const file of Array.from(files)) {
      const form = new FormData()
      form.append('file', file)
      try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sid)}/upload`, { method: 'POST', body: form })
        if (!res.ok) throw new Error('HTTP ' + res.status)
        const data = await res.json()
        setUploadedFiles(prev => [...prev, { name: data.name, path: data.path }])
      } catch (error: any) { alert('上传失败：' + error.message) }
    }
    e.target.value = ''
  }

  // ═══════════════════════════════════════════════════════════════════
  // Render
  // ═══════════════════════════════════════════════════════════════════

  const navItems = [
    { key: 'sessions' as const, icon: Icons.chat, label: '会话' },
    { key: 'files' as const, icon: Icons.folder, label: '文件' },
    { key: 'skills' as const, icon: Icons.skill, label: '技能' },
    { key: 'plugins' as const, icon: Icons.settings, label: '插件' },
    { key: 'mcp' as const, icon: Icons.settings, label: 'MCP' },
    { key: 'hooks' as const, icon: Icons.clock, label: 'Hooks' },
    { key: 'trace' as const, icon: Icons.brain, label: 'Trace' },
  ]

  const filteredSessions = sessions.filter(session =>
    (session.title || session.id).toLocaleLowerCase().includes(sessionSearch.trim().toLocaleLowerCase())
  )
  const activeSession = sessions.find(session => session.id === activeSid)
  const activeTitle = sidebarTab === 'sessions'
    ? (activeSession?.title || 'Ridge')
    : navItems.find(item => item.key === sidebarTab)?.label || 'Ridge'

  const composer = (
    <ChatComposer
      input={input} onInput={setInput} onSend={sendMessage} sending={sending}
      models={models} selectedModel={selectedModel}
      modelPickerOpen={showModelPicker} onToggleModelPicker={() => setShowModelPicker(v => !v)}
      onChooseModel={chooseModel}
      uploadedFiles={uploadedFiles}
      onRemoveFile={path => setUploadedFiles(prev => prev.filter(file => file.path !== path))}
      onFileUpload={handleFileUpload}
      planMode={planMode} onTogglePlanMode={togglePlanMode}
      agentTeams={agentTeams} onToggleTeams={toggleTeams}
    />
  )

  return (
    <div className="app-shell">
      {mobileNavOpen && (
        <button type="button" className="sidebar-backdrop" aria-label="关闭侧边栏"
          onClick={() => setMobileNavOpen(false)} />
      )}

      <aside ref={sidebarRef} aria-label="主导航"
        className={`app-sidebar ${mobileNavOpen ? 'mobile-open' : ''}`}
        style={{ width: sidebarCollapsed ? 0 : sidebarWidth }}>
        <div className="sidebar-interior">
          <div className="sidebar-top">
            <div className="sidebar-brand">
              <span className="sidebar-mark" aria-hidden="true">✳</span>
              <span>Ridge</span>
              <button type="button" className="mobile-only ml-auto topbar-button"
                onClick={() => setMobileNavOpen(false)} aria-label="关闭导航">
                <Svg d={Icons.xmark} size={18} />
              </button>
            </div>
            <button type="button" className="sidebar-main-button" onClick={createSession}>
              <Svg d={Icons.plus} size={18} />
              <span>新建任务</span>
            </button>
            <div className="flex items-center gap-1 mt-2 text-gray-400 px-1">
              <Svg d={Icons.search} size={16} />
              <input type="search" className="sidebar-search" placeholder="搜索对话"
                aria-label="搜索对话" value={sessionSearch}
                onChange={event => setSessionSearch(event.target.value)} />
            </div>
          </div>

          <nav className="flex-1 overflow-y-auto px-3 pb-4" aria-label="会话和工作台">
            {filteredSessions.some(session => session.pinned) && (
              <section aria-label="置顶对话">
                <div className="sidebar-section-label">置顶</div>
                {filteredSessions.filter(session => session.pinned).map(session => (
                  <SessionItem key={session.id} session={session} active={session.id === activeSid && sidebarTab === 'sessions'}
                    onSelect={() => { setSidebarTab('sessions'); setMobileNavOpen(false); selectSession(session.id) }}
                    onDelete={() => setConfirmDelete(session.id)}
                    onRename={async title => { try { await api.sessions.rename(session.id, title); loadSessions() } catch {} }}
                    onTogglePin={async () => { try { await api.sessions.togglePin(session.id); loadSessions() } catch {} }}
                  />
                ))}
              </section>
            )}
            <section aria-label="最近对话">
              <div className="sidebar-section-label">最近的对话</div>
              {filteredSessions.filter(session => !session.pinned).map(session => (
                <SessionItem key={session.id} session={session} active={session.id === activeSid && sidebarTab === 'sessions'}
                  onSelect={() => { setSidebarTab('sessions'); setMobileNavOpen(false); selectSession(session.id) }}
                  onDelete={() => setConfirmDelete(session.id)}
                  onRename={async title => { try { await api.sessions.rename(session.id, title); loadSessions() } catch {} }}
                  onTogglePin={async () => { try { await api.sessions.togglePin(session.id); loadSessions() } catch {} }}
                />
              ))}
              {filteredSessions.length === 0 && (
                <p className="px-3 py-4 text-xs text-gray-400">
                  {sessionSearch ? '没有匹配的对话' : '还没有对话，创建一个新任务吧'}
                </p>
              )}
            </section>
            <section aria-label="工作台">
              <div className="sidebar-section-label">工作台</div>
              {navItems.filter(item => item.key !== 'sessions').map(item => (
                <button key={item.key} type="button"
                  className={`sidebar-nav-button ${sidebarTab === item.key ? 'active' : ''}`}
                  onClick={() => { setSidebarTab(item.key); setMobileNavOpen(false); setShowModelPicker(false) }}>
                  <Svg d={item.icon} size={17} /> <span>{item.label}</span>
                </button>
              ))}
            </section>
            {agentTeams && (
              <section aria-label="Agent 团队" className="px-1">
                <div className="sidebar-section-label">Agent 团队</div>
                {teamsList.map(team => (
                  <div key={team.name} className="flex justify-between gap-2 px-3 py-2 text-xs text-gray-600">
                    <span className="truncate">{team.name}</span>
                    <span className="shrink-0 text-gray-400">{team.running} 运行 / {team.succeeded} 完成</span>
                  </div>
                ))}
                {showNewTeam ? (
                  <div className="flex gap-1 px-2">
                    <input aria-label="团队名称" value={newTeamName}
                      onChange={event => setNewTeamName(event.target.value)}
                      onKeyDown={event => { if (event.key === 'Enter') createTeam() }}
                      placeholder="团队名称" className="min-w-0 flex-1 rounded-lg border border-gray-200 bg-white px-2 py-1 text-xs" />
                    <button type="button" onClick={createTeam} className="rounded-lg bg-gray-800 px-2 py-1 text-xs text-white">创建</button>
                  </div>
                ) : (
                  <button type="button" onClick={() => setShowNewTeam(true)}
                    className="sidebar-nav-button"><Svg d={Icons.plus} size={14} /> 创建团队</button>
                )}
              </section>
            )}
          </nav>
          <div className="sidebar-bottom">
            <a href={GITHUB_URL} target="_blank" rel="noopener noreferrer"
              className="sidebar-nav-button no-underline">
              <Svg d={Icons.doc} size={17} /> <span>GitHub 源码与文档</span>
            </a>
            <div className="flex items-center gap-2 px-3 py-2 text-xs text-gray-500">
              <span className="h-7 w-7 rounded-full bg-gray-200 flex items-center justify-center">
                <Svg d={Icons.user} size={14} />
              </span>
              <span>本地工作区</span>
            </div>
          </div>
        </div>
        {!sidebarCollapsed && (
          <div className="sidebar-grip desktop-only" role="separator" aria-label="调整侧边栏宽度"
            onMouseDown={event => {
              event.preventDefault()
              const startX = event.clientX
              const startW = sidebarWidth
              const onMove = (e: MouseEvent) => setSidebarWidth(Math.max(224, Math.min(420, startW + e.clientX - startX)))
              const onUp = () => {
                document.removeEventListener('mousemove', onMove)
                document.removeEventListener('mouseup', onUp)
              }
              document.addEventListener('mousemove', onMove)
              document.addEventListener('mouseup', onUp)
            }}
          />
        )}
      </aside>

      <main className="app-main">
        <header className="topbar">
          <div className="flex items-center gap-2 min-w-0">
            <button type="button" className="topbar-button desktop-only"
              onClick={() => setSidebarCollapsed(value => !value)}
              aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}>
              <Svg d={Icons.chat} size={18} />
            </button>
            <button type="button" className="topbar-button mobile-only"
              onClick={() => setMobileNavOpen(true)} aria-label="打开侧边栏">
              <Svg d={Icons.chat} size={18} />
            </button>
            <h2 className="topbar-title">{activeTitle}</h2>
          </div>
          <div className="flex items-center gap-2 sm:gap-3 shrink-0">
            {activeSid && (
              <div className={`connection-state ${streamConnected ? '' : 'offline'}`} role="status">
                <span className="connection-dot" aria-hidden="true" />
                {streamConnected ? '实时连接正常' : '连接中 / 自动重连…'}
              </div>
            )}
            {sending && (
              <button type="button" onClick={interrupt}
                className="rounded-full border border-red-200 px-3 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50">停止生成</button>
            )}
            <a href={GITHUB_URL} target="_blank" rel="noopener noreferrer"
              className="topbar-button" aria-label="查看项目源码" title="GitHub">
              <Svg d={Icons.doc} size={18} />
            </a>
          </div>
        </header>

        {sidebarTab === 'skills' && (
          <SkillsPanel skills={skillsList} onRefresh={async () => { try { setSkillsList(await api.skills.list()) } catch {} }} />
        )}
        {sidebarTab === 'plugins' && <PluginsPanel />}
        {sidebarTab === 'mcp' && (
          <McpPanel servers={mcpServers} onRefresh={async () => { try { setMcpServers(await api.mcp.list()) } catch {} }} />
        )}
        {sidebarTab === 'hooks' && <HooksPanel />}
        {sidebarTab === 'files' && <FileBrowser sessionId={activeSid} />}
        {sidebarTab === 'trace' && <TracePanel sessionId={activeSid} />}

        {sidebarTab === 'sessions' && (messages.length === 0 && !sending ? (
          <div className="empty-chat">
            <div className="empty-chat-content">
              <h1>Hi, 我是 Ridge</h1>
              {composer}
              <div className="prompt-shortcuts" aria-label="快捷任务">
                {SCENARIOS.map(scenario => (
                  <button key={scenario.label} type="button" className="prompt-shortcut"
                    onClick={() => fillScenario(scenario.prompt)}>
                    {scenario.label}
                  </button>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <div className="conversation-area" role="log" aria-label="对话消息" aria-live="polite">
            <div className="message-stream">
              {messages.map(message => (
                <div key={message.id} className={`chat-message ${message.role === 'user' ? 'user' : 'assistant'} animate-slide-up`}>
                  <div className={message.role === 'user' ? 'chat-message-user-body' : 'chat-message-assistant-body'}>
                    <MessageContent content={message.content || (message.streaming ? '' : '(空)')}
                      streaming={message.streaming} />
                    {message.step && <div className="mt-1 text-xs text-gray-400">Step {message.step}</div>}
                    {message.role === 'assistant' && !message.streaming && message.content && (
                      <div className="message-actions">
                        <button type="button" aria-label="复制回答"
                          onClick={() => navigator.clipboard?.writeText(message.content)}>
                          <Svg d={Icons.doc} size={13} /> 复制
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
              {tools.length > 0 && (
                <section className="message-tools" aria-label="工具执行记录">
                  <h3 className="text-xs">工具执行记录</h3>
                  <div className="space-y-2">
                    {tools.map(tool => <ToolCard key={tool.id} tool={tool} sid={activeSid} />)}
                  </div>
                </section>
              )}
              {subagentStatus && <p className="text-xs text-blue-600">{subagentStatus}</p>}
              {generatedFiles.length > 0 && (
                <section aria-label="生成的文件" className="rounded-xl border border-gray-100 p-3">
                  <h3 className="mb-2 text-xs font-medium text-gray-500">生成的文件</h3>
                  <div className="flex flex-wrap gap-2">
                    {generatedFiles.map(file => (
                      <a key={file.path} href={`/api/sessions/${activeSid}/files/download?path=${encodeURIComponent(file.path)}`}
                        download className="inline-flex items-center gap-1 rounded-full border border-gray-200 bg-gray-50 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-100">
                        <Svg d={Icons.download} size={12} /> {file.name}
                      </a>
                    ))}
                  </div>
                </section>
              )}
              <div ref={chatEndRef} />
            </div>
          </div>
        ))}

        {sidebarTab === 'sessions' && (messages.length > 0 || sending) && (
          <div className="chat-footer">
            <div className="chat-footer-inner">
              {composer}
              <p className="chat-disclaimer">Ridge 可能出错，请核对重要代码和执行结果。</p>
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
