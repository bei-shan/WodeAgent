import { AgentClient } from "./api/client"
import { connectStream, type ConnectionState } from "./api/stream"
import type { AgentEvent, SessionInfo } from "./protocol"
import { emptyView, projectEvent, withHistory, type SessionView } from "./state/reducer"

/** Application controller: UI invokes commands, server owns Agent execution. */
export class TuiController {
  private view: SessionView = emptyView()
  private listeners = new Set<(view: SessionView) => void>()
  private disconnect: (() => void) | null = null
  private epoch = 0
  private disposed = false

  constructor(private readonly api: AgentClient) {}

  get snapshot(): SessionView { return this.view }

  subscribe(listener: (view: SessionView) => void): () => void {
    this.listeners.add(listener)
    listener(this.view)
    return () => this.listeners.delete(listener)
  }

  private update(next: SessionView): void {
    if (this.disposed) return
    this.view = next
    for (const listener of this.listeners) listener(next)
  }

  private error(cause: unknown): void {
    const message = cause instanceof Error ? cause.message : String(cause)
    this.update({ ...this.view, notice: message, busy: false })
  }

  async start(): Promise<void> {
    try {
      await this.api.health()
      const sessions = await this.api.listSessions()
      if (this.disposed) return
      this.update({ ...this.view, sessions })
      if (sessions.length) await this.selectSession(sessions[0].id)
      else await this.newSession()
    } catch (cause) {
      this.error(cause)
    }
  }

  async newSession(): Promise<void> {
    try {
      const created = await this.api.createSession()
      if (this.disposed) return
      this.update({ ...this.view, sessions: [created, ...this.view.sessions] })
      await this.selectSession(created.id, true)
    } catch (cause) {
      this.error(cause)
    }
  }

  async selectSession(id: string, newSession = false): Promise<void> {
    this.disconnect?.()
    this.disconnect = null
    const epoch = ++this.epoch
    const selected = this.view.sessions.find((s: SessionInfo) => s.id === id)
    this.update({
      ...emptyView(),
      sessions: this.view.sessions,
      sessionId: id,
      title: selected?.title ?? id.slice(0, 8),
      connection: "connecting",
    })
    try {
      await this.api.activateSession(id)
      const [history, turn] = await Promise.all([this.api.history(id), this.api.turn(id)])
      if (epoch !== this.epoch || this.disposed) return
      this.update({
        ...withHistory(this.view, history.messages),
        busy: turn.status === "running",
        status: turn.status,
      })
      // A fresh session needs replay from sequence 0; an existing session
      // starts at the server's watermark (or active turn start).
      this.disconnect = connectStream(
        this.api.baseUrl, id,
        (event) => {
          if (epoch !== this.epoch || this.disposed) return
          this.handleEvent(id, event, epoch)
        },
        (connection: ConnectionState) => {
          if (epoch !== this.epoch || this.disposed) return
          this.update({ ...this.view, connection })
        },
        newSession ? 0 : null,
      )
    } catch (cause) {
      if (epoch === this.epoch && !this.disposed) this.error(cause)
    }
  }

  private handleEvent(id: string, event: AgentEvent, epoch: number): void {
    const next = projectEvent(this.view, event)
    this.update(next)
    if (event.type === "stream.reset") {
      // Event Journal is in-memory; history on disk is the recovery source.
      // Never fabricate token deltas after an expired cursor.
      void this.api.history(id).then((history) => {
        if (epoch !== this.epoch || this.disposed) return
        this.update(withHistory(this.view, history.messages))
      }).catch((cause) => this.error(cause))
    }
    if (event.type === "turn.finished") {
      void this.refreshSessions()
    }
  }

  async refreshSessions(): Promise<void> {
    try {
      const sessions = await this.api.listSessions()
      if (!this.disposed) this.update({ ...this.view, sessions })
    } catch (cause) {
      this.error(cause)
    }
  }

  /** Submission is context-sensitive: pending AskUser and permission are server commands. */
  async submit(raw: string): Promise<void> {
    const content = raw.trim()
    const id = this.view.sessionId
    if (!id || !content) return
    const pending = this.view.pending
    try {
      if (pending?.kind === "ask") {
        await this.api.answer(id, pending.requestId, content)
        if (id === this.view.sessionId) this.update({ ...this.view, pending: null })
        return
      }
      if (pending?.kind === "permission") {
        const decision = content.toLowerCase()
        if (!["y", "yes", "n", "no"].includes(decision)) {
          this.update({ ...this.view, notice: "Type y to allow or n to deny" })
          return
        }
        await this.api.permission(id, pending.requestId, decision === "y" || decision === "yes")
        if (id === this.view.sessionId) this.update({ ...this.view, pending: null, notice: "" })
        return
      }
      if (this.view.busy) {
        this.update({ ...this.view, notice: "Turn already running. Ctrl+C to interrupt." })
        return
      }
      const user = {
        id: "local-" + Date.now(), role: "user" as const,
        content, streaming: false,
      }
      this.update({
        ...this.view, messages: [...this.view.messages, user],
        busy: true, status: "running", notice: "",
      })
      await this.api.send(id, content)
    } catch (cause) {
      this.error(cause)
    }
  }

  async interrupt(): Promise<void> {
    if (!this.view.busy || !this.view.sessionId) return
    try {
      await this.api.interrupt(this.view.sessionId)
      this.update({ ...this.view, notice: "Stop requested; waiting for turn.finished" })
    } catch (cause) {
      this.error(cause)
    }
  }

  close(): void {
    this.disposed = true
    ++this.epoch
    this.disconnect?.()
    this.disconnect = null
    this.listeners.clear()
  }
}
