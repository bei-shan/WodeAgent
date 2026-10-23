"use strict"

// Ridge Code desktop shell.
//
// The window is a plain BrowserWindow pointed at the local FastAPI process,
// which also serves the built SPA (WEB_SERVE_FRONTEND=1). That keeps the shell
// free of proxies, CORS rules and per-API wiring: the Web client behaves
// exactly as it does under `python -m ridgecode web`.
//
// Development spawns `python -m ridgecode serve` from this checkout. A packaged
// build looks for a PyInstaller sidecar at resources/backend/ridgecode-backend.

const { app, BrowserWindow, dialog } = require("electron")
const { spawn } = require("node:child_process")
const fs = require("node:fs")
const http = require("node:http")
const net = require("node:net")
const path = require("node:path")

const REPO_ROOT = path.resolve(__dirname, "..", "..")
const HEALTH_TIMEOUT_MS = 40_000

let backend = null
let quitting = false

function isWindows() {
  return process.platform === "win32"
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.unref()
    server.on("error", reject)
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

function packagedBackend() {
  const name = isWindows() ? "ridgecode-backend.exe" : "ridgecode-backend"
  const candidate = path.join(process.resourcesPath, "backend", name)
  return fs.existsSync(candidate) ? candidate : null
}

function devPython() {
  if (process.env.RIDGECODE_PYTHON) return process.env.RIDGECODE_PYTHON
  const candidates = isWindows()
    ? [path.join(REPO_ROOT, "venv", "Scripts", "python.exe")]
    : [path.join(REPO_ROOT, "venv", "bin", "python"), path.join(REPO_ROOT, ".venv", "bin", "python")]
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate
  }
  return isWindows() ? "python" : "python3"
}

function backendCommand() {
  if (app.isPackaged) {
    const packaged = packagedBackend()
    if (packaged) return { command: packaged, args: ["serve"] }
  }
  return { command: devPython(), args: ["-m", "ridgecode", "serve"] }
}

function frontendDir() {
  if (app.isPackaged) {
    const packaged = path.join(process.resourcesPath, "web-dist")
    return fs.existsSync(path.join(packaged, "index.html")) ? packaged : null
  }
  const built = path.join(REPO_ROOT, "desktop", "web", "dist")
  return fs.existsSync(path.join(built, "index.html")) ? built : null
}

function waitForHealth(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    const probe = () => {
      const request = http.get(
        { host: "127.0.0.1", port, path: "/api/health", timeout: 2000 },
        (response) => {
          response.resume()
          if (response.statusCode === 200) resolve()
          else retry()
        },
      )
      request.on("timeout", () => request.destroy())
      request.on("error", () => retry())
    }
    const retry = () => {
      if (Date.now() > deadline) {
        reject(new Error("The local service did not report healthy in time."))
        return
      }
      setTimeout(probe, 400)
    }
    probe()
  })
}

function startBackend(port) {
  const { command, args } = backendCommand()
  const dist = frontendDir()
  const env = {
    ...process.env,
    WEB_SERVE_FRONTEND: "1",
    PYTHONIOENCODING: "utf-8",
    PYTHONUNBUFFERED: "1",
  }
  if (dist) env.WEB_FRONTEND_DIR = dist

  const child = spawn(command, [...args, "--port", String(port)], {
    cwd: REPO_ROOT,
    env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  })
  child.stdout.on("data", (chunk) => process.stdout.write(`[backend] ${chunk}`))
  child.stderr.on("data", (chunk) => process.stderr.write(`[backend] ${chunk}`))
  child.on("error", (error) => {
    if (!quitting) dialog.showErrorBox("Ridge Code", `Could not start the local service: ${error.message}`)
  })
  child.on("exit", (code, signal) => {
    if (quitting) return
    dialog.showErrorBox(
      "Ridge Code",
      `The local service stopped unexpectedly (code ${code ?? signal ?? "unknown"}).`,
    )
    app.quit()
  })
  return child
}

function stopBackend() {
  if (!backend || backend.pid === undefined) return
  const child = backend
  backend = null
  if (isWindows()) {
    // Kill the whole tree: uvicorn may have spawned helpers.
    spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore",
    })
  } else {
    child.kill("SIGTERM")
  }
}

async function createWindow() {
  if (!frontendDir()) {
    throw new Error(
      "The Web client is not built. Run `npm run build` in desktop/web first.",
    )
  }
  const port = await freePort()
  backend = startBackend(port)
  await waitForHealth(port, HEALTH_TIMEOUT_MS)

  const window = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: "#ffffff",
    title: "Ridge Code",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  window.removeMenu()
  await window.loadURL(`http://127.0.0.1:${port}/`)
  window.on("closed", () => stopBackend())
  return window
}

app.whenReady().then(async () => {
  try {
    await createWindow()
  } catch (error) {
    dialog.showErrorBox("Ridge Code failed to start", String(error?.message ?? error))
    app.quit()
  }
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow().catch((error) => {
        dialog.showErrorBox("Ridge Code failed to start", String(error?.message ?? error))
        app.quit()
      })
    }
  })
})

app.on("window-all-closed", () => app.quit())
app.on("before-quit", () => {
  quitting = true
  stopBackend()
})
