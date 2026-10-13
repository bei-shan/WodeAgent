import { decodeEvent, type AgentEvent } from "../protocol"

export type ConnectionState = "connecting" | "connected" | "disconnected"

/** Sequence ID is scoped to a session, not shared across workspaces. */
export class Cursor {
  lastSeq: number | null
  constructor(after: number | null = null) { this.lastSeq = after }
  accept(event: AgentEvent): boolean {
    if (event.type === "stream.reset") {
      const next = event.payload.resume_from
      this.lastSeq = typeof next === "number" && Number.isSafeInteger(next) && next >= 0 ? next : null
      return true
    }
    if (event.seq === undefined) return true
    if (this.lastSeq !== null && event.seq <= this.lastSeq) return false
    this.lastSeq = event.seq
    return true
  }
}

export function streamUrl(base: string, sessionId: string, after: number | null): string {
  const url = new URL("/api/sessions/" + encodeURIComponent(sessionId) + "/stream", base)
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
  if (after !== null) url.searchParams.set("after", String(after))
  return url.toString()
}

/** One subscription owns its WebSocket, replay cursor and retry timer. */
export function connectStream(
  base: string,
  sessionId: string,
  onEvent: (event: AgentEvent) => void,
  onConnection: (state: ConnectionState) => void,
  after: number | null = null,
): () => void {
  let closed = false
  let socket: WebSocket | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  let attempts = 0
  const cursor = new Cursor(after)
  const open = () => {
    if (closed) return
    onConnection("connecting")
    const ws = new WebSocket(streamUrl(base, sessionId, cursor.lastSeq))
    socket = ws
    ws.onopen = () => {
      if (closed) return
      attempts = 0
      onConnection("connected")
    }
    ws.onmessage = (frame) => {
      if (closed) return
      const event = decodeEvent(frame.data)
      if (event && cursor.accept(event)) onEvent(event)
    }
    ws.onerror = () => ws.close()
    ws.onclose = () => {
      if (closed) return
      onConnection("disconnected")
      attempts++
      timer = setTimeout(open, Math.min(10_000, 500 * 2 ** Math.min(attempts, 5)))
    }
  }
  open()
  return () => {
    closed = true
    if (timer !== undefined) clearTimeout(timer)
    if (!socket) return
    socket.onopen = null
    socket.onmessage = null
    socket.onerror = null
    socket.onclose = null
    socket.close()
  }
}
