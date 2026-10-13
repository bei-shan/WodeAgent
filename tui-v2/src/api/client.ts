import type { SessionInfo, TurnStatus } from "../protocol"

export type SavedHistory = { messages: Array<{ role: string; content: string }> }
export type TurnOutcome = { status: TurnStatus; response?: string; error_kind?: string; retryable?: boolean }

/** Adapter for the existing Python/FastAPI application. No terminal dependencies. */
export class AgentClient {
  readonly baseUrl: string
  constructor(baseUrl = "http://127.0.0.1:8000") {
    const base = new URL(baseUrl)
    if (!["http:", "https:"].includes(base.protocol)) throw new Error("Server URL must use http(s)")
    this.baseUrl = base.origin
  }

  private async request<T>(method: string, path: string, body?: object): Promise<T> {
    const result = await fetch(this.baseUrl + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
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
