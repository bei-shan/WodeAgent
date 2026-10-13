import type { Dispatch, MutableRefObject, SetStateAction } from 'react'
import * as api from '../api'
import type { AgentEvent, TeamInfo } from '../api'
import type { Message, ToolEntry, PermRequest, AskRequest } from '../components/types'

export interface SessionEventBindings {
  activeSidRef: MutableRefObject<string | null>
  setSubagentStatus: Dispatch<SetStateAction<string>>
  setMessages: Dispatch<SetStateAction<Message[]>>
  setGeneratedFiles: Dispatch<SetStateAction<Array<{ name: string; path: string }>>>
  setTools: Dispatch<SetStateAction<ToolEntry[]>>
  setPermReq: Dispatch<SetStateAction<PermRequest | null>>
  setAskReq: Dispatch<SetStateAction<AskRequest | null>>
  setSending: Dispatch<SetStateAction<boolean>>
  setTeamsList: Dispatch<SetStateAction<TeamInfo[]>>
  loadSessions: () => void | Promise<void>
  uid: () => string
}

/**
 * Translate the server's ordered event journal into UI state updates.
 * No JSX and no React hooks: views consume the state projected here.
 * The caller owns WebSocket subscription lifetime, session epoch and cleanup.
 */
export function createSessionEventHandler(bindings: SessionEventBindings): (event: AgentEvent) => void {
  const { activeSidRef, setSubagentStatus, setMessages, setGeneratedFiles,
    setTools, setPermReq, setAskReq, setSending, setTeamsList, loadSessions, uid } = bindings
  return (event: AgentEvent) => {
    const { type, payload } = event
    switch (type) {
      case 'stream.reset':
        setSubagentStatus('事件缓存已过期，正在从已保存的会话历史恢复…')
        if (activeSidRef.current) {
          const sid = activeSidRef.current
          api.sessions.history(sid).then(data => {
            if (activeSidRef.current === sid && data.messages.length) {
              setMessages(data.messages.map(m => ({ id: uid(), role: m.role, content: m.content })))
            }
          }).catch(() => {})
        }
        break
      case 'llm.delta':
        if (payload.kind === 'content' && payload.text) {
          setMessages(prev => {
            const last = prev[prev.length - 1]
            if (last?.role === 'assistant' && last.streaming) {
              return prev.map((m, i) => i === prev.length - 1 ? { ...m, content: m.content + payload.text } : m)
            }
            return [...prev, { id: uid(), role: 'assistant', content: payload.text, streaming: true }]
          })
        }
        break
      case 'subagent.started': setSubagentStatus('子代理运行中…'); break
      case 'subagent.tool_use': setSubagentStatus('子代理正在调用工具…'); break
      case 'subagent.finished': setSubagentStatus('子代理已完成'); break
      case 'tool.started':
        setTools(prev => [...prev, { id: payload.tool_call_id || uid(), name: payload.tool, input: payload.input || {}, status: 'running', step: event.step }]); break
      case 'tool.completed': {
        let filePath = ''
        if (payload.status === 'success' && (payload.tool === 'Write' || payload.tool === 'Edit' || payload.tool === 'MultiEdit')) {
          try {
            const out = JSON.parse(payload.output || '{}')
            filePath = out?.data?.path || out?.context?.path_resolved || payload.input?.path || ''
          } catch {}
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
      case 'run.finished': loadSessions(); break
      case 'permission.requested': setPermReq({ requestId: payload.request_id, tool: payload.tool, path: payload.path, action: payload.action }); break
      case 'ask_user.requested': setAskReq({ requestId: payload.request_id, prompt: payload.prompt }); break
      case 'turn.finished': {
        const status = String(payload.status || 'failed')
        const response = String(payload.response || '')
        const messages: Record<string, string> = {
          rate_limited: '模型服务限流，重试次数已用尽',
          provider_unavailable: '模型服务暂时不可用',
          network: '模型连接失败',
          unauthorized: '模型认证失败，请检查 API 配置',
          context_overflow: '上下文已超出模型限制，请先压缩对话',
          invalid_request: '模型请求参数无效',
          stream_interrupted: '模型流式输出已中断；未自动重复请求',
          step_limit: '已达到 Agent 最大执行步数，请缩小任务范围或调整设置',
          agent_error: 'Agent 执行失败，请查看服务端日志',
        }
        setMessages(prev => {
          const last = prev[prev.length - 1]
          if (status === 'failed') {
            const detail = messages[String(payload.error_kind)] || '执行失败，请检查服务端日志'
            return [...prev.map(m => m.streaming ? { ...m, streaming: false } : m),
              { id: uid(), role: 'assistant', content: `执行失败：${detail}` }]
          }
          if (status === 'cancelled') {
            if (last?.streaming) {
              return prev.map((m, i) => i === prev.length - 1
                ? { ...m, content: response || '已停止生成（用户中断）', streaming: false } : m)
            }
            return [...prev, { id: uid(), role: 'assistant', content: response || '已停止生成（用户中断）' }]
          }
          if (last?.streaming) {
            return prev.map((m, i) => i === prev.length - 1
              ? { ...m, content: response || m.content, streaming: false } : m)
          }
          return prev
        })
        setSending(false); loadSessions()
        if (activeSidRef.current) { api.teams.list(activeSidRef.current).then(setTeamsList).catch(() => {}) }
        break
      }
      case 'turn.completed':
        // Legacy completion event; canonical turn.finished is handled above.
        setMessages(prev => prev.map(m => m.streaming ? {
          ...m,
          content: String(payload.response || '').startsWith('已停止生成')
            ? String(payload.response) : m.content || String(payload.response || ''),
          streaming: false,
        } : m))
        setSending(false); loadSessions()
        if (activeSidRef.current) { api.teams.list(activeSidRef.current).then(setTeamsList).catch(() => {}) }
        break
      case 'error':
        setMessages(prev => [...prev.map(m => m.streaming ? { ...m, streaming: false } : m), { id: uid(), role: 'assistant', content: String(payload.message || '运行失败') }])
        setSending(false); break
    }
  }

}
