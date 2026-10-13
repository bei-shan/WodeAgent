import { describe, expect, test } from "bun:test"
import { emptyView, projectEvent, withHistory } from "../src/state/reducer"

describe("session event projection", () => {
  test("replays deltas, tools and terminal outcome exactly once", () => {
    let state = emptyView()
    state = projectEvent(state, { type: "run.started", payload: {}, seq: 1, step: 0 })
    state = projectEvent(state, { type: "llm.delta", payload: { kind: "content", text: "Hel" }, seq: 2, step: 1 })
    state = projectEvent(state, { type: "llm.delta", payload: { kind: "content", text: "lo" }, seq: 3, step: 1 })
    state = projectEvent(state, { type: "llm.delta", payload: { kind: "content", text: "lo" }, seq: 3, step: 1 })
    expect(state.messages[0].content).toBe("Hello")
    state = projectEvent(state, { type: "tool.started", payload: { tool: "Read", tool_call_id: "abc" }, seq: 4, step: 1 })
    state = projectEvent(state, { type: "tool.completed", payload: { tool_call_id: "abc", status: "success" }, seq: 5, step: 1 })
    expect(state.tools[0].status).toBe("success")
    state = projectEvent(state, { type: "turn.finished", payload: { status: "completed", response: "Hello" }, seq: 6, step: 1 })
    expect(state.busy).toBe(false)
    expect(state.status).toBe("completed")
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].streaming).toBe(false)
  })

  test("permission, cursor reset, history and failures", () => {
    let state = projectEvent(emptyView(), { type: "permission.requested",
      payload: { request_id: "r", tool: "Bash", path: ".", action: "run" }, step: 0, seq: 1 })
    expect(state.pending?.kind).toBe("permission")
    state = projectEvent(state, { type: "stream.reset", payload: { resume_from: 4 }, step: 0 })
    expect(state.needsResync).toBe(true)
    expect(state.lastSeq).toBe(4)
    state = withHistory(state, [{ role: "assistant", content: "saved" }])
    expect(state.needsResync).toBe(false)
    expect(state.messages[0].content).toBe("saved")
    state = projectEvent(state, { type: "turn.finished",
      payload: { status: "failed", error_kind: "network" }, step: 0, seq: 5 })
    expect(state.status).toBe("failed")
    expect(state.notice).toContain("network")
  })

  test("final answer without token streaming", () => {
    const state = projectEvent(emptyView(), { type: "assistant.final",
      payload: { content: "Answer" }, step: 1, seq: 1 })
    expect(state.messages.map((m) => m.content)).toEqual(["Answer"])
  })
})
