import type { AgentEvent, ChatMessage, PendingPrompt, SessionInfo, ToolEntry, TurnStatus } from "../protocol"
import { stringField } from "../protocol"

/** Pure projection: every UI state change is derived from a command result or event. */
export interface SessionView {
  sessionId: string | null
  title: string
  sessions: SessionInfo[]
  messages: ChatMessage[]
  tools: ToolEntry[]
  pending: PendingPrompt | null
  busy: boolean
  status: TurnStatus
  connection: "connecting" | "connected" | "disconnected"
  lastSeq: number | null
  needsResync: boolean
  notice: string
  subagent: string
}

export function emptyView(): SessionView {
  return {
    sessionId: null, title: "", sessions: [], messages: [], tools: [],
    pending: null, busy: false, status: "idle", connection: "disconnected",
    lastSeq: null, needsResync: false, notice: "", subagent: "",
  }
}

export function withHistory(view: SessionView, history: Array<{ role: string; content: string }>): SessionView {
  const messages: ChatMessage[] = history
    .filter((m) => (m.role === "user" || m.role === "assistant") && typeof m.content === "string")
    .map((m, i) => ({
      id: "history-" + i,
      role: m.role as ChatMessage["role"],
      content: m.content,
      streaming: false,
    }))
  return { ...view, messages: messages.slice(-200), tools: [], needsResync: false }
}

function appendAssistant(view: SessionView, content: string, step: number): ChatMessage[] {
  const last = view.messages.at(-1)
  if (last?.role === "assistant" && last.streaming && last.step === step) {
    return [...view.messages.slice(0, -1), { ...last, content: last.content + content }]
  }
  return [...view.messages, {
    id: "stream-" + (view.lastSeq ?? view.messages.length),
    role: "assistant", content, streaming: true, step,
  }]
}

function finishAssistant(messages: ChatMessage[], content: string, step: number): ChatMessage[] {
  const last = messages.at(-1)
  if (last?.role === "assistant" && last.streaming) {
    return [...messages.slice(0, -1), { ...last, content: content || last.content, streaming: false }]
  }
  if (content && !(last?.role === "assistant" && last.content === content)) {
    return [...messages, { id: "final-" + messages.length, role: "assistant", content, streaming: false, step }]
  }
  return messages
}

export function projectEvent(current: SessionView, event: AgentEvent): SessionView {
  const p = event.payload
  if (event.type === "stream.reset") {
    const resume = p.resume_from
    return {
      ...current,
      lastSeq: typeof resume === "number" && Number.isSafeInteger(resume) && resume >= 0 ? resume : null,
      needsResync: true,
      notice: "Event history expired; restoring saved messages",
    }
  }
  if (event.seq !== undefined && current.lastSeq !== null && event.seq <= current.lastSeq) return current
  const view: SessionView = { ...current, lastSeq: event.seq ?? current.lastSeq }
  const step = event.step
  switch (event.type) {
    case "run.started":
      return { ...view, busy: true, status: "running", notice: "" }
    case "llm.delta":
      if (p.kind !== "content" || typeof p.text !== "string" || !p.text) return view
      return { ...view, messages: appendAssistant(view, p.text, step) }
    case "assistant.final":
      return { ...view, messages: finishAssistant(view.messages, stringField(p, "content"), step) }
    case "tool.started": {
      const id = stringField(p, "tool_call_id") || "tool-" + (view.lastSeq ?? view.tools.length)
      return { ...view, tools: [...view.tools, {
        id, name: stringField(p, "tool"), status: "running" as const, input: p.input,
      }].slice(-100) }
    }
    case "tool.completed": {
      const id = stringField(p, "tool_call_id")
      return { ...view, tools: view.tools.map((t) => t.id === id ? {
        ...t,
        status: p.status === "success" ? "success" : "error",
        output: stringField(p, "output"),
      } : t) }
    }
    case "permission.requested":
      return { ...view, pending: {
        kind: "permission", requestId: stringField(p, "request_id"),
        tool: stringField(p, "tool"), path: stringField(p, "path"),
        action: stringField(p, "action"),
      } }
    case "ask_user.requested":
      return { ...view, pending: {
        kind: "ask", requestId: stringField(p, "request_id"), question: stringField(p, "prompt"),
      } }
    case "subagent.started":
      return { ...view, subagent: "Subagent running: " + stringField(p, "description") }
    case "subagent.tool_use":
      return { ...view, subagent: "Subagent tool: " + stringField(p, "name") }
    case "subagent.finished":
      return { ...view, subagent: p.ok === true ? "Subagent finished" : "Subagent failed" }
    case "turn.finished": {
      const raw = stringField(p, "status")
      const status: TurnStatus = raw === "completed" || raw === "failed" || raw === "cancelled" ? raw : "failed"
      return {
        ...view, busy: false, status, pending: null,
        messages: finishAssistant(view.messages, stringField(p, "response"), step),
        notice: status === "failed" ? "Turn failed: " + (stringField(p, "error_kind") || "agent_error")
          : status === "cancelled" ? "Turn stopped" : "",
      }
    }
    case "error":
      return { ...view, notice: stringField(p, "message") || "Server error" }
    default:
      return view
  }
}
