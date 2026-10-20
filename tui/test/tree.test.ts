import { describe, expect, test } from "bun:test"
import { parseTerminalCommand } from "../src/commands"
import { formatSessionTree } from "../src/tree"
import { TuiController } from "../src/controller"
import type { AgentClient, SessionTree } from "../src/api/client"

const TREE: SessionTree = {
  nodes: {
    a1b2c3d4: { id: "a1b2c3d4", type: "message", role: "user", parentId: null, content: "original task" },
    b1b2c3d4: { id: "b1b2c3d4", type: "message", role: "assistant", parentId: "a1b2c3d4", content: "old answer" },
  },
  children: { __root__: ["a1b2c3d4"], a1b2c3d4: ["b1b2c3d4"] },
  cursor_id: "a1b2c3d4",
  labels: {},
}

describe("terminal session tree and fork commands", () => {
  test("safe parser and bounded node previews", () => {
    expect(parseTerminalCommand("/tree")).toEqual({ type: "tree" })
    expect(parseTerminalCommand("/fork a1b2c3d4")).toEqual({ type: "fork", targetId: "a1b2c3d4" })
    expect(parseTerminalCommand("/fork ../etc")).toMatchObject({ type: "invalid" })
    const text = formatSessionTree(TREE)
    expect(text).toContain("a1b2c3d4")
    expect(text).toContain("b1b2c3d4")
    expect(text).toContain("Use /fork")
    expect(formatSessionTree(TREE, 1)).toContain("more nodes")
  })

  test("controller forks only through API and reloads chosen history", async () => {
    const calls: string[] = []
    const fake = {
      tree: async () => { calls.push("tree"); return TREE },
      fork: async (_id: string, node: string) => {
        calls.push("fork:" + node)
        return { cursor_id: node }
      },
      history: async () => {
        calls.push("history")
        return { messages: [{ role: "user", content: "original task" }] }
      },
      send: async () => { calls.push("send"); return { status: "accepted" } },
    }
    const c = new TuiController(fake as unknown as AgentClient)
    ;(c as unknown as { view: unknown }).view = {
      ...c.snapshot, sessionId: "abc", title: "a", busy: false,
      messages: [{ id: "old", role: "assistant", content: "old answer" }],
    }
    try {
      await c.submit("/tree")
      expect(c.snapshot.notice).toContain("a1b2c3d4")
      await c.submit("/fork a1b2c3d4")
      expect(c.snapshot.notice).toContain("Forked from message")
      expect(c.snapshot.messages).toHaveLength(1)
      expect(c.snapshot.messages[0].content).toBe("original task")
      expect(calls).toEqual(["tree", "fork:a1b2c3d4", "history"])
      await c.submit("/fork invalid")
      expect(c.snapshot.notice).toContain("Usage:")
      expect(calls).not.toContain("send")
    } finally {
      c.close()
    }
  })
})
