import { Show } from "solid-js"
import type { SessionView } from "../state/reducer"

export function Composer(props: {
  view: SessionView
  value: string
  onChange: (value: string) => void
  onSubmit: (value: string) => void
}) {
  return (
    <>
      <Show when={props.view.pending} keyed>
        {(pending) => (
          <box border borderColor="#c19a58" padding={1} flexDirection="column">
            <text fg="#ffd58a">
              {pending.kind === "permission"
                ? "Permission required · " + pending.tool + " · " + pending.path
                : "Agent asks · " + pending.question}
            </text>
            <text fg="#bac7da">
              {pending.kind === "permission" ? "Type y to allow or n to deny" : "Type your answer below"}
            </text>
          </box>
        )}
      </Show>
      <box height={3} paddingLeft={1} paddingRight={1}
        border borderColor="#445b78" flexDirection="column">
        <input
          focused
          value={props.value}
          placeholder={props.view.pending ? "Respond to request…" : "Ask the agent…  /help for commands"}
          onInput={props.onChange}
          onSubmit={() => props.onSubmit(props.value)}
          width="100%"
        />
        <text fg="#70819c">Enter send · Ctrl+N new · Ctrl+C interrupt / exit</text>
      </box>
    </>
  )
}
