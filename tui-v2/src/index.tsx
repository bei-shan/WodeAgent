import { render, useKeyboard, useRenderer, useTerminalDimensions } from "@opentui/solid"
import { createSignal, For, onCleanup, onMount, Show } from "solid-js"
import { AgentClient } from "./api/client"
import { TuiController } from "./controller"

const api = new AgentClient(process.env.WODEAGENT_SERVER || "http://127.0.0.1:8000")

function App() {
  const controller = new TuiController(api)
  const [view, setView] = createSignal(controller.snapshot)
  const [draft, setDraft] = createSignal("")
  const renderer = useRenderer()
  const size = useTerminalDimensions()
  const unsubscribe = controller.subscribe(setView)

  onMount(() => { void controller.start() })
  onCleanup(() => { unsubscribe(); controller.close() })

  const submit = (value: string) => {
    if (!value.trim()) return
    setDraft("")
    if (value.trim() === "/new" && !view().pending) {
      void controller.newSession()
      return
    }
    void controller.submit(value)
  }

  useKeyboard((key) => {
    if (key.ctrl && key.name === "n") {
      void controller.newSession()
      return
    }
    if (key.ctrl && key.name === "c") {
      if (view().busy) void controller.interrupt()
      else renderer.destroy()
    }
  })

  return (
    <box flexDirection="column" width="100%" height="100%" backgroundColor="#10141d">
      <box height={3} flexDirection="row" justifyContent="space-between" paddingLeft={2} paddingRight={2}
        alignItems="center" backgroundColor="#1c2637">
        <text fg="#75baff">WodeAgent · TUI v2</text>
        <text fg="#aebbd0">
          {view().connection + " · " + view().status + " · " + (view().sessionId?.slice(0, 8) ?? "no session")}
        </text>
      </box>
      <box flexDirection="row" flexGrow={1} minHeight={0}>
        <Show when={size().width >= 78}>
          <box width={23} flexShrink={0} paddingLeft={1} paddingTop={1} border borderColor="#344155"
            flexDirection="column" gap={1}>
            <text fg="#8ad1ff">Sessions   Ctrl+N new</text>
            <For each={view().sessions.slice(0, 15)}>
              {(session) => (
                <box onMouseUp={() => { void controller.selectSession(session.id) }} paddingLeft={1}>
                  <text fg={session.id === view().sessionId ? "#7cdda1" : "#bac7da"}>
                    {(session.id === view().sessionId ? "> " : "  ") + (session.title || session.id.slice(0, 8)).slice(0, 17)}
                  </text>
                </box>
              )}
            </For>
          </box>
        </Show>
        <box flexGrow={1} minWidth={0} flexDirection="column" paddingLeft={1} paddingRight={1}>
          <box height={2} flexShrink={0} flexDirection="row" justifyContent="space-between">
            <text fg="#d9e4f8">{view().title || "No session"}</text>
            <text fg="#8fa2c1">{view().busy ? "● Agent working" : "Ready"}</text>
          </box>
          <scrollbox flexGrow={1} minHeight={0} width="100%" paddingLeft={1}>
            <For each={view().messages.slice(-80)}>
              {(message) => (
                <box flexDirection="column" marginBottom={1}>
                  <text fg={message.role === "user" ? "#7cdda1" : "#7eafff"}>
                    {message.role === "user" ? "You" : (message.streaming ? "Agent …" : "Agent")}
                  </text>
                  <text fg="#e3eaf6">{message.content}</text>
                </box>
              )}
            </For>
            <For each={view().tools.slice(-8)}>
              {(tool) => (
                <text fg={tool.status === "error" ? "#ff8484" : "#a9b9cd"}>
                  {"⚙ " + tool.name + " · " + tool.status}
                </text>
              )}
            </For>
          </scrollbox>
          <Show when={view().subagent}>
            <box height={1}><text fg="#c09ef0">{view().subagent}</text></box>
          </Show>
          <Show when={view().notice}>
            <box height={1}><text fg="#f3bb6e">{view().notice}</text></box>
          </Show>
          <Show when={view().pending}>
            {(pending) => (
              <box border borderColor="#c19a58" padding={1} flexDirection="column">
                <text fg="#ffd58a">
                  {pending().kind === "permission"
                    ? "Permission required · " + pending().tool + " · " + pending().path
                    : "Agent asks · " + pending().question}
                </text>
                <text fg="#bac7da">
                  {pending().kind === "permission" ? "Type y to allow or n to deny" : "Type your answer below"}
                </text>
              </box>
            )}
          </Show>
          <box height={3} paddingLeft={1} paddingRight={1} border borderColor="#445b78" flexDirection="column">
            <input
              focused
              value={draft()}
              placeholder={view().pending ? "Respond to request…" : "Ask the agent…  /new creates a session"}
              onInput={(value) => setDraft(value)}
              onSubmit={submit}
              width="100%"
            />
            <text fg="#70819c">Enter send · Ctrl+N new · Ctrl+C interrupt / exit</text>
          </box>
        </box>
      </box>
    </box>
  )
}

await render(App)
