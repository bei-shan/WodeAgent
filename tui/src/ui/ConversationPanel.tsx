import { For } from "solid-js"
import type { SessionView } from "../state/reducer"

/** Pure presentation: this module cannot send HTTP requests or execute tools. */
export function ConversationPanel(props: { view: SessionView }) {
  return (
    <box flexDirection="column" flexGrow={1} minHeight={0} width="100%">
      <scrollbox flexGrow={1} minHeight={0} width="100%" paddingLeft={1}>
        <For each={props.view.messages.slice(-80)} fallback={<text fg="#70819c">Start a conversation with the Agent.</text>}>
          {(message) => (
            <box flexDirection="column" marginBottom={1}>
              <text fg={message.role === "user" ? "#7cdda1" : "#7eafff"}>
                {message.role === "user" ? "You" : (message.streaming ? "Agent …" : "Agent")}
              </text>
              <text fg="#e3eaf6">{message.content}</text>
            </box>
          )}
        </For>
        <For each={props.view.tools.slice(-8)} fallback={<text> </text>}>
          {(tool) => (
            <text fg={tool.status === "error" ? "#ff8484" : "#a9b9cd"}>
              {"⚙ " + tool.name + " · " + tool.status}
            </text>
          )}
        </For>
      </scrollbox>
      <box height={1}><text fg="#c09ef0">{props.view.subagent || " "}</text></box>
      <box flexDirection="column"
        height={Math.min(6, Math.max(1, props.view.notice.split("\n").length))}>
        <For each={props.view.notice.split("\n").slice(0, 6)}>
          {(line) => <text fg="#f3bb6e">{line || " "}</text>}
        </For>
      </box>
    </box>
  )
}
