# Ridge Code 桌面壳（Electron）

窗口就是一个指向本地 FastAPI 的 `BrowserWindow`：后端用 `WEB_SERVE_FRONTEND=1` 同时托管构建好的 SPA，
所以壳里没有代理、没有 CORS、也不需要逐接口接线——Web 客户端的行为和 `python -m ridgecode web` 完全一致。

## 开发运行

```powershell
# 1. 先构建一次前端（壳会直接加载 dist）
cd desktop/web
npm ci
npm run build

# 2. 装壳的依赖（Electron 二进制较大）
cd ../shell
npm install

# 3. 启动
npm start
```

启动过程：选一个空闲端口 → 拉起 `python -m ridgecode serve --port <port>`
（优先用仓库里的 `venv`，可用 `RIDGECODE_PYTHON` 覆盖）→ 轮询 `/api/health` → 打开窗口。

关窗或退出应用会终止后端进程（Windows 上连子进程树一起结束）。

## 打包

```powershell
npm run dist:win   # Windows NSIS 安装包
npm run dist:mac   # macOS dmg（必须在 macOS 上执行）
```

产物在 `desktop/shell/release/`。当前 `build.files` 只包含 `main.js` 和 `package.json`，
前端通过 `extraResources` 把 `desktop/web/dist` 复制成 `resources/web-dist`。

### 还需要补的一步：Python sidecar

开发模式直接调用解释器；正式分发需要把后端打成一个可执行文件，放到
`resources/backend/ridgecode-backend(.exe)`，`main.js` 会自动优先使用它：

```powershell
pip install pyinstaller
pyinstaller --onefile --name ridgecode-backend `
  --collect-submodules core --collect-submodules desktop --collect-submodules tools `
  -m ridgecode.cli
```

打包时把产物加到 `build.extraResources`：

```json
{ "from": "dist-backend/ridgecode-backend.exe", "to": "backend/ridgecode-backend.exe" }
```

## 已知限制

- **从 Electron 应用内部启动**：如果当前环境设置了 `ELECTRON_RUN_AS_NODE=1`（例如在某些基于 Electron 的
  终端/工具里），Electron 会退化成纯 Node，`require("electron")` 拿不到 `app`。先清掉该变量再启动：
  `Remove-Item Env:ELECTRON_RUN_AS_NODE`。
- **签名**：Windows 未签名的 exe 会触发 SmartScreen；macOS 未签名/未公证的 dmg 需要用户右键打开。
  正式分发要有 Windows 代码签名证书和 Apple Developer 账号（公证用 `notarytool`）。
- **跨平台构建**：Windows 包必须在 Windows 上打，dmg 必须在 macOS 上打，CI 需要两套 runner。
- **数据目录**：当前沿用仓库工作区的 `memory/`、`.mycodeagent/`；正式桌面版应改到
  `%APPDATA%` / `~/Library/Application Support`（可用 `MYCODEAGENT_DATA_DIR` 覆盖）。
- **密钥**：现在仍读 `.env`；正式版应改用系统凭据库（`keyring`）。
- **自动更新**：未接入，需要 `electron-updater` 加发布渠道。
- **本地 API 无鉴权**：仅绑定 127.0.0.1，和现有 `serve` 行为一致；如需要可注入随机 `WEB_AUTH_TOKEN`。
