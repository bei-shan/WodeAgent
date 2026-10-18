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

