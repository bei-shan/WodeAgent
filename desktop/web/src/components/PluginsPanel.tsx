import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import type { PluginInfo } from '../api'

export default function PluginsPanel() {
  const [items, setItems] = useState<PluginInfo[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      setItems(await api.plugins.list())
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '无法读取插件')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const execute = async (action: () => Promise<unknown>) => {
    setBusy(true)
    try {
      await action()
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败')
      try { setItems(await api.plugins.list()) } catch { /* keep diagnostic */ }
    } finally {
      setBusy(false)
    }
  }

  const install = (file?: File) => {
    if (!file) return
    if (file.size > 2 * 1024 * 1024) { setError('ZIP 不能超过 2 MiB'); return }
    void execute(() => api.plugins.install(file))
  }

  return <section className="flex-1 overflow-y-auto px-5 py-6">
    <div className="mx-auto max-w-3xl space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-gray-800">插件管理</h2>
        <p className="mt-1 text-xs text-gray-500">
          插件目录在 .mycode/plugins；仅支持本地 ZIP 导入，默认禁用，不会自动下载或执行第三方代码。
        </p>
      </div>
      <div className="rounded-xl border border-gray-200 bg-white p-4 space-y-3">
        <label htmlFor="plugin-upload" className="text-sm font-medium text-gray-800">安装插件</label>
        <input id="plugin-upload" type="file" accept=".zip,application/zip"
          disabled={busy} className="block w-full text-xs text-gray-600"
          onChange={event => {
            install(event.target.files?.[0])
            event.currentTarget.value = ''
          }} />
        <p className="text-xs text-gray-500">
          ZIP 根目录需有 plugin.json（或 .claude-plugin/plugin.json）。
          启用后插件 Skills 会进入 Agent 的 Skill 列表；已有会话的代码型扩展需要重新创建会话。
        </p>
      </div>
      {error && <div role="alert" className="rounded-xl border border-red-100 bg-red-50 p-3 text-xs text-red-700">{error}</div>}
      {loading && <p className="text-xs text-gray-500">正在读取插件…</p>}
      {!loading && items.length === 0 && <p className="rounded-xl bg-white p-4 text-sm text-gray-500">尚未安装插件。</p>}
      {items.map(plugin => <article key={plugin.name} className="rounded-xl border border-gray-200 bg-white p-4 space-y-3">
        <div className="flex justify-between items-start gap-3">
          <div className="min-w-0">
            <h3 className="font-semibold text-sm text-gray-800 break-all">{plugin.name}
              <span className="ml-2 text-xs font-normal text-gray-400">v{plugin.version}</span>
            </h3>
            {plugin.description && <p className="text-xs text-gray-500 mt-1">{plugin.description}</p>}
            <p className="text-xs text-gray-500 mt-1">
              {plugin.skills} 个 Skill 目录 {plugin.has_code ? '· 包含可执行 Hook / Python 扩展' : '· 仅声明式组件'}
            </p>
          </div>
          <label className="shrink-0 text-xs flex items-center gap-1.5 text-gray-700">
            <input type="checkbox" aria-label={`启用 ${plugin.name}`}
              checked={plugin.enabled} disabled={busy}
              onChange={event => {
                const enabled = event.target.checked
                setItems(previous => previous.map(item =>
                  item.name === plugin.name ? { ...item, enabled } : item
                ))
                void execute(() => api.plugins.enable(plugin.name, enabled))
              }} />
            启用
          </label>
        </div>
        {plugin.has_code && <div className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900">
          插件代码可能访问本地文件与执行命令。信任状态与启用状态独立，且服务端仍须显式设置 RIDGE_ALLOW_PLUGIN_CODE=1；未经信任时只加载 Skills。
          <label className="flex items-center gap-2 mt-2">
            <input type="checkbox" checked={plugin.trusted} disabled={busy}
              aria-label={`信任 ${plugin.name}`} onChange={event => {
                const next = event.target.checked
                if (next && !window.confirm(`确认信任插件 ${plugin.name} 的可执行代码？仅在审查来源后进行此操作。`)) return
                setItems(previous => previous.map(item =>
                  item.name === plugin.name ? { ...item, trusted: next } : item
                ))
                void execute(() => api.plugins.trust(plugin.name, next))
              }} />
            信任代码
          </label>
        </div>}
        <button type="button" disabled={busy}
          onClick={() => {
            if (window.confirm(`确定卸载插件 ${plugin.name}？`)) void execute(() => api.plugins.delete(plugin.name))
          }}
          className="text-xs text-red-600 hover:underline disabled:opacity-50">卸载插件</button>
      </article>)}
    </div>
  </section>
}
