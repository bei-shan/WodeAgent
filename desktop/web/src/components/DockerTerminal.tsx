import { useEffect, useRef, useState } from 'react'
import * as api from '../api'

export default function DockerTerminal({ sessionId }: { sessionId: string | null }) {
  const [enabled, setEnabled] = useState(false)
  const [connection, setConnection] = useState<'idle'|'connecting'|'connected'|'closed'>('idle')
  const [output, setOutput] = useState('')
  const [command, setCommand] = useState('')
  const socket = useRef<WebSocket | null>(null)

  useEffect(() => {
    let cancelled = false
    api.info.terminalCapabilities().then(info => {
      if (!cancelled) setEnabled(info.enabled === true)
    }).catch(() => { if (!cancelled) setEnabled(false) })
    return () => {
      cancelled = true
      socket.current?.close()
      socket.current = null
    }
  }, [sessionId])

  const connect = () => {
    if (!sessionId || !enabled || socket.current) return
    setConnection('connecting')
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(protocol + '//' + location.host + '/api/sessions/' +
      encodeURIComponent(sessionId) + '/terminal')
    socket.current = ws
    ws.onmessage = event => {
      try {
        const record = JSON.parse(event.data)
        if (record.type === 'ready') setConnection('connected')
        if (record.type === 'data' && typeof record.text === 'string') {
          // Backpressure cap: the UI is a command console, not a terminal emulator.
          setOutput(old => (old + record.text).slice(-100000))
        }
        if (record.type === 'limit' || record.type === 'error') {
          setOutput(old => old + '\n[' + String(record.message || record.type) + ']\n')
        }
      } catch { /* invalid container transport frame */ }
    }
    ws.onclose = () => { if (socket.current === ws) socket.current = null; setConnection('closed') }
    ws.onerror = () => setOutput(old => old + '\n[容器连接失败]\n')
  }

  const disconnect = () => {
    socket.current?.close()
    socket.current = null
    setConnection('closed')
  }

  const send = () => {
    if (!command.trim() || socket.current?.readyState !== WebSocket.OPEN) return
    socket.current.send(JSON.stringify({ type: 'input', text: command + '\n' }))
    setCommand('')
  }

  return <section aria-label="Docker 交互终端" className="flex flex-col gap-3">
    <p className="text-xs text-gray-500">
      实验性 Docker TTY：真实命令在禁网络、资源受限的容器中运行。暂不提供 ANSI 全屏终端仿真或 BoxLite PTY。
      此能力必须由管理员显式启用并配置 WEB_AUTH_TOKEN。
    </p>
    {!enabled ? <p className="rounded-lg bg-gray-100 p-3 text-xs text-gray-600">Docker TTY 未启用或当前服务器不支持。</p> :
      <div className="flex gap-2">
        <button type="button" onClick={connect} disabled={!!socket.current || !sessionId}
          className="rounded-lg bg-gray-800 px-3 py-2 text-xs text-white disabled:opacity-40">连接 Docker TTY</button>
        <button type="button" onClick={disconnect} disabled={!socket.current}
          className="rounded-lg border px-3 py-2 text-xs disabled:opacity-40">断开</button>
        <span role="status" className="ml-auto self-center text-xs text-gray-500">{connection}</span>
      </div>
    }
    <pre className="terminal-body terminal-client-output" aria-label="TTY 输出">{output || '(无终端输出)'}</pre>
    <div className="flex gap-2">
      <input value={command} onChange={e => setCommand(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); send() } }}
        placeholder="输入命令，Enter 发送" aria-label="发送容器命令"
        disabled={connection !== 'connected'} className="min-w-0 flex-1 rounded-lg border px-3 py-2 text-xs disabled:opacity-40" />
      <button type="button" onClick={send} disabled={connection !== 'connected' || !command.trim()}
        className="rounded-lg bg-gray-800 px-3 py-2 text-xs text-white disabled:opacity-40">发送</button>
    </div>
  </section>
}
