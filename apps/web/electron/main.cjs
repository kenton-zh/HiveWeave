/**
 * HiveWeave — Electron main process
 *
 * Dev mode: loads from Vite dev server (localhost:5173).
 * No production build/packaging yet — just the native dialog shell.
 */
const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const path = require("path");
const fs = require("fs");

const DEV_URL = "http://localhost:5173";

function createWindow() {
  const win = new BrowserWindow({
    width: 1600,
    height: 1000,
    minWidth: 1024,
    minHeight: 700,
    title: "HiveWeave",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.loadURL(DEV_URL);
}

// ── IPC handlers ──────────────────────────────────────────────

ipcMain.handle("select-folder", async () => {
  const result = await dialog.showOpenDialog({
    properties: ["openDirectory"],
    title: "选择工作区目录",
  });
  if (result.canceled || result.filePaths.length === 0) return null;
  // Normalize to forward slashes for consistency with the backend
  return result.filePaths[0].replace(/\\/g, "/");
});

// ── App lifecycle ─────────────────────────────────────────────

// Packaged desktop: pin HIVEWEAVE_BROWSE_BIN to the agent-browser binary we
// ship under process.resourcesPath/agent-browser. This env only reaches the
// Python backend when the app spawns it as a child of this process; a
// standalone backend resolves its own path via resolve_browse_bin()'s
// ancestor-walk of the resources tree. Dev mode leaves the env unset so the
// backend falls back to node_modules/agent-browser (priority #3).
// Must match config.py `agent_browser_bin_name()` — the npm package only
// ships platform-suffixed names (agent-browser-win32-x64.exe, etc.).
function agentBrowserBinName() {
  if (process.platform === "win32") return "agent-browser-win32-x64.exe";
  if (process.platform === "darwin") {
    return process.arch === "arm64"
      ? "agent-browser-darwin-arm64"
      : "agent-browser-darwin-x64";
  }
  return process.arch === "arm64"
    ? "agent-browser-linux-arm64"
    : "agent-browser-linux-x64";
}

function injectAgentBrowserBin() {
  if (!app.isPackaged) return;
  const bin = path.join(
    process.resourcesPath,
    "agent-browser",
    agentBrowserBinName()
  );
  if (fs.existsSync(bin)) {
    process.env.HIVEWEAVE_BROWSE_BIN = bin;
  }
}

app.whenReady().then(() => {
  injectAgentBrowserBin();
  createWindow();
});

app.on("window-all-closed", () => {
  app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

// Packaging note: ship the native binary into the app resources, e.g.
// electron-builder extraResources (single platform file, not the whole bin/
// directory — that holds ~135MB across all 7 platform binaries):
//   {
//     "from": "node_modules/agent-browser/bin/agent-browser-win32-x64.exe",
//     "to": "agent-browser/agent-browser-win32-x64.exe"
//   }
