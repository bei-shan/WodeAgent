import type { SessionInfo, TurnStatus } from "../protocol"

export type SessionTreeNode = {
  id: string
  parentId: string | null
  role?: string
  content?: string
  type: string
  summary?: string
}
export type SessionTree = {
  nodes: Record<string, SessionTreeNode>
  children: Record<string, string[]>
  cursor_id: string | null
  labels: Record<string, string>
}
export type PortableMessage = {
  id: string
  parent_id: string | null
  role: "user" | "assistant"
  content: string
}
export type PortableSession = {
  format: "ridgecode.portable-session"
  version: 1
  title: string
  cursor_id: string | null
  messages: PortableMessage[]
  labels: Record<string, string>
}
export type SavedHistory = { messages: Array<{ role: string; content: string }> }
export type TurnOutcome = { status: TurnStatus; response?: string; error_kind?: string; retryable?: boolean }

/** Adapter for the existing Python/FastAPI application. No terminal dependencies. */
export class AgentClient {
  readonly baseUrl: string
  readonly authToken: string | undefined
  constructor(baseUrl = "http://127.0.0.1:8000", authToken?: string) {
    const base = new URL(baseUrl)
    if (!["http:", "https:"].includes(base.protocol)) throw new Error("Server URL must use http(s)")
    this.baseUrl = base.origin
    this.authToken = authToken || undefined
    if (this.authToken && base.protocol === "http:" &&
        !["localhost", "127.0.0.1", "[::1]"].includes(base.hostname)) {
      throw new Error("Bearer authentication requires HTTPS or a loopback server")
    }
  }

  private async request<T>(method: string, path: string, body?: object): Promise<T> {
    const result = await fetch(this.baseUrl + path, {
      method,
      headers: {
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...(this.authToken ? { Authorization: "Bearer " + this.authToken } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    })
    if (!result.ok) {
      let detail = result.statusText
      try {
        const payload: unknown = await result.json()
        if (payload && typeof payload === "object" && "detail" in payload) {
          const value = (payload as { detail?: unknown }).detail
          if (typeof value === "string") detail = value
        }
      } catch { /* server may not return JSON */ }
      throw new Error("HTTP " + result.status + ": " + detail)
    }
    if (result.status === 204) return undefined as T
    return await result.json() as T
  }

  health() { return this.request<{ status: string }>("GET", "/api/health") }
  listSessions() { return this.request<SessionInfo[]>("GET", "/api/sessions") }
  createSession(title = "") { return this.request<SessionInfo>("POST", "/api/sessions", { title }) }
  activateSession(id: string) { return this.request<{ status: string }>("POST", "/api/sessions/" + encodeURIComponent(id) + "/activate") }
  renameSession(id: string, title: string) {
    return this.request<{ status: string; title: string }>("PUT", "/api/sessions/" + encodeURIComponent(id) + "/rename", { title })
  }
  togglePin(id: string) {
    return this.request<{ pinned: boolean }>("POST", "/api/sessions/" + encodeURIComponent(id) + "/pin")
  }
  deleteSession(id: string) {
    return this.request<{ status: string }>("DELETE", "/api/sessions/" + encodeURIComponent(id))
  }
  models() {
    return this.request<Array<{ name: string; model: string; provider: string }>>("GET", "/api/models")
  }
  sessionConfig(id: string) {
    return this.request<{ model: string; provider: string; plan_mode: boolean; enable_agent_teams: boolean }>(
      "GET", "/api/sessions/" + encodeURIComponent(id) + "/config",
    )
  }
  updateSessionConfig(id: string, payload: { model_profile?: string; plan_mode?: boolean; enable_agent_teams?: boolean }) {
    return this.request<{ status: string }>("PUT", "/api/sessions/" + encodeURIComponent(id) + "/config", payload)
  }
  tree(id: string) {
    return this.request<SessionTree>("GET", "/api/sessions/" + encodeURIComponent(id) + "/tree")
  }
  fork(id: string, targetId: string) {
    return this.request<{ cursor_id: string }>(
      "POST", "/api/sessions/" + encodeURIComponent(id) + "/fork",
      { target_id: targetId },
    )
  }
  exportPortableSession(id: string) {
    return this.request<PortableSession>("GET", "/api/sessions/" + encodeURIComponent(id) + "/export")
  }
  importPortableSession(payload: unknown) {
    return this.request<SessionInfo>("POST", "/api/sessions/import", payload as object)
  }
  history(id: string) { return this.request<SavedHistory>("GET", "/api/sessions/" + encodeURIComponent(id) + "/history") }
  turn(id: string) { return this.request<TurnOutcome>("GET", "/api/sessions/" + encodeURIComponent(id) + "/turn") }
  send(id: string, content: string) {
    return this.request<{ status: string }>("POST", "/api/sessions/" + encodeURIComponent(id) + "/messages", { content })
  }
  interrupt(id: string) { return this.request<{ status: string }>("POST", "/api/sessions/" + encodeURIComponent(id) + "/interrupt") }
  permission(id: string, requestId: string, granted: boolean) {
    return this.request<{ status: string }>("POST", "/api/sessions/" + encodeURIComponent(id)
      + "/permissions/" + encodeURIComponent(requestId) + "/resolve",
      { decision: granted ? "granted" : "denied" })
  }
  answer(id: string, requestId: string, answer: string) {
    return this.request<{ status: string }>("POST", "/api/sessions/" + encodeURIComponent(id)
      + "/ask-user/" + encodeURIComponent(requestId) + "/answer", { answer })
  }
}
