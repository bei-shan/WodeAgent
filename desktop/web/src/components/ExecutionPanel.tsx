import { useState } from 'react'
import type { ToolEntry } from './types'
import DockerTerminal from './DockerTerminal'

type Tab = 'timeline' | 'terminal' | 'interactive'

function sandboxResult(tool: ToolEntry): { stdout: string; stderr: string; exitCode: number | null; sandbox: string } | null {
  if (tool.name.toLowerCase() !== 'bash' || !tool.output) return null
  try {
    const parsed = JSON.parse(tool.output)
    const data = parsed?.data
    if (!data || !['boxlite', 'docker'].includes(data.sandbox)) return null
    return {
      stdout: typeof data.stdout === 'string' ? data.stdout.slice(0, 20000) : '',
      stderr: typeof data.stderr === 'string' ? data.stderr.slice(0, 20000) : '',
      exitCode: Number.isInteger(data.exit_code) ? data.exit_code : null,
      sandbox: data.sandbox,
    }
  } catch { return null }
}

/** This panel never executes commands; it only projects already received tool events. */
export default function ExecutionPanel({ tools, open, onClose, sessionId }: {
  tools: ToolEntry[]; open: boolean; onClose: () => void; sessionId: string | null
}) {
  const [tab, setTab] = useState<Tab>('timeline')
  if (!open) return null
  const sandboxLogs = tools.map(tool => ({ tool, result: sandboxResult(tool) }))
    .filter((row): row is { tool: ToolEntry; result: NonNullable<ReturnType<typeof sandboxResult>> } => row.result !== null)
  const complete = tools.filter(tool => tool.status !== 'running').length

  return <aside className="execution-panel" aria-label="Agent 执行详情">
    <header className="flex items-center justify-between border-b border-gray-100 px-4 py-3">
      <div>
        <h2 className="font-semibold text-sm text-gray-800">Agent 执行详情</h2>
        <p className="text-[11px] text-gray-500 mt-1">当前会话实时工具事件 · {complete}/{tools.length} 已结束</p>
      </div>
      <button type="button" onClick={onClose} aria-label="关闭执行详情" className="rounded-lg px-2 py-1 text-gray-500 hover:bg-gray-100">✕</button>
    </header>
    <div className="flex gap-2 border-b border-gray-100 px-3 py-2">
      <button type="button" onClick={() => setTab('timeline')} aria-pressed={tab === 'timeline'}
        className={tab === 'timeline' ? 'execution-tab selected' : 'execution-tab'}>执行时间线</button>
      <button type="button" onClick={() => setTab('terminal')} aria-pressed={tab === 'terminal'}
        className={tab === 'terminal' ? 'execution-tab selected' : 'execution-tab'}>Sandbox 输出</button>
      <button type="button" onClick={() => setTab('interactive')} aria-pressed={tab === 'interactive'}
        className={tab === 'interactive' ? 'execution-tab selected' : 'execution-tab'}>Docker TTY</button>
    </div>
    <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-3">
      {tab === 'interactive' ? <DockerTerminal sessionId={sessionId} /> : tab === 'timeline' ? (
        tools.length ? tools.map((tool, index) => (
          <details key={tool.id + '-' + index} className="rounded-xl border border-gray-200 bg-white p-3 text-xs">
            <summary className="cursor-pointer font-medium text-gray-800">
              <span className={tool.status === 'success' ? 'text-green-600' : tool.status === 'error' ? 'text-red-600' : 'text-amber-600'}>● </span>
              Step {tool.step} · {tool.name}
              <span className="float-right text-gray-500">{tool.status}</span>
            </summary>
            <div className="mt-3 space-y-2">
              <div className="text-gray-500">参数</div>
              <pre className="execution-json">{JSON.stringify(tool.input, null, 2).slice(0, 5000)}</pre>
              {tool.output && <><div className="text-gray-500">结果</div>
                <pre className="execution-json">{tool.output.slice(0, 8000)}</pre></>}
            </div>
          </details>
        )) : <p className="text-xs text-gray-500 p-3">当前会话暂无实时工具调用。历史回答不包含完整工具事件。</p>
      ) : (
        <>
          <p className="text-xs text-gray-500 px-1">只读输出，来自已执行的 Docker / BoxLite Bash 工具；不能在这里输入或执行命令。</p>
          {sandboxLogs.length ? sandboxLogs.map(({ tool, result }, index) => (
            <section key={tool.id + '-' + index} className="terminal-output">
              <div className="terminal-header">
                <span>Step {tool.step} · {result.sandbox}</span>
                <span>{result.exitCode === null ? '退出码未知' : '退出码 ' + result.exitCode}</span>
              </div>
              <pre className="terminal-body">{result.stdout || '(stdout 为空)'}{result.stderr ? '\n[stderr]\n' + result.stderr : ''}</pre>
            </section>
          )) : <p className="text-xs text-gray-500 p-3">暂无隔离沙箱命令输出；普通主机 Bash 结果不会被标记为 Sandbox。</p>}
        </>
      )}
    </div>
  </aside>
}
