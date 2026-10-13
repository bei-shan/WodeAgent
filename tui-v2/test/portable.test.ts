import { describe, expect, test } from "bun:test"
import { mkdtemp, readFile, stat, symlink, writeFile, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { MAX_ARCHIVE_BYTES, loadArchiveFile, saveArchiveFile } from "../src/localArchive"
import { parseTerminalCommand } from "../src/commands"
import { TuiController } from "../src/controller"
import type { AgentClient, PortableSession } from "../src/api/client"

const demo: PortableSession = {
  format: "ridgecode.portable-session", version: 1,
  title: "Demo", cursor_id: "a1b2c3d4",
  messages: [{ id: "a1b2c3d4", parent_id: null, role: "user", content: "hello" }],
  labels: {},
}

describe("portable archive commands", () => {
  test("parse explicit file commands and reject missing path", () => {
    expect(parseTerminalCommand("/export /tmp/demo.json")).toEqual({
      type: "export", path: "/tmp/demo.json",
    })
    expect(parseTerminalCommand("/import file with spaces.json")).toEqual({
      type: "import", path: "file with spaces.json",
    })
    expect(parseTerminalCommand("/export")).toMatchObject({ type: "invalid" })
    expect(parseTerminalCommand("/import")).toMatchObject({ type: "invalid" })
  })

  test("local archives are restricted, do not overwrite, reject oversized and symlinked inputs", async () => {
    const dir = await mkdtemp(join(tmpdir(), "ridge-portable-"))
    const file = join(dir, "session.json")
    try {
      await saveArchiveFile(file, demo)
      expect(JSON.parse(await readFile(file, "utf8"))).toEqual(demo)
      expect((await stat(file)).mode & 0o777).toBe(0o600)
      await expect(saveArchiveFile(file, demo)).rejects.toThrow()
      expect(await loadArchiveFile(file)).toEqual(demo)

      const oversized = join(dir, "big.json")
      await writeFile(oversized, Buffer.alloc(MAX_ARCHIVE_BYTES + 1, 32))
      await expect(loadArchiveFile(oversized)).rejects.toThrow("exceeds 1 MiB")
      const link = join(dir, "link.json")
      await symlink(file, link)
      await expect(loadArchiveFile(link)).rejects.toThrow()
      await expect(saveArchiveFile(link, demo)).rejects.toThrow()
    } finally {
      await rm(dir, { recursive: true, force: true })
    }
  })

  test("controller uses export HTTP and local file, then creates a new imported session", async () => {
    const dir = await mkdtemp(join(tmpdir(), "ridge-command-"))
    const path = join(dir, "portable.json")
    const calls: string[] = []
    const fake = {
      exportPortableSession: async (sid: string) => { calls.push("export:" + sid); return demo },
      importPortableSession: async (payload: unknown) => {
        expect(payload).toEqual(demo)
        calls.push("import")
        return { id: "new-session", title: "Demo", busy: false }
      },
      listSessions: async () => [{ id: "new-session", title: "Demo", busy: false }],
      send: async () => { calls.push("send") },
    }
    const c = new TuiController(fake as unknown as AgentClient)
    ;(c as unknown as { view: unknown }).view = {
      ...c.snapshot, sessionId: "original", title: "old",
    }
    c.selectSession = async (id: string) => {
      ;(c as unknown as { view: unknown }).view = { ...c.snapshot, sessionId: id }
    }
    try {
      await c.submit("/export " + path)
      expect(c.snapshot.notice).toContain("Exported")
      await c.submit("/import " + path)
      expect(c.snapshot.sessionId).toBe("new-session")
      expect(c.snapshot.notice).toContain("Imported")
      expect(calls).toEqual(["export:original", "import"])
    } finally {
      c.close()
      await rm(dir, { recursive: true, force: true })
    }
  })
})
