/** Explicit local-file I/O for portable user conversation archives only. */
import { constants } from "node:fs"
import { open, unlink } from "node:fs/promises"
import type { PortableSession } from "./api/client"

export const MAX_ARCHIVE_BYTES = 1_048_576

/** Never overwrite an existing user file or follow its final symlink. */
export async function saveArchiveFile(path: string, archive: PortableSession): Promise<void> {
  if (archive.format !== "ridgecode.portable-session" || archive.version !== 1) {
    throw new Error("Unsupported portable session format")
  }
  const bytes = Buffer.from(JSON.stringify(archive, null, 2) + "\n", "utf8")
  if (bytes.length > MAX_ARCHIVE_BYTES) throw new Error("Portable session exceeds 1 MiB")
  const flags = constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW ?? 0)
  const handle = await open(path, flags, 0o600)
  let written = false
  try {
    await handle.writeFile(bytes)
    await handle.sync()
    written = true
  } finally {
    await handle.close()
    if (!written) await unlink(path).catch(() => {})
  }
}

export async function loadArchiveFile(path: string): Promise<unknown> {
  const handle = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0))
  try {
    // Reading a fixed upper bound avoids allocating an arbitrarily large file.
    const bytes = Buffer.alloc(MAX_ARCHIVE_BYTES + 1)
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0)
    if (bytesRead > MAX_ARCHIVE_BYTES) throw new Error("Portable session exceeds 1 MiB")
    const raw = new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, bytesRead))
    return JSON.parse(raw) as unknown
  } finally {
    await handle.close()
  }
}
