import { For } from "solid-js"
import type { SessionView } from "../state/reducer"

export function SessionSidebar(props: { view: SessionView; onSelect: (id: string) => void }) {
  return (
    <box width={23} flexShrink={0} paddingLeft={1} paddingTop={1}
      border borderColor="#344155" flexDirection="column" gap={1}>
      <text fg="#8ad1ff">Sessions · /use N</text>
      <For each={props.view.sessions.slice(0, 15)}>
        {(session, index) => (
          <box onMouseUp={() => props.onSelect(session.id)} paddingLeft={1}>
            <text fg={session.id === props.view.sessionId ? "#7cdda1" : "#bac7da"}>
              {(index() + 1) + ". " + (session.title || session.id.slice(0, 8)).slice(0, 16)}
            </text>
          </box>
        )}
      </For>
    </box>
  )
}
