import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import type { TraceAnalysis, TraceRun, TraceNode } from '../api'

const millis = (value: number | null) => value === null ? '—' :
  value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value} ms`

function TokenNumbers({ run }: { run: TraceRun }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <Metric label="输入 Tokens" value={run.tokens.input.toLocaleString()} />
      <Metric label="输出 Tokens" value={run.tokens.output.toLocaleString()} />
      <Metric label="执行时长" value={millis(run.duration_ms)} />
      <Metric label="估算费用 (USD)" value={
        run.cost_usd_estimate === null ? '未配置费率' : `$${run.cost_usd_estimate.toFixed(6)}`
      } />
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="rounded-xl border border-gray-200 bg-white p-3">
    <div className="text-[11px] text-gray-500">{label}</div>
    <div className="mt-1 text-sm font-semibold text-gray-900 break-all">{value}</div>
  </div>
}

function TraceLeaf({ node }: { node: TraceNode }) {
  const tone = node.status === 'success' || node.status === 'completed'
    ? 'text-green-700 bg-green-50'
    : node.status === 'error' || node.status === 'failed'
      ? 'text-red-700 bg-red-50' : 'text-amber-700 bg-amber-50'
  return <div className="ml-5 flex items-center justify-between gap-4 rounded-lg border border-gray-100 bg-gray-50/70 px-3 py-2 text-xs">
    <div className="flex min-w-0 items-center gap-2">
      <span className="text-gray-400">{node.kind === 'model' ? '◇' : '⚙'}</span>
      <span className="truncate font-medium text-gray-700">{node.name}</span>
      <span className={`shrink-0 rounded-sm px-1.5 py-0.5 text-[10px] ${tone}`}>{node.status}</span>
    </div>
    <div className="shrink-0 text-right text-gray-500">
      {millis(node.duration_ms)}
      {node.tokens && <span className="ml-2" title="输入/输出 Tokens">
        {node.tokens.input.toLocaleString()}/{node.tokens.output.toLocaleString()}
      </span>}
    </div>
  </div>
}

function RunView({ run }: { run: TraceRun }) {
  const health = run.quality.label === 'healthy' ? '无已知运行错误'
    : run.quality.label === 'needs_review' ? '需要检查' : '尚未结束或存在重试'
  return <article className="rounded-2xl border border-gray-200 bg-white p-4 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-semibold text-gray-800">Turn #{run.run_id}</h3>
      <span className="text-xs rounded-full bg-gray-100 px-3 py-1 text-gray-600">{run.status}</span>
    </div>
    <TokenNumbers run={run} />
    <div className="rounded-xl bg-gray-50 p-3 text-xs text-gray-600">
      <div className="font-semibold text-gray-700">运行健康度：{health}</div>
      <div className="mt-1">工具错误 {run.quality.tool_errors} · 空响应重试 {run.quality.empty_retries}</div>
      <div className="mt-1 text-[11px] text-gray-400">这是基于执行状态的诊断信号，不是回答正确率、代码质量或 LLM 评审分数。</div>
    </div>
    <div className="space-y-2" aria-label="调用树">
      <div className="text-xs font-semibold text-gray-600">调用树</div>
      {run.steps.length === 0 && <p className="text-xs text-gray-400">尚无步骤元数据。</p>}
      {run.steps.map(step => <details key={step.step} className="group rounded-xl border border-gray-100 bg-white" open={step.step === run.steps[0]?.step}>
        <summary className="cursor-pointer select-none px-3 py-2 text-xs font-semibold text-gray-700">
          Step {step.step} <span className="ml-2 font-normal text-gray-400">{step.nodes.length} 个调用</span>
        </summary>
        <div className="space-y-2 px-3 pb-3">
          {step.nodes.map((node, idx) => <TraceLeaf key={idx} node={node} />)}
        </div>
      </details>)}
    </div>
  </article>
}

export default function TracePanel({ sessionId }: { sessionId: string | null }) {
  const [data, setData] = useState<TraceAnalysis | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const reload = useCallback(async () => {
    if (!sessionId) { setData(null); return }
    try {
      const info = await api.sessions.trace(sessionId)
      setData(info)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : '追踪获取失败')
    } finally {
      setLoading(false)
    }
  }, [sessionId])

  useEffect(() => {
    setData(null)
    setLoading(true)
    void reload()
    const handle = window.setInterval(() => { void reload() }, 8000)
    return () => window.clearInterval(handle)
  }, [reload])

  return <div className="flex-1 overflow-y-auto px-5 py-6">
    <div className="mx-auto max-w-4xl space-y-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-800">Agent Trace 分析</h2>
          <p className="mt-1 text-xs text-gray-500">
            本地执行元数据 · 每 8 秒更新 · 提示词与工具输入输出不会通过此接口传输
          </p>
        </div>
        <button type="button" onClick={() => { setLoading(true); void reload() }}
          disabled={!sessionId || loading}
          className="shrink-0 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>
      {!sessionId && <div className="rounded-xl bg-white p-6 text-sm text-gray-500">请先选择会话。</div>}
      {error && <div role="alert" className="rounded-xl border border-red-100 bg-red-50 p-3 text-xs text-red-700">{error}</div>}
      {sessionId && data && !data.available && <div className="rounded-xl bg-white p-6 text-sm text-gray-500">
        当前会话没有可读取的本地 Trace。需要先执行一次 Agent Turn，并启用 TRACE_ENABLED。
      </div>}
      {data?.available && <>
        <div className="flex flex-wrap gap-2 items-center rounded-xl border border-gray-200 bg-white p-3">
          <span className="text-xs text-gray-500">本地 Trace ID</span>
          <code className="text-xs font-semibold text-gray-800 break-all">{data.trace_id}</code>
          <button type="button" className="ml-auto text-xs text-blue-600 hover:underline"
            onClick={() => { if (data.trace_id) void navigator.clipboard.writeText(data.trace_id) }}>复制</button>
        </div>
        {data.truncated && <p className="text-xs text-amber-700">数据量超过读取上限，显示的是部分执行记录。</p>}
        {!data.pricing_configured && <p className="text-xs text-gray-500">
          费用估算未启用：请在服务端设置 TRACE_INPUT_USD_PER_MILLION 与 TRACE_OUTPUT_USD_PER_MILLION。不同模型需使用对应费率。
        </p>}
        {data.runs.length === 0 && <p className="text-sm text-gray-500">尚无可分析的运行记录。</p>}
        {data.runs.map(run => <RunView key={run.run_id} run={run} />)}
      </>}
    </div>
  </div>
}
