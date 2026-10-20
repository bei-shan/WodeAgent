import { describe, expect, test } from "bun:test"
import { TuiController } from "../src/controller"
import type { AgentClient } from "../src/api/client"

class MockSocket {
  static instances: MockSocket[] = []
  readonly url: string
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((message: { data: string }) => void) | null = null
  constructor(url: string) {
    this.url = url
    MockSocket.instances.push(this)
  }
  close() { this.onclose?.() }
  deliver(type: string, payload: Record<string, unknown>, seq: number) {
    this.onmessage?.({ data: JSON.stringify({ type, payload, seq, step: 1 }) })
  }
}

describe("session command orchestration", () => {
  test("starts a fresh session, streams, deduplicates and resolves permission", async () => {
    const oldSocket = globalThis.WebSocket
    MockSocket.instances = []
    globalThis.WebSocket = MockSocket as unknown as typeof WebSocket
    const calls: string[] = []
    const fake = {
      baseUrl: "http://127.0.0.1:8000",
      health: async () => ({ status: "ok" }),
      listSessions: async () => [{ id: "s1", title: "Demo", busy: false }],
      createSession: async () => {
        calls.push("create")
        return { id: "s1", title: "Demo", busy: false }
      },
      activateSession: async (id: string) => {
        calls.push("activate " + id)
        return { status: "active" }
      },
      history: async () => ({ messages: [] }),
      turn: async () => ({ status: "idle" as const }),
      send: async (_id: string, content: string) => {
        calls.push("send " + content)
        return { status: "accepted" }
      },
      permission: async (_sid: string, _rid: string, allow: boolean) => {
        calls.push("permission " + allow)
        return { status: "resolved" }
      },
      interrupt: async () => {
        calls.push("interrupt")
        return { status: "requested" }
      },
    }
    const controller = new TuiController(fake as unknown as AgentClient)
    try {
      await controller.start()
      expect(controller.snapshot.sessionId).toBe("s1")
      expect(MockSocket.instances).toHaveLength(1)
      const socket = MockSocket.instances[0]
      expect(socket.url).toBe("ws://127.0.0.1:8000/api/sessions/s1/stream")
      await controller.submit("hello")
      expect(calls).toContain("send hello")
      expect(controller.snapshot.busy).toBe(true)
      socket.deliver("llm.delta", { kind: "content", text: "Hi" }, 1)
      socket.deliver("llm.delta", { kind: "content", text: "Hi" }, 1)
      expect(controller.snapshot.messages.at(-1)?.content).toBe("Hi")
      socket.deliver("permission.requested", {
        request_id: "p1", tool: "Bash", path: "/tmp", action: "run",
      }, 2)
      expect(controller.snapshot.pending?.kind).toBe("permission")
      await controller.submit("yes")
      expect(calls).toContain("permission true")
      expect(controller.snapshot.pending).toBeNull()
      await controller.interrupt()
      expect(calls).toContain("interrupt")
      socket.deliver("turn.finished", { status: "cancelled", response: "Hi" }, 3)
      expect(controller.snapshot.busy).toBe(false)
      expect(controller.snapshot.status).toBe("cancelled")
    } finally {
      controller.close()
      globalThis.WebSocket = oldSocket
    }
  })

  test("new session streams from sequence zero to cover connection races", async () => {
    const oldSocket = globalThis.WebSocket
    MockSocket.instances = []
    globalThis.WebSocket = MockSocket as unknown as typeof WebSocket
    const fake = {
      baseUrl: "http://127.0.0.1:8000",
      health: async () => ({ status: "ok" }),
      listSessions: async () => [],
      createSession: async () => ({ id: "new", title: "New", busy: false }),
      activateSession: async () => ({ status: "active" }),
      history: async () => ({ messages: [] }),
      turn: async () => ({ status: "idle" as const }),
    }
    const controller = new TuiController(fake as unknown as AgentClient)
    try {
      await controller.start()
      expect(MockSocket.instances[0].url).toEndWith("/api/sessions/new/stream?after=0")
    } finally {
      controller.close()
      globalThis.WebSocket = oldSocket
    }
  })
})
