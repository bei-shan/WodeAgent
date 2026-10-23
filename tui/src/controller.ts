import { AgentClient } from "./api/client"
import { parseTerminalCommand, TERMINAL_HELP, type TerminalCommand } from "./commands"
import { formatSessionTree } from "./tree"
import { loadArchiveFile, saveArchiveFile } from "./localArchive"
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
        this.api.authToken,
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

  private note(text: string): void {
    this.update({ ...this.view, notice: text })
  }

  private async command(cmd: TerminalCommand): Promise<void> {
    const id = this.view.sessionId
    if (cmd.type === "invalid") { this.note(cmd.reason); return }
    if (cmd.type === "help") { this.note(TERMINAL_HELP); return }
    if (cmd.type === "stop") { await this.interrupt(); return }
    if (cmd.type === "sessions") {
      await this.refreshSessions()
      this.note(this.view.sessions.map((s, i) =>
        (i + 1) + ". " + (s.pinned ? "★ " : "") + s.title + " (" + s.id.slice(0, 8) + ")",
      ).join("\n") || "No sessions")
      return
    }
    if (cmd.type === "new") { await this.newSession(); return }
    if (cmd.type === "use") {
      const selected = this.view.sessions[cmd.index - 1]
      if (!selected) { this.note("Session index out of range. Use /sessions."); return }
      await this.selectSession(selected.id)
      return
    }
    if (cmd.type === "models") {
      const models = await this.api.models()
      this.note(models.map((m) => m.name + " · " + m.provider + "/" + m.model).join("\n") || "No model profiles configured")
      return
    }
    if (!id) { this.note("Create or select a session first."); return }
    if (cmd.type === "import") {
      if (this.view.busy) { this.note("Finish the current turn before importing."); return }
      const archive = await loadArchiveFile(cmd.path)
      const created = await this.api.importPortableSession(archive)
      if (this.disposed) return
      await this.refreshSessions()
      await this.selectSession(created.id)
      if (this.view.sessionId === created.id) {
        this.note("Imported portable conversation: " + created.title)
      }
      return
    }
    if (cmd.type === "export") {
      if (this.view.busy) { this.note("Finish the current turn before exporting."); return }
      const epoch = this.epoch
      const archive = await this.api.exportPortableSession(id)
      if (this.disposed || epoch !== this.epoch || this.view.sessionId !== id) return
      await saveArchiveFile(cmd.path, archive)
      this.note("Exported portable conversation to " + cmd.path)
      return
    }
    if (cmd.type === "tree") {
      if (this.view.busy) { this.note("Wait for the current turn to finish."); return }
      this.note(formatSessionTree(await this.api.tree(id)))
      return
    }
    if (cmd.type === "goal") {
      if (cmd.action === "set") {
        const saved = await this.api.setGoal(id, cmd.objective)
        if (this.disposed) return
        this.note(
          "Goal set: " + saved.goal.objective
          + " · rounds " + saved.goal.rounds_started + "/" + saved.goal.max_rounds,
        )
        return
      }
      if (cmd.action === "clear") {
        const result = await this.api.clearGoal(id)
        if (this.disposed) return
        this.note(result.cleared ? "Goal cleared." : "No goal to clear.")
        return
      }
      const current = await this.api.goal(id)
      if (this.disposed) return
      this.note(current.goal
        ? "Goal [" + current.goal.status + "] " + current.goal.objective
          + " · rounds " + current.goal.rounds_started + "/" + current.goal.max_rounds
          + (current.goal.blocked_reason ? " · blocked: " + current.goal.blocked_reason : "")
        : "No goal for this session. Use /goal set TEXT.")
      return
    }
    if (cmd.type === "delete") {
      if (!cmd.confirmed) { this.note("To delete the current session, type /delete confirm"); return }
      if (this.view.busy) { this.note("Stop the current turn before deleting."); return }
      await this.api.deleteSession(id)
      this.disconnect?.()
      this.disconnect = null
      ++this.epoch
      const sessions = await this.api.listSessions()
      this.update({ ...emptyView(), sessions, notice: "Session deleted" })
      if (sessions.length) await this.selectSession(sessions[0].id)
      else await this.newSession()
      return
    }
    if (cmd.type === "rename") {
      const result = await this.api.renameSession(id, cmd.title)
      this.update({ ...this.view, title: result.title,
        sessions: this.view.sessions.map((s) => s.id === id ? { ...s, title: result.title } : s),
        notice: "Session renamed" })
      return
    }
    if (cmd.type === "pin") {
      const { pinned } = await this.api.togglePin(id)
      this.update({ ...this.view,
        sessions: this.view.sessions.map((s) => s.id === id ? { ...s, pinned } : s),
        notice: pinned ? "Session pinned" : "Session unpinned" })
      return
    }
    if (this.view.busy) { this.note("Wait for the current turn to finish."); return }
    if (cmd.type === "fork") {
      const epoch = this.epoch
      const result = await this.api.fork(id, cmd.targetId)
      const history = await this.api.history(id)
      if (this.disposed || epoch !== this.epoch || id !== this.view.sessionId) return
      this.update({
        ...withHistory(this.view, history.messages),
        notice: "Forked from message " + result.cursor_id,
        busy: false,
        status: "idle",
      })
      return
    }
    if (cmd.type === "model") {
      await this.api.updateSessionConfig(id, { model_profile: cmd.profile })
      this.note("Model profile selected: " + cmd.profile)
    } else if (cmd.type === "plan" || cmd.type === "teams") {
      await this.api.updateSessionConfig(id, cmd.type === "plan"
        ? { plan_mode: cmd.enabled } : { enable_agent_teams: cmd.enabled })
      this.note(cmd.type + (cmd.enabled ? " enabled" : " disabled"))
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
      const cmd = parseTerminalCommand(content)
      if (cmd) {
        await this.command(cmd)
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
