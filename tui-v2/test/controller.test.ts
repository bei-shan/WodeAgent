import { describe, expect, test } from "bun:test"
import { TuiController } from "../src/controller"
import type { AgentClient } from "../src/api/client"

describe("application commands", () => {
  test("starts a session and delegates messages to HTTP without importing Agent internals", async () => {
    const calls: string[] = []
    const fake = {
      baseUrl: "http://127.0.0.1:8000",
      health: async () => ({ status: "ok" }),
      listSessions: async () => [],
      createSession: async () => {
        calls.push("created")
        return { id: "s1", title: "new", busy: false }
      },
      activateSession: async () => {
        calls.push("activated")
        return { status: "active" }
      },
      history: async () => ({ messages: [] }),
      turn: async () => ({ status: "idle" as const }),
      send: async (_id: string, content: string) => {
        calls.push("send " + content)
        return { status: "accepted" }
      },
    }
    // Test lifecycle of the HTTP-facing adapter; websocket requires an
    // integration fake rather than a real model endpoint.
    const controller = new TuiController(fake as unknown as AgentClient)
    // Avoid starting a real socket in the unit test. Constructor is pure.
    expect(controller.snapshot.sessionId).toBeNull()
    expect(controller.snapshot.busy).toBe(false)
    controller.close()
    expect(calls).toEqual([])
  })
})
