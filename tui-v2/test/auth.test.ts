import { afterEach, describe, expect, test } from "bun:test"
import { AgentClient } from "../src/api/client"
import { connectStream } from "../src/api/stream"

const originalFetch = globalThis.fetch
const originalWebSocket = globalThis.WebSocket

afterEach(() => {
  globalThis.fetch = originalFetch
  globalThis.WebSocket = originalWebSocket
})

describe("TUI local bearer authentication", () => {
  test("REST includes bearer header, never URL or body", async () => {
    let url = ""
    let headers: HeadersInit | undefined
    let requestBody: BodyInit | undefined
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      url = String(input)
      headers = init?.headers
      requestBody = init?.body ?? undefined
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) } as Response
    }) as typeof fetch
    const client = new AgentClient("http://127.0.0.1:8000", "test-very-secret-token")
    await client.send("s1", "hello")
    expect(new Headers(headers).get("authorization")).toBe("Bearer test-very-secret-token")
    expect(url).toEndWith("/api/sessions/s1/messages")
    expect(url).not.toContain("test-very-secret-token")
    expect(String(requestBody)).toBe('{"content":"hello"}')
  })

  test("remote cleartext with bearer token fails closed", () => {
    expect(() => new AgentClient("http://example.org:8000", "secret")).toThrow("requires HTTPS")
    expect(() => new AgentClient("https://example.org", "secret")).not.toThrow()
    expect(() => new AgentClient("http://127.0.0.1:8000", "secret")).not.toThrow()
  })

  test("WebSocket auth uses Bun headers, not URL; policy denial does not loop", () => {
    class MockSocket {
      url: string
      options?: { headers?: Record<string, string> }
      onopen: (() => void) | null = null
      onmessage: ((event: { data: string }) => void) | null = null
      onerror: (() => void) | null = null
      onclose: ((event: { code: number }) => void) | null = null
      constructor(url: string, options?: { headers?: Record<string, string> }) {
        this.url = url
        this.options = options
        sockets.push(this)
      }
      close() {}
    }
    const sockets: MockSocket[] = []
    globalThis.WebSocket = MockSocket as unknown as typeof WebSocket
    const states: string[] = []
    const disconnect = connectStream("http://127.0.0.1:8000", "s1", () => {}, (s) => states.push(s), 0, "ws-secret")
    try {
      expect(sockets).toHaveLength(1)
      expect(sockets[0].url).toBe("ws://127.0.0.1:8000/api/sessions/s1/stream?after=0")
      expect(sockets[0].options?.headers?.Authorization).toBe("Bearer ws-secret")
      expect(sockets[0].url).not.toContain("ws-secret")
      sockets[0].onclose?.({ code: 1008 })
      expect(states).toEqual(["connecting", "disconnected"])
    } finally {
      disconnect()
    }
  })
})
