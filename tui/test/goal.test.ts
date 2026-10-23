import { describe, expect, test } from "bun:test"
import { TuiController } from "../src/controller"
import { parseTerminalCommand } from "../src/commands"
import { emptyView } from "../src/state/reducer"
import type { AgentClient } from "../src/api/client"

const goal = {
  session_id: "s1",
  objective: "Ship it",
  status: "active" as const,
  rounds_started: 1,
  max_rounds: 8,
}

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

describe("goal command parsing", () => {
  test("show, set and clear", () => {
    expect(parseTerminalCommand("/goal")).toEqual({ type: "goal", action: "show", objective: "" })
    expect(parseTerminalCommand("/goal set Ship the migration")).toEqual({
      type: "goal", action: "set", objective: "Ship the migration",
    })
    expect(parseTerminalCommand("/goal clear")).toEqual({ type: "goal", action: "clear", objective: "" })
  })

  test("rejects malformed arguments", () => {
    expect(parseTerminalCommand("/goal set   ")?.type).toBe("invalid")
    expect(parseTerminalCommand("/goal nope")?.type).toBe("invalid")
  })
})

describe("goal controller", () => {
  test("shows the current goal with its budget", async () => {
    const c = controller({ goal: async () => ({ goal }) })
    await run(c, "/goal")
    expect(c.snapshot.notice).toContain("Goal [active] Ship it")
    expect(c.snapshot.notice).toContain("rounds 1/8")
  })

  test("reports when no goal is set", async () => {
    const c = controller({ goal: async () => ({ goal: null }) })
    await run(c, "/goal")
    expect(c.snapshot.notice).toContain("No goal for this session")
  })

  test("sets and clears through the API", async () => {
    const calls: string[] = []
    const c = controller({
      setGoal: async (_id: string, objective: string) => {
        calls.push("set " + objective)
        return { goal }
      },
      clearGoal: async () => {
        calls.push("clear")
        return { cleared: true }
      },
    })

    await run(c, "/goal set Ship it")
    expect(c.snapshot.notice).toContain("Goal set: Ship it")
    await run(c, "/goal clear")
    expect(calls).toEqual(["set Ship it", "clear"])
    expect(c.snapshot.notice).toBe("Goal cleared.")
  })
})
