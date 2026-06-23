/**
 * HiveWeave — Electron main process
 *
 * Dev mode: loads from Vite dev server (localhost:5173).
 * No production build/packaging yet — just the native dialog shell.
 */
const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const path = require("path");

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

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
