import { readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

// Load .env file if present — this module is imported first to ensure
// env vars are set before any other module reads process.env.
const __dirname = dirname(fileURLToPath(import.meta.url));
try {
  const envPath = join(__dirname, "..", ".env");
  for (const line of readFileSync(envPath, "utf-8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eqIdx = trimmed.indexOf("=");
    if (eqIdx > 0) {
      const key = trimmed.slice(0, eqIdx);
      const val = trimmed.slice(eqIdx + 1);
      if (!process.env[key]) process.env[key] = val;
    }
  }
} catch {
  // .env not found — that's fine, env vars come from the system
}
