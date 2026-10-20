import { describe, expect, test } from "bun:test"
import { parseTerminalCommand, TERMINAL_HELP } from "../src/commands"
import { TuiController } from "../src/controller"
import type { AgentClient } from "../src/api/client"

describe("TS terminal command grammar", () => {
  test("commands have unambiguous typed structures", () => {
    expect(parseTerminalCommand("  hello")).toBeNull()
    expect(parseTerminalCommand("/use 12")).toEqual({ type: "use", index: 12 })
    expect(parseTerminalCommand("/use nope")).toEqual({ type: "invalid", reason: "Usage: /use N" })
    expect(parseTerminalCommand("/rename Improved agent")).toEqual({ type: "rename", title: "Improved agent" })
    expect(parseTerminalCommand("/delete")).toEqual({ type: "delete", confirmed: false })
    expect(parseTerminalCommand("/delete confirm")).toEqual({ type: "delete", confirmed: true })
    expect(parseTerminalCommand("/plan on")).toEqual({ type: "plan", enabled: true })
    expect(parseTerminalCommand("/teams off")).toEqual({ type: "teams", enabled: false })
    expect(parseTerminalCommand("/model gpt-4")).toEqual({ type: "model", profile: "gpt-4" })
    expect(parseTerminalCommand("/unknown")).toMatchObject({ type: "invalid" })
    expect(TERMINAL_HELP).toContain("/sessions")
  })

  test("slash commands call only supported APIs; never send as prompt", async () => {
    const calls: string[] = []
    const fake = {
      baseUrl: "http://127.0.0.1:8000",
      listSessions: async () => [{ id: "s1", title: "Hello", pinned: false, busy: false }],
      renameSession: async (_id: string, title: string) => {
        calls.push("rename " + title)
        return { status: "renamed", title }
      },
      togglePin: async () => { calls.push("pin"); return { pinned: true } },
      updateSessionConfig: async (_id: string, patch: object) => {
        calls.push("config " + JSON.stringify(patch))
        return { status: "updated" }
      },
      models: async () => [{ name: "default", model: "gpt", provider: "openai" }],
      deleteSession: async () => { calls.push("delete"); return { status: "deleted" } },
      createSession: async () => { calls.push("new"); return { id: "s2", title: "New", busy: false } },
      activateSession: async () => ({ status: "ok" }),
      history: async () => ({ messages: [] }),
      turn: async () => ({ status: "idle" as const }),
      send: async () => { calls.push("send"); return { status: "accepted" } },
    }
    const c = new TuiController(fake as unknown as AgentClient)
    // Load a session directly, avoiding WebSocket subscriptions in this test.
    ;(c as unknown as { view: unknown }).view = {
      ...c.snapshot, sessionId: "s1", title: "Hello", sessions: [{ id: "s1", title: "Hello", busy: false }],
    }
    try {
      await c.submit("/rename Project review")
      expect(c.snapshot.title).toBe("Project review")
      await c.submit("/pin")
      expect(c.snapshot.sessions[0].pinned).toBe(true)
      await c.submit("/plan on")
      await c.submit("/teams off")
      await c.submit("/model default")
      await c.submit("/models")
      expect(c.snapshot.notice).toContain("default")
      await c.submit("/delete")
      expect(c.snapshot.notice).toContain("confirm")
      expect(calls).not.toContain("delete")
      await c.submit("/bogus")
      expect(c.snapshot.notice).toContain("Unknown command")
      expect(calls).toEqual([
        "rename Project review", "pin",
        'config {"plan_mode":true}',
        'config {"enable_agent_teams":false}',
        'config {"model_profile":"default"}',
      ])
    } finally {
      c.close()
    }
  })

  test("delete requires explicit confirmation", async () => {
    const calls: string[] = []
    const fake = {
      baseUrl: "http://127.0.0.1:8000",
      deleteSession: async () => { calls.push("delete"); return { status: "deleted" } },
      listSessions: async () => [],
      createSession: async () => { calls.push("new"); return { id: "s2", title: "New", busy: false } },
      activateSession: async () => ({ status: "active" }),
      history: async () => ({ messages: [] }),
      turn: async () => ({ status: "idle" }),
    }
    const originalWebSocket = globalThis.WebSocket
    class MockSocket {
      onopen = null
      onclose = null
      onerror = null
      onmessage = null
      constructor(_: string) {}
      close() {}
    }
    globalThis.WebSocket = MockSocket as unknown as typeof WebSocket
    const c = new TuiController(fake as unknown as AgentClient)
    ;(c as unknown as { view: unknown }).view = {
      ...c.snapshot, sessionId: "s1", title: "Demo", sessions: [{ id: "s1", title: "Demo" }],
    }
    try {
      await c.submit("/delete confirm")
      expect(calls).toEqual(["delete", "new"])
      expect(c.snapshot.sessionId).toBe("s2")
    } finally {
      c.close()
      globalThis.WebSocket = originalWebSocket
    }
  })
})
