/** Type declarations for Electron IPC bridge exposed via preload script. */
interface ElectronAPI {
  isElectron: true;
  selectFolder: () => Promise<string | null>;
}

interface Window {
  electronAPI?: ElectronAPI;
}
