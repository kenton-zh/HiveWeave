/**
 * HiveWeave — Electron preload script
 *
 * Exposes a safe IPC bridge to the renderer process via contextBridge.
 * The renderer accesses this through window.electronAPI.
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  isElectron: true,
  selectFolder: () => ipcRenderer.invoke("select-folder"),
});
