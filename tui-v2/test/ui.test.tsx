import { expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { ConversationPanel } from "../src/ui/ConversationPanel"
import { emptyView, withHistory } from "../src/state/reducer"

test("independent terminal conversation component renders saved messages", async () => {
  const view = withHistory(emptyView(), [
    { role: "user", content: "hello" },
    { role: "assistant", content: "world" },
  ])
  const setup = await testRender(() => <ConversationPanel view={view} />, { width: 80, height: 18 })
  try {
    await setup.renderOnce()
    const output = setup.captureCharFrame()
    expect(output).toContain("You")
    expect(output).toContain("hello")
    expect(output).toContain("Agent")
    expect(output).toContain("world")
  } finally {
    setup.renderer.destroy()
  }
})
