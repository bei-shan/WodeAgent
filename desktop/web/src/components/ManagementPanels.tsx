import { useState, useEffect } from 'react'
import type React from 'react'
import * as api from '../api'
import type { SkillInfo, McpServerCfg } from '../api'
import { Icons, Svg } from './icons'

export function SkillsPanel({ skills, onRefresh }: { skills: SkillInfo[]; onRefresh: () => void }) {
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
          <div className="bg-white border border-gray-200 rounded-xl p-5 mb-4 shadow-xs">
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

export function McpPanel({ servers, onRefresh }: { servers: McpServerCfg[]; onRefresh: () => void }) {
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
          <div className="bg-white border border-gray-200 rounded-xl p-5 mb-4 shadow-xs">
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
// Hooks management panel
// ═══════════════════════════════════════════════════════════════════════

export function HooksPanel() {
  const [hooks, setHooks] = useState<Record<string, any>>({})
  const [json, setJson] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    fetch('/api/hooks').then(r => r.json()).then(d => {
      setHooks(d.hooks || {})
      setJson(JSON.stringify(d, null, 2))
    }).catch(() => {})
  }, [])

  const save = async () => {
    try {
      const parsed = JSON.parse(json)
      await fetch('/api/hooks', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: json })
      setHooks(parsed.hooks || {})
      setSaved(true); setTimeout(() => setSaved(false), 2000)
    } catch { alert('JSON 格式无效') }
  }

  const events = ['SessionStart', 'PreToolUse', 'PostToolUse', 'SessionEnd']
  return (
    <div className="flex-1 overflow-y-auto bg-[#F9FAFB]">
      <div className="max-w-3xl mx-auto py-8 px-6">
        <div className="flex items-center justify-between mb-6">
          <h2 className="text-xl font-semibold text-gray-800">Hooks 管理</h2>
          <button onClick={save}
            className="px-4 py-2 rounded-xl bg-blue-500 text-white text-sm font-medium hover:bg-blue-600 transition-colors">
            {saved ? '✓ 已保存' : '保存'}
          </button>
        </div>
        <div className="text-xs text-gray-400 mb-3">配置文件: <code className="text-gray-500">.mycode/hooks.json</code></div>

        {/* Summary */}
        <div className="grid grid-cols-4 gap-3 mb-5">
          {events.map(e => (
            <div key={e} className="bg-white border border-gray-200 rounded-xl p-3 text-center">
              <div className="text-lg font-bold text-gray-700">{(hooks[e] || []).length}</div>
              <div className="text-[10px] text-gray-400">{e}</div>
            </div>
          ))}
        </div>

        {/* JSON editor */}
        <textarea value={json} onChange={e => setJson(e.target.value)}
          rows={20}
          className="w-full bg-white border border-gray-200 rounded-xl p-4 text-sm text-gray-700 resize-none input-focus-ring font-mono"
          spellCheck={false} />
        <div className="text-[10px] text-gray-400 mt-1.5">
          格式: {"{"}"hooks": {"{"}"SessionStart": [{"{"}"matcher": "*", "hooks": [{"{"}"command": "echo started"{ "}"}]{"}"}]{"}"}{"}"}
        </div>
      </div>
    </div>
  )
}


export function MemoryPanel() {
  const [status, setStatus] = useState<api.MemoryStatus | null>(null)
  const [records, setRecords] = useState<api.MemoryRecordInfo[]>([])
  const [draft, setDraft] = useState('')
  const [query, setQuery] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  const loadRecords = async (text = query) => {
    try {
      const result = await api.memory.records(text)
      setRecords(result.records)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  const load = async () => {
    try {
      const current = await api.memory.status()
      setStatus(current)
      if (current.settings.enabled) await loadRecords('')
      else setRecords([])
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  useEffect(() => { load() }, [])

  const saveSettings = async (next: api.MemorySettings) => {
    setBusy(true)
    try {
      const updated = await api.memory.update(next)
      setStatus(updated)
      setNotice(updated.settings.enabled ? '记忆已启用' : '记忆已关闭')
      if (updated.settings.enabled) await loadRecords('')
      else setRecords([])
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  const toggle = async () => {
    if (!status) return
    const enabled = !status.settings.enabled
    const provider = enabled && status.settings.provider === 'off' ? 'sqlite' : status.settings.provider
    await saveSettings({ ...status.settings, enabled, provider })
  }

  const refreshStatus = async () => {
    try {
      setStatus(await api.memory.status())
    } catch {
      // The list below already reflects the change; a stale count is tolerable.
    }
  }

  const addRecord = async () => {
    const content = draft.trim()
    if (!content) return
    setBusy(true)
    try {
      await api.memory.add(content)
      setDraft('')
      await loadRecords(query)
      await refreshStatus()
      setNotice('已保存一条记忆')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  const removeRecord = async (id: string) => {
    try {
      await api.memory.remove(id)
      setRecords(prev => prev.filter(item => item.id !== id))
      await refreshStatus()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  if (!status) {
    return <div className="flex-1 bg-[#F9FAFB] p-6 text-sm text-gray-400">正在读取记忆设置…</div>
  }

  const enabled = status.settings.enabled
  return (
    <div className="flex-1 overflow-y-auto bg-[#F9FAFB]">
      <div className="max-w-3xl mx-auto py-8 px-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-xl font-semibold text-gray-800">记忆</h2>
            <p className="text-xs text-gray-400 mt-1">
              长期记忆只属于当前工作区；启用后 Agent 可主动存取。
            </p>
          </div>
          <button type="button" onClick={toggle} disabled={busy}
            className={`px-4 py-2 rounded-xl text-sm font-medium transition-colors ${enabled
              ? 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50'
              : 'bg-blue-500 text-white hover:bg-blue-600'}`}>
            {enabled ? '关闭记忆' : '启用记忆'}
          </button>
        </div>

        <div className="grid grid-cols-3 gap-3 mb-5">
          <div className="bg-white border border-gray-200 rounded-xl p-3 text-center">
            <div className="text-lg font-bold text-gray-700">{enabled ? status.count : 0}</div>
            <div className="text-[10px] text-gray-400">条记忆</div>
          </div>
          <div className="bg-white border border-gray-200 rounded-xl p-3 text-center">
            <div className="text-lg font-bold text-gray-700">{status.settings.provider}</div>
            <div className="text-[10px] text-gray-400">存储后端</div>
          </div>
          <div className="bg-white border border-gray-200 rounded-xl p-3 text-center">
            <div className="text-lg font-bold text-gray-700">{status.settings.limit}</div>
            <div className="text-[10px] text-gray-400">每次召回条数</div>
          </div>
        </div>

        {notice && <div className="text-xs text-gray-500 mb-3">{notice}</div>}
        {status.error && <div className="text-xs text-red-500 mb-3">{status.error}</div>}

        <div className="bg-white border border-gray-200 rounded-xl p-4 mb-5">
          <div className="flex flex-wrap items-center gap-3">
            <label className="text-xs text-gray-500">后端</label>
            <select value={status.settings.provider}
              onChange={event => saveSettings({ ...status.settings, provider: event.target.value, enabled })}
              className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm">
              {status.providers.map(provider => (
                <option key={provider} value={provider}>{provider}</option>
              ))}
            </select>
            <label className="text-xs text-gray-500">召回条数</label>
            <input type="number" min={1} max={20} value={status.settings.limit}
              onChange={event => setStatus({ ...status, settings: { ...status.settings, limit: Number(event.target.value) } })}
              onBlur={() => saveSettings(status.settings)}
              className="w-20 border border-gray-200 rounded-lg px-2 py-1.5 text-sm" />
            <label className="flex items-center gap-2 text-xs text-gray-500">
              <input type="checkbox" checked={status.settings.auto_capture} disabled={!enabled}
                onChange={event => saveSettings({ ...status.settings, auto_capture: event.target.checked })} />
              自动记忆
            </label>
            <label className="text-xs text-gray-500">每回合最多</label>
            <input type="number" min={1} max={3} value={status.settings.max_per_turn}
              disabled={!status.settings.auto_capture}
              onChange={event => setStatus({ ...status, settings: { ...status.settings, max_per_turn: Number(event.target.value) } })}
              onBlur={() => saveSettings(status.settings)}
              className="w-16 border border-gray-200 rounded-lg px-2 py-1.5 text-sm disabled:opacity-40" />
            <span className="text-[10px] text-gray-400 break-all">{status.store}</span>
          </div>
        </div>

        <div className="bg-white border border-gray-200 rounded-xl p-4 mb-5">
          <textarea value={draft} onChange={event => setDraft(event.target.value)}
            rows={3} placeholder={enabled ? '写入一条长期记忆，例如：本仓库使用 pnpm，不要用 npm。' : '先启用记忆再写入'}
            disabled={!enabled}
            className="w-full border border-gray-200 rounded-lg p-3 text-sm text-gray-700 resize-none input-focus-ring" />
          <div className="flex justify-end mt-2">
            <button type="button" onClick={addRecord} disabled={!enabled || busy || !draft.trim()}
              className="px-4 py-1.5 rounded-lg bg-blue-500 text-white text-sm font-medium hover:bg-blue-600 disabled:opacity-40">
              保存记忆
            </button>
          </div>
        </div>

        <div className="flex items-center gap-2 mb-3">
          <input value={query} onChange={event => setQuery(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter') loadRecords() }}
            placeholder="搜索记忆后回车"
            className="flex-1 border border-gray-200 rounded-lg px-3 py-2 text-sm input-focus-ring" />
          <button type="button" onClick={() => loadRecords()} disabled={!enabled}
            className="px-3 py-2 rounded-lg border border-gray-200 text-sm text-gray-600 hover:bg-gray-50 disabled:opacity-40">
            搜索
          </button>
        </div>

        <div className="space-y-2">
          {records.length === 0 && (
            <div className="text-xs text-gray-400">{enabled ? '还没有匹配的记忆' : '记忆未启用'}</div>
          )}
          {records.map(record => (
            <div key={record.id} className="bg-white border border-gray-200 rounded-xl p-3 flex items-start gap-3">
              <div className="flex-1 min-w-0">
                <p className="text-sm text-gray-700 whitespace-pre-wrap break-words">{record.content}</p>
                <p className="text-[10px] text-gray-400 mt-1">{record.created_at}</p>
              </div>
              <button type="button" onClick={() => removeRecord(record.id)}
                className="text-xs text-red-500 hover:text-red-600 shrink-0">删除</button>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

export function ModelsPanel() {
  const [settings, setSettings] = useState<api.ModelSettings | null>(null)
  const [form, setForm] = useState({ name: '', model: '', provider: '', base_url: '', api_key: '' })
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  const load = async () => {
    try {
      setSettings(await api.modelSettings.list())
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  useEffect(() => { load() }, [])

  const save = async () => {
    setBusy(true)
    try {
      await api.modelSettings.save({
        name: form.name.trim(),
        model: form.model.trim(),
        provider: form.provider.trim(),
        base_url: form.base_url.trim(),
        api_key: form.api_key,
      })
      setForm({ name: '', model: '', provider: '', base_url: '', api_key: '' })
      await load()
      setNotice('模型配置已保存')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (name: string) => {
    try {
      await api.modelSettings.remove(name)
      await load()
      setNotice('已删除 ' + name)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  if (!settings) {
    return <div className="flex-1 bg-[#F9FAFB] p-6 text-sm text-gray-400">正在读取模型配置…</div>
  }

  return (
    <div className="flex-1 overflow-y-auto bg-[#F9FAFB]">
      <div className="max-w-3xl mx-auto py-8 px-6">
        <h2 className="text-xl font-semibold text-gray-800">模型</h2>
        <p className="text-xs text-gray-400 mt-1 mb-6">
          这里保存的配置写入 .mycodeagent/settings/models.json，密钥只在本机保存、界面只显示末四位。
        </p>

        {notice && <div className="text-xs text-gray-500 mb-3">{notice}</div>}

        <div className="space-y-2 mb-6">
          {settings.profiles.length === 0 && (
            <div className="text-xs text-gray-400">还没有模型配置，先在下方添加一个。</div>
          )}
          {settings.profiles.map(profile => (
            <div key={profile.name} className="bg-white border border-gray-200 rounded-xl p-3 flex items-start gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-800">{profile.name}</span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500">
                    {profile.source === 'env' ? 'env' : '界面'}
                  </span>
                </div>
                <div className="text-xs text-gray-500 mt-0.5">
                  {profile.model}
                  {profile.provider ? ' · ' + profile.provider : ''}
                  {profile.base_url ? ' · ' + profile.base_url : ''}
                </div>
                <div className="text-[10px] text-gray-400 mt-0.5">
                  密钥：{profile.has_key ? profile.api_key : '未设置'}
                </div>
              </div>
              {profile.source === 'file' && (
                <button type="button" onClick={() => remove(profile.name)}
                  className="text-xs text-red-500 hover:text-red-600 shrink-0">删除</button>
              )}
            </div>
          ))}
        </div>

        <div className="bg-white border border-gray-200 rounded-xl p-4">
          <div className="text-sm font-medium text-gray-700 mb-3">添加 / 更新</div>
          <div className="grid grid-cols-2 gap-3">
            <input value={form.name} onChange={event => setForm({ ...form, name: event.target.value })}
              placeholder="名称（小写字母/数字/-_）"
              className="border border-gray-200 rounded-lg px-3 py-2 text-sm input-focus-ring" />
            <input value={form.model} onChange={event => setForm({ ...form, model: event.target.value })}
              placeholder="模型 ID，例如 deepseek-v4"
              className="border border-gray-200 rounded-lg px-3 py-2 text-sm input-focus-ring" />
            <input value={form.provider} onChange={event => setForm({ ...form, provider: event.target.value })}
              placeholder="Provider，例如 deepseek / openai"
              className="border border-gray-200 rounded-lg px-3 py-2 text-sm input-focus-ring" />
            <input value={form.base_url} onChange={event => setForm({ ...form, base_url: event.target.value })}
              placeholder="Base URL，例如 https://api.deepseek.com"
              className="border border-gray-200 rounded-lg px-3 py-2 text-sm input-focus-ring" />
            <input type="password" value={form.api_key} onChange={event => setForm({ ...form, api_key: event.target.value })}
              placeholder="API Key（留空则保留原值）"
              className="col-span-2 border border-gray-200 rounded-lg px-3 py-2 text-sm input-focus-ring" />
          </div>
          <div className="flex justify-end mt-3">
            <button type="button" onClick={save} disabled={busy || !form.name.trim() || !form.model.trim()}
              className="px-4 py-1.5 rounded-lg bg-blue-500 text-white text-sm font-medium hover:bg-blue-600 disabled:opacity-40">
              保存模型
            </button>
          </div>
          <p className="text-[10px] text-gray-400 mt-3">
            env 中通过 MODEL_PROFILES 配置的同名模型会被这里覆盖；保存后新会话即可选用。
          </p>
        </div>
      </div>
    </div>
  )
}
