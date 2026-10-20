import type { SessionTree } from "./api/client"

/** Stable, bounded terminal tree projection. IDs are accepted by /fork ID. */
export function formatSessionTree(tree: SessionTree, maxRows = 80): string {
  const nodes = Object.values(tree.nodes)
  if (!nodes.length) return "Empty session tree. Send a message first."
  const rows: string[] = []
  for (const node of nodes.slice(0, maxRows)) {
    let parent = node.parentId
    let depth = 0
    const visited = new Set<string>([node.id])
    while (parent && tree.nodes[parent] && !visited.has(parent) && depth < 8) {
      visited.add(parent)
      depth++
      parent = tree.nodes[parent].parentId
    }
    const selected = node.id === tree.cursor_id ? ">" : " "
    const label = tree.labels[node.id] ? " [" + tree.labels[node.id] + "]" : ""
    const preview = (node.content || node.summary || "").replace(/\s+/g, " ").slice(0, 50)
    rows.push(selected + " " + "  ".repeat(depth) + node.id + " " +
      (node.role || node.type) + label + (preview ? " · " + preview : ""))
  }
  if (nodes.length > maxRows) rows.push("… " + (nodes.length - maxRows) + " more nodes")
  rows.push("Use /fork NODE_ID to continue from an earlier message.")
  return rows.join("\n")
}
