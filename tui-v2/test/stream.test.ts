import { describe, expect, test } from "bun:test"
import { decodeEvent } from "../src/protocol"
import { Cursor, streamUrl } from "../src/api/stream"

describe("event transport contract", () => {
  test("decodes only well-shaped JSON events", () => {
    expect(decodeEvent("broken")).toBeNull()
    expect(decodeEvent('{"type":"x","payload":{},"seq":-1}')).toBeNull()
    expect(decodeEvent('{"type":"x","payload":[]}')).toBeNull()
    expect(decodeEvent('{"type":"x","payload":{"ok":true},"seq":1}')).toMatchObject({ type: "x", seq: 1 })
  })
  test("per-session cursor deduplicates replay and honors reset", () => {
    const cursor = new Cursor(0)
    const event = { type: "llm.delta", payload: {}, seq: 1, step: 1 }
    expect(cursor.accept(event)).toBe(true)
    expect(cursor.accept(event)).toBe(false)
    expect(cursor.accept({ type: "stream.reset", payload: { resume_from: 7 }, step: 0 })).toBe(true)
    expect(cursor.lastSeq).toBe(7)
    expect(cursor.accept({ ...event, seq: 6 })).toBe(false)
    expect(cursor.accept({ ...event, seq: 8 })).toBe(true)
  })
  test("uses WebSocket transport on the same API origin", () => {
    expect(streamUrl("http://127.0.0.1:8000", "session-1", 3)).toBe("ws://127.0.0.1:8000/api/sessions/session-1/stream?after=3")
  })
})
