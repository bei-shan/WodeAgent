import { useKeyboard, useRenderer, useTerminalDimensions } from "@opentui/solid"
import { createSignal, onCleanup, onMount, Show } from "solid-js"
import { AgentClient } from "../api/client"
import { TuiController } from "../controller"
import { ConversationPanel } from "./ConversationPanel"
import { SessionSidebar } from "./SessionSidebar"
import { Composer } from "./Composer"

export function App(props: { client: AgentClient }) {
  const controller = new TuiController(props.client)
  const [view, setView] = createSignal(controller.snapshot)
  const [draft, setDraft] = createSignal("")
  const renderer = useRenderer()
  const size = useTerminalDimensions()
  const unsubscribe = controller.subscribe(setView)

  onMount(() => { void controller.start() })
  onCleanup(() => { unsubscribe(); controller.close() })

  const submit = (value: string) => {
    const input = value.trim()
    if (!input) return
    setDraft("")
    void controller.submit(input)
  }
  useKeyboard((key) => {
    if (key.ctrl && key.name === "n") {
      void controller.newSession()
    } else if (key.ctrl && key.name === "c") {
      if (view().busy) void controller.interrupt()
      else renderer.destroy()
    }
  })

  return (
    <box flexDirection="column" width="100%" height="100%" backgroundColor="#10141d">
      <box height={3} flexDirection="row" justifyContent="space-between"
        paddingLeft={2} paddingRight={2} alignItems="center" backgroundColor="#1c2637">
        <text fg="#75baff">Ridge · TUI v2</text>
        <text fg="#aebbd0">
          {view().connection + " · " + view().status + " · " + (view().sessionId?.slice(0, 8) ?? "no session")}
        </text>
      </box>
      <box flexDirection="row" flexGrow={1} minHeight={0}>
        <Show when={size().width >= 78}>
          <SessionSidebar view={view()} onSelect={(id) => void controller.selectSession(id)} />
        </Show>
        <box flexGrow={1} minWidth={0} flexDirection="column" paddingLeft={1} paddingRight={1}>
          <box height={2} flexShrink={0} flexDirection="row" justifyContent="space-between">
            <text fg="#d9e4f8">{view().title || "No session"}</text>
            <text fg="#8fa2c1">{view().busy ? "● Agent working" : "Ready"}</text>
          </box>
          <ConversationPanel view={view()} />
          <Composer view={view()} value={draft()} onChange={setDraft} onSubmit={submit} />
        </box>
      </box>
    </box>
  )
}
