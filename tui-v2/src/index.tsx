import { render } from "@opentui/solid"
import { AgentClient } from "./api/client"
import { App } from "./ui/App"

const client = new AgentClient(process.env.RIDGECODE_SERVER || process.env.WODEAGENT_SERVER || "http://127.0.0.1:8000")
await render(() => <App client={client} />)
