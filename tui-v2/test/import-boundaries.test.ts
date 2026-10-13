import { expect, test } from "bun:test"

test("pure terminal components cannot import server runtime or network transports", async () => {
  const glob = new Bun.Glob("src/ui/**/*.{ts,tsx}")
  const violations: string[] = []
  for await (const path of glob.scan({ cwd: import.meta.dir + "/.." })) {
    if (path.endsWith("/App.tsx")) continue // composition root may construct controller
    const file = await Bun.file(import.meta.dir + "/../" + path).text()
    if (/from\s+["'][^"']*(?:\/api\/|\/controller|agents\/|core\/runtime)/.test(file) ||
      /(?:\bfetch\s*\(|\bWebSocket\b)/.test(file)) violations.push(path)
  }
  expect(violations).toEqual([])
})
