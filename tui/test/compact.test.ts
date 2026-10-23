import { describe, expect, test } from "bun:test"
import { TuiController } from "../src/controller"
import { parseTerminalCommand } from "../src/commands"
import { emptyView } from "../src/state/reducer"
import type { AgentClient } from "../src/api/client"

function controller(api: Record<string, unknown>): TuiController {
  const c = new TuiController(api as unknown as AgentClient)
  ;(c as unknown as { update: (view: unknown) => void }).update({ ...emptyView(), sessionId: "s1" })
  return c
}

async function run(c: TuiController, raw: string): Promise<void> {
  const cmd = parseTerminalCommand(raw)
  expect(cmd).not.toBeNull()
  await (c as unknown as { command: (cmd: unknown) => Promise<void> }).command(cmd)
}

describe("compact command", () => {
  test("parses without and with a focus", () => {
    expect(parseTerminalCommand("/compact")).toEqual({ type: "compact", focus: "" })
    expect(parseTerminalCommand("/compact keep API details")).toEqual({
      type: "compact", focus: "keep API details",
    })
  })

  test("passes the focus to the API", async () => {
    const calls: string[] = []
    const c = controller({
      compact: async (_id: string, focus: string) => {
        calls.push(focus)
        return { compacted: true, reason: "", summary_generated: true }
      },
    })
    await run(c, "/compact keep API details")
    expect(calls).toEqual(["keep API details"])
    expect(c.snapshot.notice).toContain("已压缩历史")
  })

  test("reports why nothing was compacted", async () => {
    const c = controller({
      compact: async () => ({ compacted: false, reason: "rounds_not_enough", summary_generated: false }),
    })
    await run(c, "/compact")
    expect(c.snapshot.notice).toContain("rounds_not_enough")
  })
})
