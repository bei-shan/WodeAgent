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
          启用后插件 Skills 会进入 Agent 的 Skill 列表；命令 Hook 仅能在受信任的本地模式下，经显式审核后加载；Python Feature 插件不再支持。
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
              {plugin.skills} 个 Skill 目录 {plugin.has_code ? '· 包含需授权的命令 Hook' : '· 仅声明式组件'}
            </p>
            <p className="text-xs text-gray-400 mt-1 break-all">
              来源：{plugin.source || 'local_directory'}
              {plugin.sha256 ? ` · ZIP SHA-256: ${plugin.sha256.slice(0, 16)}…` : ''}
              {plugin.previous_version ? ` · 上一版 v${plugin.previous_version}` : ''}
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
          {plugin.trusted && !plugin.trust_valid && <p role="alert" className="mb-2 font-semibold">
            插件文件在授权后发生变化（或授权记录无效），Hook 信任已失效，请审查文件后重新授权。
          </p>}
          本地命令 Hook 可访问宿主资源，不属于沙箱。必须同时启用插件、审查内容指纹并在服务端设置 RIDGE_ALLOW_TRUSTED_HOOKS=1；托管 Worker 不加载此类 Hook。
          <label className="flex items-center gap-2 mt-2">
            <input type="checkbox" checked={plugin.trusted} disabled={busy}
              aria-label={`信任 ${plugin.name}`} onChange={event => {
                const next = event.target.checked
                if (next && !window.confirm(`确认授权插件 ${plugin.name} 的本地命令 Hook？它可以在宿主执行命令，请先审查来源。`)) return
                setItems(previous => previous.map(item =>
                  item.name === plugin.name ? { ...item, trusted: next } : item
                ))
                void execute(() => api.plugins.trust(plugin.name, next))
              }} />
            信任命令 Hook
          </label>
        </div>}
        <div className="flex flex-wrap items-center gap-3 text-xs">
          {plugin.sha256 && <label className="flex items-center gap-1.5 text-gray-700">
            <input type="checkbox" aria-label={`锁定 ${plugin.name} 版本`}
              checked={!!plugin.pinned} disabled={busy}
              onChange={event => {
                const pinned = event.target.checked
                setItems(previous => previous.map(item =>
                  item.name === plugin.name ? { ...item, pinned } : item
                ))
                void execute(() => api.plugins.pin(plugin.name, pinned))
              }} />
            锁定版本
          </label>}
          <label className={`text-blue-600 cursor-pointer ${busy || plugin.pinned ? 'opacity-50 pointer-events-none' : ''}`}>
            更新 ZIP
            <input type="file" accept=".zip,application/zip" className="sr-only"
              aria-label={`更新 ${plugin.name} ZIP`} disabled={busy || !!plugin.pinned}
              onChange={event => {
                const file = event.target.files?.[0]
                event.currentTarget.value = ''
                if (!file) return
                if (file.size > 2 * 1024 * 1024) { setError('ZIP 不能超过 2 MiB'); return }
                if (!window.confirm(`更新 ${plugin.name} 后会禁用插件并撤销代码信任，确定继续？`)) return
                void execute(() => api.plugins.update(plugin.name, file))
              }} />
          </label>
          {plugin.rollback_available && <button type="button" disabled={busy}
            onClick={() => {
              if (window.confirm(`回退 ${plugin.name} 到上一个版本？插件将保持禁用。`)) {
                void execute(() => api.plugins.rollback(plugin.name))
              }
            }} className="text-blue-600 hover:underline disabled:opacity-50">回退上一版</button>}
          <button type="button" disabled={busy}
            onClick={() => {
              if (window.confirm(`确定卸载插件 ${plugin.name}？`)) void execute(() => api.plugins.delete(plugin.name))
            }}
            className="text-red-600 hover:underline disabled:opacity-50">卸载插件</button>
        </div>
      </article>)}
    </div>
  </section>
}
