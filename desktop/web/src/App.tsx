import { useState, useEffect, useRef } from 'react'
import * as api from './api'
import type { SessionInfo, AgentEvent, FileEntry, SkillInfo, McpServerCfg, TeamInfo } from './api'

// ═══════════════════════════════════════════════════════════════════════
// Types
// ═══════════════════════════════════════════════════════════════════════

interface Message { id: string; role: 'user' | 'assistant'; content: string; step?: number; streaming?: boolean }
interface ToolEntry { id: string; name: string; input: Record<string, any>; status: 'running' | 'success' | 'error'; output?: string; filePath?: string; step: number }
interface PermRequest { requestId: string; tool: string; path: string; action: string }
interface AskRequest { requestId: string; prompt: string }

const uid = () => Math.random().toString(36).slice(2, 10)
const GITHUB_URL = 'https://github.com/bei-shan/MyCodeAgent'

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

const Svg = ({ d, size = 16, className = '' }: { d: string; size?: number; className?: string }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className={className}>
    <path d={d} />
  </svg>
)

const Icons = {
  search:   'M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z',
  skill:    'M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z',
  clock:    'M12 6v6l4 2m6-2a10 10 0 11-20 0 10 10 0 0120 0z',
  folder:   'M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z',
  teams:    'M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z',
  download: 'M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z',
  settings: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z',
  plus:     'M12 5v14m-7-7h14',
  send:     'M12 19V5m0 0l-7 7m7-7l7 7',
  brain:    'M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z',
  check:    'M5 13l4 4L19 7',
  xmark:    'M18 6L6 18M6 6l12 12',
  dot:      'M12 12h.01',
  file:     'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8',
  chat:     'M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z',
  user:     'M16 7a4 4 0 11-8 0 4 4 0 018 0z M12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z',
  lock:     'M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z',
  ask:      'M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01 M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
  doc:      'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M12 18v-6 M9 15h6',
  asset:    'M4 7v10c0 2 1 3 3 3h10c2 0 3-1 3-3V7 M9 3h6 M8 3h8a2 2 0 012 2v1H6V5a2 2 0 012-2z M4 7h16',
  chevron:  'M6 9l6 6 6-6',
}

// ═══════════════════════════════════════════════════════════════════════
// Permission Modal
// ═══════════════════════════════════════════════════════════════════════

function PermissionModal({ req, sid, onDone }: { req: PermRequest; sid: string; onDone: () => void }) {
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

function AskUserModal({ req, sid, onDone }: { req: AskRequest; sid: string; onDone: () => void }) {
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

function ToolCard({ tool, sid }: { tool: ToolEntry; sid: string | null }) {
  const [expanded, setExpanded] = useState(false)
  const colors = tool.status === 'running' ? 'border-amber-200 bg-amber-50/50' : tool.status === 'error' ? 'border-red-200 bg-red-50/50' : 'border-emerald-200 bg-emerald-50/50'
  const dot = tool.status === 'running' ? 'bg-amber-400 animate-pulse' : tool.status === 'error' ? 'bg-red-400' : 'bg-emerald-400'
  return (
    <div className={`ml-10 border rounded-xl px-3 py-2 text-xs ${colors} animate-slide-up`}>
      <div className="flex items-center gap-2 cursor-pointer select-none" onClick={() => setExpanded(!expanded)}>
        <span className={`w-2 h-2 rounded-full ${dot}`} />
        <span className="font-medium text-gray-700">{tool.name}</span>
        <span className="text-gray-400 ml-auto text-[10px]">{expanded ? '收起' : '展开'}</span>
      </div>
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

function NavItem({ icon, label, active, onClick }: { icon: string; label: string; active?: boolean; onClick?: () => void }) {
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

function SessionItem({ session, active, onSelect, onDelete, onRename, onTogglePin }: {
  session: SessionInfo; active: boolean; onSelect: () => void;
  onDelete: () => void; onRename: (t: string) => void; onTogglePin: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [name, setName] = useState(session.title)

  return (
    <div onClick={onSelect}
      className={`group flex items-center justify-between px-3 py-2 rounded-xl text-sm cursor-pointer transition-colors ${
        active ? 'bg-blue-50 text-blue-700 font-medium' : 'text-gray-600 hover:bg-gray-50'
      }`}>
      {renaming ? (
        <input value={name} onChange={e => setName(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { onRename(name); setRenaming(false) } }}
          onBlur={() => { onRename(name); setRenaming(false) }}
          autoFocus
          className="flex-1 bg-white border border-blue-300 rounded-lg px-2 py-0.5 text-[13px] outline-none"
          onClick={e => e.stopPropagation()} />
      ) : (
        <div className="flex items-center gap-1.5 min-w-0">
          {session.pinned && <span className="text-[10px] shrink-0">📌</span>}
          <span className="truncate text-[13px]">{session.title || session.id.slice(0, 8)}</span>
          {session.busy && <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse shrink-0" />}
        </div>
      )}
      <div className="relative ml-1 shrink-0" onClick={e => e.stopPropagation()}>
        <button onClick={() => setMenuOpen(!menuOpen)}
          className="w-6 h-6 flex items-center justify-center rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 opacity-0 group-hover:opacity-100 transition-opacity">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="3" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="8" cy="13" r="1.5"/></svg>
        </button>
        {menuOpen && (
          <div className="absolute right-0 top-full mt-1 bg-white border border-gray-200 rounded-xl shadow-lg py-1 z-20 w-32 animate-fade-in">
            <button onClick={() => { setRenaming(true); setMenuOpen(false) }}
              className="w-full text-left px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 transition-colors">重命名</button>
            <button onClick={() => { onTogglePin(); setMenuOpen(false) }}
              className="w-full text-left px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 transition-colors">
              {session.pinned ? '取消置顶' : '置顶'}
            </button>
            <button onClick={() => { onDelete(); setMenuOpen(false) }}
              className="w-full text-left px-3 py-1.5 text-xs text-red-500 hover:bg-red-50 transition-colors">删除</button>
          </div>
        )}
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// Skills management panel
// ═══════════════════════════════════════════════════════════════════════

function SkillsPanel({ skills, onRefresh }: { skills: SkillInfo[]; onRefresh: () => void }) {
  const [form, setForm] = useState({ name: '', description: '', content: '' })
  const [showForm, setShowForm] = useState(false)
  const [validateMsg, setValidateMsg] = useState<{ valid: boolean; errors: string[] } | null>(null)
  const [enabledMap, setEnabledMap] = useState<Record<string, boolean>>({})

  // Load enabled state
  useEffect(() => { skills.forEach(async s => { try { const r = await api.skills.getEnabled(s.name); setEnabledMap(prev => ({ ...prev, [s.name]: r.enabled })) } catch {} }) }, [skills])

  const validate = async () => {
    if (!form.name || !form.description || !form.content) { setValidateMsg({ valid: false, errors: ['请填写所有字段'] }); return }
    try { const r = await api.skills.validate(form.name, form.description, form.content); setValidateMsg(r); if (r.valid) await doCreate() } catch (e: any) { setValidateMsg({ valid: false, errors: [e.message] }) }
  }

  const doCreate = async () => {
    try { await api.skills.create(form.name, form.description, form.content); setShowForm(false); setForm({ name: '', description: '', content: '' }); setValidateMsg(null); onRefresh() } catch (e: any) { alert(e.message) }
  }

  const toggleEnabled = async (name: string) => {
    try { const r = await api.skills.toggleEnabled(name); setEnabledMap(prev => ({ ...prev, [name]: r.enabled })) } catch {}
  }

  const deleteSkill = async (name: string) => {
    if (!confirm(`确定删除技能 "${name}"？`)) return
    try { await api.skills.delete(name); onRefresh() } catch (e: any) { alert(e.message) }
  }

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]; if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      const text = reader.result as string
      // Parse YAML frontmatter from uploaded file
      const parts = text.split('---')
      if (parts.length >= 3) {
        const fmLines = parts[1].trim().split('\n')
        const fm: Record<string, string> = {}
        for (const line of fmLines) { const idx = line.indexOf(':'); if (idx > 0) fm[line.slice(0, idx).trim()] = line.slice(idx + 1).trim().replace(/^"(.*)"$/, '$1') }
        const body = parts.slice(2).join('---').trim()
        setForm({ name: fm.name || '', description: fm.description || '', content: body })
      } else {
        setForm(prev => ({ ...prev, content: text }))
      }
      setValidateMsg(null)
    }
    reader.readAsText(file)
  }

  return (
    <div className="flex-1 overflow-y-auto bg-[#F9FAFB]">
      <div className="max-w-3xl mx-auto py-8 px-6">
        <div className="flex items-center justify-between mb-6">
          <h2 className="text-xl font-semibold text-gray-800">技能管理</h2>
          <button onClick={() => { setShowForm(true); setValidateMsg(null) }}
            className="px-4 py-2 rounded-xl bg-blue-500 text-white text-sm font-medium hover:bg-blue-600 transition-colors">+ 新建技能</button>
        </div>
        <div className="text-xs text-gray-400 mb-3">
          出厂自带: <code className="text-gray-500">skills/&lt;name&gt;/SKILL.md</code> · 运行时: <code className="text-gray-500">.mycodeagent/skills/&lt;name&gt;/SKILL.md</code>
        </div>

        {showForm && (
          <div className="bg-white border border-gray-200 rounded-xl p-5 mb-4 shadow-sm">
            <div className="flex items-center justify-between mb-3">
              <span className="text-sm font-medium text-gray-700">新建技能</span>
              <label className="cursor-pointer text-xs text-blue-500 hover:text-blue-600 flex items-center gap-1">
                <Svg d={Icons.doc} size={14} /> 上传 SKILL.md
                <input type="file" accept=".md,.txt" onChange={handleFile} className="hidden" />
              </label>
            </div>
            <input value={form.name} onChange={e => { setForm({ ...form, name: e.target.value }); setValidateMsg(null) }}
              placeholder="skill-name (小写字母+连字符)" className="w-full bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 text-sm mb-3 input-focus-ring" />
            <input value={form.description} onChange={e => { setForm({ ...form, description: e.target.value }); setValidateMsg(null) }}
              placeholder="描述（触发词、使用场景）" className="w-full bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 text-sm mb-3 input-focus-ring" />
            <textarea value={form.content} onChange={e => { setForm({ ...form, content: e.target.value }); setValidateMsg(null) }}
              rows={8} placeholder="Markdown 内容…" className="w-full bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 text-sm mb-3 input-focus-ring font-mono resize-none" />
            {/* Validation result */}
            {validateMsg && (
              <div className={`text-xs mb-3 p-2 rounded-lg ${validateMsg.valid ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-600'}`}>
                {validateMsg.valid ? '✓ 格式校验通过，已创建' : validateMsg.errors.map((e, i) => <div key={i}>• {e}</div>)}
              </div>
            )}
            <div className="flex gap-2 justify-end">
              <button onClick={() => { setShowForm(false); setValidateMsg(null) }} className="px-4 py-2 text-sm text-gray-500 hover:bg-gray-100 rounded-lg">取消</button>
              <button onClick={validate} className="px-4 py-2 text-sm bg-blue-500 text-white rounded-lg hover:bg-blue-600 font-medium">校验并创建</button>
            </div>
          </div>
        )}

        <div className="space-y-2">
          {skills.map((s, i) => {
            const enabled = enabledMap[s.name] !== false
            const isSource = s.base_dir.startsWith('skills/')  // read-only source skill
            return (
              <div key={i} className={`bg-white border rounded-xl p-4 flex items-start justify-between group transition-opacity ${!enabled ? 'opacity-50 border-gray-100' : 'border-gray-200'}`}>
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-800">{s.name}</span>
                    {isSource && <span className="text-[10px] bg-blue-50 text-blue-500 px-1.5 py-0.5 rounded-full font-medium">出厂</span>}
                    {!isSource && <span className="text-[10px] bg-emerald-50 text-emerald-600 px-1.5 py-0.5 rounded-full font-medium">用户</span>}
                    {!enabled && <span className="text-[10px] bg-gray-100 text-gray-400 px-1.5 py-0.5 rounded-full">已禁用</span>}
                  </div>
                  <div className="text-sm text-gray-500 mt-0.5">{s.description}</div>
                  <div className="text-[10px] text-gray-400 mt-0.5 font-mono">{s.base_dir}/SKILL.md</div>
                </div>
                <div className="flex items-center gap-2 ml-3 shrink-0">
                  <label className="toggle" title={enabled ? '禁用' : '启用'}>
                    <input type="checkbox" checked={enabled} onChange={() => toggleEnabled(s.name)} />
                    <span className="slider" />
                  </label>
                  {!isSource && (
                    <button onClick={() => deleteSkill(s.name)}
                      className="text-gray-300 hover:text-red-500 transition-colors opacity-0 group-hover:opacity-100 text-sm">删除</button>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════
// MCP management panel
// ═══════════════════════════════════════════════════════════════════════

function McpPanel({ servers, onRefresh }: { servers: McpServerCfg[]; onRefresh: () => void }) {
  const [form, setForm] = useState({ name: '', command: 'npx', args: '' })
  const [jsonInput, setJsonInput] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [inputMode, setInputMode] = useState<'form' | 'json'>('form')
  const [validateMsg, setValidateMsg] = useState('')
  const [enabledMap, setEnabledMap] = useState<Record<string, boolean>>({})

  useEffect(() => { servers.forEach(async s => { try { const r = await api.mcp.getEnabled(s.name); setEnabledMap(prev => ({ ...prev, [s.name]: r.enabled })) } catch {} }) }, [servers])

  const validateJson = (): { valid: boolean; name: string; command: string; args: string[] } | null => {
    try {
      const obj = JSON.parse(jsonInput)
      const servers = obj.mcpServers || obj
      const name = Object.keys(servers)[0]
      if (!name) return null
      const cfg = servers[name]
      if (!cfg.command) return null
      return { valid: true, name, command: cfg.command, args: cfg.args || [] }
    } catch { return null }
  }

  const addServer = async () => {
    if (inputMode === 'json') {
      const parsed = validateJson()
      if (!parsed) { setValidateMsg('JSON 格式无效，示例: {"mcpServers": {"name": {"command": "npx", "args": ["-y", "server"]}}}'); return }
      try {
        await api.mcp.create(parsed.name, parsed.command, parsed.args)
        setShowForm(false); setJsonInput(''); setValidateMsg(''); onRefresh()
      } catch (e: any) { setValidateMsg(e.message) }
      return
    }
    if (!form.name || !form.command) { setValidateMsg('请填写名称和命令'); return }
    const argsList = form.args.split(/\s+/).filter(Boolean)
    try {
      await api.mcp.create(form.name, form.command, argsList)
      setShowForm(false); setForm({ name: '', command: 'npx', args: '' }); setValidateMsg(''); onRefresh()
    } catch (e: any) { setValidateMsg(e.message) }
  }

  const toggleEnabled = async (name: string) => {
    try { const r = await api.mcp.toggleEnabled(name); setEnabledMap(prev => ({ ...prev, [name]: r.enabled })) } catch {}
  }

  const deleteServer = async (name: string) => {
    if (!confirm(`确定删除 MCP 服务器 "${name}"？`)) return
    try { await api.mcp.delete(name); onRefresh() } catch (e: any) { alert(e.message) }
  }

  return (
    <div className="flex-1 overflow-y-auto bg-[#F9FAFB]">
      <div className="max-w-3xl mx-auto py-8 px-6">
        <div className="flex items-center justify-between mb-6">
          <h2 className="text-xl font-semibold text-gray-800">MCP 服务器</h2>
          <button onClick={() => { setShowForm(true); setValidateMsg(''); setInputMode('form') }}
            className="px-4 py-2 rounded-xl bg-blue-500 text-white text-sm font-medium hover:bg-blue-600 transition-colors">+ 添加服务器</button>
        </div>
        <div className="text-xs text-gray-400 mb-3">配置文件: <code className="text-gray-500">mcp_servers.json</code>（标准 MCP JSON 格式，支持 command/args 或 url 传输）</div>

        {showForm && (
          <div className="bg-white border border-gray-200 rounded-xl p-5 mb-4 shadow-sm">
            <div className="flex gap-3 mb-3">
              <button onClick={() => { setInputMode('form'); setValidateMsg('') }}
                className={`text-xs pb-1 border-b-2 transition-colors ${inputMode === 'form' ? 'border-blue-500 text-blue-600 font-medium' : 'border-transparent text-gray-400'}`}>表单填写</button>
              <button onClick={() => { setInputMode('json'); setValidateMsg('') }}
                className={`text-xs pb-1 border-b-2 transition-colors ${inputMode === 'json' ? 'border-blue-500 text-blue-600 font-medium' : 'border-transparent text-gray-400'}`}>JSON 粘贴</button>
            </div>
            {inputMode === 'form' ? (
              <>
                <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
                  placeholder="服务器名称" className="w-full bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 text-sm mb-3 input-focus-ring" />
                <input value={form.command} onChange={e => setForm({ ...form, command: e.target.value })}
                  placeholder="命令 (npx / uvx / python)" className="w-full bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 text-sm mb-3 input-focus-ring font-mono" />
                <input value={form.args} onChange={e => setForm({ ...form, args: e.target.value })}
                  placeholder="参数 (空格分隔)" className="w-full bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 text-sm mb-3 input-focus-ring font-mono" />
              </>
            ) : (
              <textarea value={jsonInput} onChange={e => setJsonInput(e.target.value)}
                rows={6} placeholder='{"mcpServers": {"server-name": {"command": "npx", "args": ["-y", "package"]}}}'
                className="w-full bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 text-sm mb-3 input-focus-ring font-mono resize-none" />
            )}
            {validateMsg && <div className="text-xs text-red-500 mb-3 p-2 bg-red-50 rounded-lg">{validateMsg}</div>}
            <div className="flex gap-2 justify-end">
              <button onClick={() => { setShowForm(false); setValidateMsg('') }} className="px-4 py-2 text-sm text-gray-500 hover:bg-gray-100 rounded-lg">取消</button>
              <button onClick={addServer} className="px-4 py-2 text-sm bg-blue-500 text-white rounded-lg hover:bg-blue-600 font-medium">校验并添加</button>
            </div>
          </div>
        )}

        <div className="space-y-2">
          {servers.map((s, i) => {
            const enabled = enabledMap[s.name] !== false
            return (
              <div key={i} className={`bg-white border rounded-xl p-4 flex items-start justify-between group transition-opacity ${!enabled ? 'opacity-50 border-gray-100' : 'border-gray-200'}`}>
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-800">{s.name}</span>
                    {!enabled && <span className="text-[10px] bg-gray-100 text-gray-400 px-1.5 py-0.5 rounded-full">已禁用</span>}
                  </div>
                  <div className="text-xs text-gray-500 font-mono mt-0.5">{s.command} {s.args.join(' ')}</div>
                </div>
                <div className="flex items-center gap-2 ml-3 shrink-0">
                  <label className="toggle" title={enabled ? '禁用' : '启用'}>
                    <input type="checkbox" checked={enabled} onChange={() => toggleEnabled(s.name)} />
                    <span className="slider" />
                  </label>
                  <button onClick={() => deleteServer(s.name)}
                    className="text-gray-300 hover:text-red-500 transition-colors opacity-0 group-hover:opacity-100 text-sm">删除</button>
                </div>
              </div>
            )
          })}
        </div>
      </div>
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
  const [generatedFiles, setGeneratedFiles] = useState<{name: string; path: string}[]>([])
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)  // session id to confirm deletion

  // Config
  const [agentTeams, setAgentTeams] = useState(false)
  const [planMode, setPlanMode] = useState(false)
  const [thinkingLevel, setThinkingLevel] = useState('medium')
  const [teamsList, setTeamsList] = useState<TeamInfo[]>([])
  const [showNewTeam, setShowNewTeam] = useState(false)
  const [newTeamName, setNewTeamName] = useState('')

  // Sidebar state
  const [sidebarTab, setSidebarTab] = useState<'sessions'|'files'|'skills'|'mcp'>('sessions')
  const [moreOpen, setMoreOpen] = useState(false)
  const [skillsList, setSkillsList] = useState<SkillInfo[]>([])
  const [mcpServers, setMcpServers] = useState<McpServerCfg[]>([])
  const [fileEntries, setFileEntries] = useState<FileEntry[]>([])
  const [fileCwd, setFileCwd] = useState('.')

  // Models
  const [models, setModels] = useState<api.ModelInfo[]>([])
  const [showModelPicker, setShowModelPicker] = useState(false)

  const chatEndRef = useRef<HTMLDivElement>(null)
  const disconnectRef = useRef<(() => void) | null>(null)

  // ── Init ────────────────────────────────────────────────────────
  const loadSessions = async () => { try { setSessions(await api.sessions.list()) } catch {} }
  useEffect(() => { loadSessions() }, [])
  useEffect(() => { api.info.models().then(setModels).catch(() => {}) }, [])
  useEffect(() => { api.skills.list().then(setSkillsList).catch(() => {}) }, [])
  useEffect(() => { api.mcp.list().then(setMcpServers).catch(() => {}) }, [])

  const selectSession = async (sid: string) => {
    disconnectRef.current?.()
    setActiveSid(sid); setMessages([]); setTools([]); setPermReq(null); setAskReq(null); setGeneratedFiles([])
    disconnectRef.current = api.connectStream(sid, handleEvent, () => {})
    try { const cfg = await api.config.get(sid); setAgentTeams(cfg.enable_agent_teams); setPlanMode(cfg.plan_mode); setThinkingLevel(cfg.thinking_level) } catch {}
    // Load persisted history
    try {
      const res = await fetch(`/api/sessions/${sid}/history`)
      const data = await res.json()
      if (data.messages?.length) {
        setMessages(data.messages.map((m: any) => ({ id: uid(), role: m.role, content: m.content })))
      }
    } catch {}
    try { setTeamsList(await api.teams.list(sid)) } catch {}
  }

  const createSession = async () => {
    try { const s = await api.sessions.create(); setSessions(prev => [s, ...prev]); selectSession(s.id) } catch (e: any) { alert(e.message) }
  }

  const deleteSession = async (sid: string) => {
    // Optimistic: remove from UI immediately
    const prevSessions = sessions
    setSessions(prev => prev.filter(s => s.id !== sid))
    if (activeSid === sid) { setActiveSid(null); disconnectRef.current?.() }
    try { await api.sessions.delete(sid) } catch {
      // Rollback on failure
      setSessions(prevSessions)
      if (activeSid === sid) setActiveSid(sid)
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

  const loadFiles = async (path: string) => {
    if (!activeSid) return
    try { const tree = await api.files.tree(activeSid, path); setFileEntries(tree.entries); setFileCwd(tree.path) } catch {}
  }
  useEffect(() => { if (activeSid) loadFiles('.') }, [activeSid])

  // ── Events ──────────────────────────────────────────────────────
  const handleEvent = (event: AgentEvent) => {
    const { type, payload } = event
    switch (type) {
      case 'tool.started':
        setTools(prev => [...prev, { id: payload.tool_call_id || uid(), name: payload.tool, input: payload.input || {}, status: 'running', step: event.step }]); break
      case 'tool.completed': {
        let filePath = ''
        if (payload.status === 'success' && (payload.tool === 'Write' || payload.tool === 'Edit' || payload.tool === 'MultiEdit')) {
          try { const out = JSON.parse(payload.output || '{}'); filePath = out?.data?.path || out?.data?.file_path || payload.input?.file_path || '' } catch {}
          if (filePath) {
            const name = filePath.split('/').pop()?.split('\\').pop() || filePath
            setGeneratedFiles(prev => { if (prev.find(f => f.path === filePath)) return prev; return [...prev, { name, path: filePath }] })
          }
        }
        setTools(prev => prev.map(t => t.id === payload.tool_call_id ? { ...t, status: payload.status === 'success' ? 'success' : 'error', output: payload.output, filePath } : t)); break
      }
      case 'assistant.final':
        setMessages(prev => {
          const last = prev[prev.length - 1]
          if (last && last.role === 'assistant' && last.streaming) return prev.map((m, i) => i === prev.length - 1 ? { ...m, content: payload.content, streaming: false, step: payload.step } : m)
          return [...prev, { id: uid(), role: 'assistant', content: payload.content, streaming: false, step: payload.step }]
        }); setSending(false); break
      case 'run.finished': setSending(false); loadSessions(); break
      case 'permission.requested': setPermReq({ requestId: payload.request_id, tool: payload.tool, path: payload.path, action: payload.action }); break
      case 'ask_user.requested': setAskReq({ requestId: payload.request_id, prompt: payload.prompt }); break
      case 'turn.completed': setSending(false); loadSessions(); if (activeSid) { api.teams.list(activeSid).then(setTeamsList).catch(() => {}) }; break
      case 'error': setMessages(prev => [...prev, { id: uid(), role: 'assistant', content: `${payload.message}` }]); setSending(false); break
    }
  }

  useEffect(() => { chatEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, tools])

  // ── Send ────────────────────────────────────────────────────────
  const sendMessage = async () => {
    if (!input.trim() || sending) return
    // Auto-create session if none active
    let sid = activeSid
    if (!sid) {
      try {
        const s = await api.sessions.create(); setSessions(prev => [s, ...prev]); sid = s.id; setActiveSid(sid)
        disconnectRef.current?.()
        disconnectRef.current = api.connectStream(sid, handleEvent, () => {})
      } catch (e: any) { return }
    }
    const content = input.trim(); setInput(''); setSending(true)
    setMessages(prev => [...prev, { id: uid(), role: 'user', content }])
    const aid = uid(); setMessages(prev => [...prev, { id: aid, role: 'assistant', content: '', streaming: true }])
    try { await api.sessions.send(sid, content) } catch (e: any) {
      setMessages(prev => prev.map(m => m.id === aid ? { ...m, content: `${e.message}`, streaming: false } : m)); setSending(false)
    }
  }

  const fillScenario = (prompt: string) => { setInput(prompt) }
  const handleKeyDown = (e: React.KeyboardEvent) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() } }

  // ═══════════════════════════════════════════════════════════════════
  // Render
  // ═══════════════════════════════════════════════════════════════════

  const navItems = [
    { key: 'sessions' as const, icon: Icons.chat, label: '会话' },
    { key: 'files' as const, icon: Icons.folder, label: '文件' },
    { key: 'skills' as const, icon: Icons.skill, label: '技能' },
    { key: 'mcp' as const, icon: Icons.settings, label: 'MCP' },
  ]

  return (
    <div className="fixed inset-0 flex bg-[#F9FAFB] text-gray-800 overflow-hidden">
      {/* ═══════════════ LEFT SIDEBAR ═══════════════ */}
      <aside className="w-64 flex flex-col border-r border-gray-100 bg-white shrink-0">
        {/* Logo */}
        <div className="px-4 pt-4 pb-2">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-blue-500 flex items-center justify-center">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
            </div>
            <span className="font-bold text-gray-800 text-lg">MyCodeAgent</span>
          </div>
        </div>

        {/* New task button */}
        <div className="px-4 pb-3">
          <button onClick={createSession}
            className="w-full py-2.5 rounded-xl bg-blue-500 text-white text-sm font-semibold hover:bg-blue-600 transition-colors">
            + 新建任务
          </button>
        </div>

        {/* Agent teams toggle — below new task, above nav */}
        <div className="px-4 pb-3">
          <div className="flex items-center justify-between">
            <span className={`text-sm ${planMode ? 'text-gray-300' : 'text-gray-600'}`}>
              Agent 团队
              {planMode && <span className="text-[10px] text-gray-300 ml-1">(Plan 模式下不可用)</span>}
            </span>
            <label className="toggle"><input type="checkbox" checked={agentTeams} onChange={toggleTeams} disabled={planMode} /><span className="slider" /></label>
          </div>
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
      </aside>

      {/* ═══════════════ RIGHT MAIN ═══════════════ */}
      <main className="flex-1 flex flex-col min-w-0 bg-[#F9FAFB]">
        {/* Top bar */}
        <header className="px-6 py-4 flex items-center justify-end shrink-0 gap-3">
          {sending && <span className="text-xs text-blue-500 font-medium mr-2">Agent 处理中…</span>}
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
        </header>

        {/* ── Skills management panel ── */}
        {sidebarTab === 'skills' && (
          <SkillsPanel skills={skillsList} onRefresh={async () => { try { setSkillsList(await api.skills.list()) } catch {} }} />
        )}

        {/* ── MCP management panel ── */}
        {sidebarTab === 'mcp' && (
          <McpPanel servers={mcpServers} onRefresh={async () => { try { setMcpServers(await api.mcp.list()) } catch {} }} />
        )}

        {/* ── Chat / empty state ── */}
        {sidebarTab === 'sessions' && (messages.length === 0 && !sending ? (
          <div className="flex-1 flex flex-col items-center justify-center px-8">
            <h1 className="text-[28px] font-bold text-gray-800 mb-12 tracking-tight">Hi, 我是 MyCodeAgent</h1>

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
                      <span>{models[0]?.model || 'Model'}</span>
                      <Svg d={Icons.chevron} size={12} className="text-gray-400" />
                    </button>
                    {showModelPicker && models.length > 0 && (
                      <div className="absolute top-full left-0 mt-1 bg-white border border-gray-200 rounded-xl shadow-lg py-1 z-10 min-w-[180px]">
                        {models.map(m => (
                          <button key={m.name} onClick={() => setShowModelPicker(false)}
                            className="block w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-50 transition-colors">{m.model}</button>
                        ))}
                      </div>
                    )}
                  </div>
                  {/* Deep thinking toggle */}
                  <label className="flex items-center gap-2 cursor-pointer select-none">
                    <span className="text-sm text-gray-500">深度思考</span>
                    <label className="toggle"><input type="checkbox" checked={thinkingLevel === 'high'} onChange={() => setThinkingLevel(thinkingLevel === 'high' ? 'medium' : 'high')} /><span className="slider" /></label>
                  </label>
                  {/* Agent teams toggle */}
                  <label className="flex items-center gap-2 cursor-pointer select-none">
                    <span className="text-sm text-gray-500">Agent 团队</span>
                    <label className="toggle"><input type="checkbox" checked={agentTeams} onChange={toggleTeams} /><span className="slider" /></label>
                  </label>
                  {/* Plan mode toggle */}
                  <label className="flex items-center gap-2 cursor-pointer select-none">
                    <span className="text-sm text-gray-500">Plan</span>
                    <label className="toggle"><input type="checkbox" checked={planMode} onChange={async () => {
                      const next = !planMode; setPlanMode(next);
                      if (activeSid) { try { await api.config.update(activeSid, { plan_mode: next }) } catch { setPlanMode(!next) } }
                    }} /><span className="slider" /></label>
                  </label>
                </div>
                <div className="flex items-center gap-2">
                  {/* Microphone placeholder */}
                  <button className="w-9 h-9 rounded-full bg-gray-100 flex items-center justify-center text-gray-400 hover:bg-gray-200 transition-colors">
                    <Svg d={Icons.brain} size={17} />
                  </button>
                  {/* Send */}
                  <button onClick={sendMessage} disabled={!activeSid || sending || !input.trim()}
                    className="w-9 h-9 rounded-full bg-gray-200 text-gray-400 flex items-center justify-center hover:bg-blue-500 hover:text-white transition-all disabled:opacity-30 disabled:cursor-not-allowed">
                    <Svg d={Icons.send} size={17} />
                  </button>
                </div>
              </div>
              {/* Textarea */}
              <textarea value={input} onChange={e => setInput(e.target.value)} onKeyDown={handleKeyDown}
                disabled={sending} rows={2}
                placeholder="问任何问题"
                className="w-full bg-transparent px-5 pb-5 text-gray-800 resize-none text-[15px] leading-relaxed disabled:opacity-40 placeholder:text-gray-400 border-0 focus:outline-none"
              />
            </div>

            {/* Quick tags */}
            <div className="flex flex-wrap gap-2 justify-center mt-5">
              {SCENARIOS.map(s => (
                <button key={s.label} onClick={() => fillScenario(s.prompt)}
                  className="px-3.5 py-1.5 rounded-full border border-gray-200 text-xs text-gray-500 hover:text-blue-600 hover:border-blue-300 hover:bg-blue-50/50 transition-colors bg-white">
                  {s.label}
                </button>
              ))}
            </div>

            {/* Mode switch */}
            <button className="mt-12 text-xs text-gray-300 hover:text-gray-500 transition-colors">切换到经典</button>
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
                  <label className="flex items-center gap-2 cursor-pointer select-none">
                    <span className="text-sm text-gray-500">深度思考</span>
                    <label className="toggle"><input type="checkbox" checked={thinkingLevel === 'high'} onChange={() => setThinkingLevel(thinkingLevel === 'high' ? 'medium' : 'high')} /><span className="slider" /></label>
                  </label>
                </div>
                <button onClick={sendMessage} disabled={!activeSid || sending || !input.trim()}
                  className="w-8 h-8 rounded-full bg-gray-200 text-gray-400 flex items-center justify-center hover:bg-blue-500 hover:text-white transition-all disabled:opacity-30 disabled:cursor-not-allowed">
                  <Svg d={Icons.send} size={15} />
                </button>
              </div>
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
