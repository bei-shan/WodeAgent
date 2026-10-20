/** Wire contract shared by terminal components, state reducers and network transport. */
export type TurnStatus = "idle" | "running" | "completed" | "failed" | "cancelled"

export interface AgentEvent {
  type: string
  payload: Record<string, unknown>
  step: number
  seq?: number
}

export interface SessionInfo {
  id: string
  title: string
  busy: boolean
  pinned?: boolean
}

export interface ChatMessage {
  id: string
  role: "user" | "assistant"
  content: string
  streaming: boolean
  step?: number
}

export interface ToolEntry {
  id: string
  name: string
  status: "running" | "success" | "error"
  input?: unknown
  output?: string
}

export type PendingPrompt =
  | { kind: "permission"; requestId: string; tool: string; path: string; action: string }
  | { kind: "ask"; requestId: string; question: string }

export function stringField(payload: Record<string, unknown>, key: string): string {
  return typeof payload[key] === "string" ? payload[key] as string : ""
}

export function decodeEvent(raw: unknown): AgentEvent | null {
  if (typeof raw !== "string") return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== "object") return null
    const obj = parsed as Record<string, unknown>
    if (typeof obj.type !== "string" || !obj.type) return null
    if (!obj.payload || typeof obj.payload !== "object" || Array.isArray(obj.payload)) return null
    if (obj.seq !== undefined && (!Number.isSafeInteger(obj.seq) || (obj.seq as number) < 0)) return null
    return {
      type: obj.type,
      payload: obj.payload as Record<string, unknown>,
      step: typeof obj.step === "number" ? obj.step : 0,
      seq: typeof obj.seq === "number" ? obj.seq : undefined,
    }
  } catch {
    return null
  }
}
