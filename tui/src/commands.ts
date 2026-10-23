/** Terminal commands. Unknown slash commands must never reach the model. */
export type TerminalCommand =
  | { type: "help" | "new" | "sessions" | "models" | "pin" | "stop" | "tree" }
  | { type: "fork"; targetId: string }
  | { type: "export" | "import"; path: string }
  | { type: "use"; index: number }
  | { type: "rename"; title: string }
  | { type: "delete"; confirmed: boolean }
  | { type: "model"; profile: string }
  | { type: "plan" | "teams"; enabled: boolean }
  | { type: "goal"; action: "show" | "set" | "clear"; objective: string }
  | { type: "invalid"; reason: string }

export const TERMINAL_HELP = [
  "/help · /new · /sessions · /use N",
  "/rename TITLE · /pin · /delete confirm",
  "/models · /model PROFILE · /plan on|off · /teams on|off",
  "/tree · /fork NODE_ID (8 hex chars)",
  "/export FILE · /import FILE (portable conversation only)",
  "/goal · /goal set TEXT · /goal clear",
  "/stop · Ctrl+N new · Ctrl+C interrupt/exit",
].join("\n")

export function parseTerminalCommand(raw: string): TerminalCommand | null {
  const input = raw.trim()
  if (!input.startsWith("/")) return null
  const [name, ...tokens] = input.slice(1).split(/\s+/)
  const arg = tokens.join(" ").trim()
  switch (name.toLowerCase()) {
    case "help": return { type: "help" }
    case "new": return { type: "new" }
    case "sessions": return { type: "sessions" }
    case "models": return { type: "models" }
    case "tree": return { type: "tree" }
    case "fork":
      return /^[0-9a-f]{8}$/.test(arg)
        ? { type: "fork", targetId: arg }
        : { type: "invalid", reason: "Usage: /fork NODE_ID (8 lowercase hex chars)" }
    case "export":
    case "import":
      return arg && !arg.includes("\0")
        ? { type: name as "export" | "import", path: arg }
        : { type: "invalid", reason: "Usage: /" + name + " FILE" }
    case "pin": return { type: "pin" }
    case "stop": return { type: "stop" }
    case "use": {
      const index = Number(arg)
      return /^[1-9]\d*$/.test(arg) && Number.isSafeInteger(index)
        ? { type: "use", index }
        : { type: "invalid", reason: "Usage: /use N" }
    }
    case "rename":
      return arg ? { type: "rename", title: arg } : { type: "invalid", reason: "Usage: /rename TITLE" }
    case "delete":
      return { type: "delete", confirmed: arg === "confirm" }
    case "model":
      return arg ? { type: "model", profile: arg } : { type: "invalid", reason: "Usage: /model PROFILE" }
    case "plan":
    case "teams":
      return arg === "on" || arg === "off"
        ? { type: name as "plan" | "teams", enabled: arg === "on" }
        : { type: "invalid", reason: "Usage: /" + name + " on|off" }
    case "goal": {
      if (!arg) return { type: "goal", action: "show", objective: "" }
      if (arg === "clear") return { type: "goal", action: "clear", objective: "" }
      if (arg.startsWith("set ")) {
        const objective = arg.slice(4).trim()
        return objective
          ? { type: "goal", action: "set", objective }
          : { type: "invalid", reason: "Usage: /goal set TEXT" }
      }
      return { type: "invalid", reason: "Usage: /goal · /goal set TEXT · /goal clear" }
    }
    default: return { type: "invalid", reason: "Unknown command: /" + name + ". Use /help." }
  }
}
