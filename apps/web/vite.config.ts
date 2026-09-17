import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { readdirSync, rmSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

/**
 * 从构建产物中剔除 office-assets 的美术中间产物。
 *
 * 为什么需要（2026-09-17 实测）：vite 的 `publicDir` 默认把 `public/` **整目录
 * 原样复制**到 `dist/`，**不读 .gitignore**。而 `public/office-assets/` 里为
 * 美术迭代方便留了 LaMa 修图的备份图与中间产物（`.gitignore:201-203` 已排除，
 * 注释写明「working artifacts」）：
 *     *.bak.png  *.patched.png  *.pre-lama*.bak.png
 * 实测 7 个文件 / **7.93 MB**，被 vite 复制进 dist 后又被 build-exe.bat 的
 * robocopy 打进 EXE 产物 —— 纯浪费（运行时零引用：`ASSET_URLS` 只指向无后缀
 * 生产图，全仓唯一提到 .bak 名的是 components/office/constants.ts:138 的注释）。
 *
 * ⚠ 为什么在构建侧剔除，而不是把这些文件从 public/ 挪走：
 *   它们在 public/ 是**有意留的**（美术迭代时要能就地看到上一版对比，DevServer
 *   下直接可访问）。把它们挪到别处会打断既有的修图工作流。故此处只**拦出口**，
 *   不动源头。
 *   ⚠ 但注意：这意味着 `pnpm dev`（DevServer）下这些中间产物**仍可访问**
 *     （vite serve 时 publicDir 是中间件、不走 closeBundle），这正是美术要的。
 *     只有 `pnpm build`（与 EXE 打包）才剔除。这是刻意的行为差异。
 *
 * 兜底说明：build-exe.bat 另有一道 `/XF` + 出厂门禁（2026-09-17 加）。
 * 两道是**不同层次**的防线，都需要：本插件保证 dist 干净（治本，dev 与 CI
 * 都受益）；bat 那道保证「即使 dist 被手工污染也不会出包」（治标，兜住
 * 绕过 vite 的路径）。去掉任何一道都会留下缺口。
 */

/** 美术中间产物的文件名模式（与 .gitignore:201-203 保持一致） */
const ARTIFACT_PATTERNS: RegExp[] = [
  /\.bak\.png$/i,
  /\.patched\.png$/i,
  /\.pre-lama2?\.png$/i,
  /\.pre-lama2?\.bak\.png$/i,
];

/** 需要清理的子目录（相对 outDir）。新增美术目录时在此登记。 */
const SCAN_SUBDIRS = ["office-assets", "sprites"];

function isArtifact(name: string): boolean {
  return ARTIFACT_PATTERNS.some((re) => re.test(name));
}

/**
 * 递归删除 outDir 下匹配 ARTIFACT_PATTERNS 的文件。
 * 返回被删文件的相对路径列表（供日志与断言使用）。
 */
function pruneArtifacts(outDirAbs: string): string[] {
  const removed: string[] = [];

  const walk = (dirAbs: string, relBase: string): void => {
    let entries: string[];
    try {
      entries = readdirSync(dirAbs);
    } catch {
      // 目录不存在（例如 sprites 未生成）⇒ 静默跳过，这不是错误
      return;
    }
    for (const name of entries) {
      const abs = join(dirAbs, name);
      const rel = relBase ? `${relBase}/${name}` : name;
      let isDir = false;
      try {
        isDir = statSync(abs).isDirectory();
      } catch {
        continue;
      }
      if (isDir) {
        walk(abs, rel);
        continue;
      }
      if (isArtifact(name)) {
        try {
          rmSync(abs, { force: true });
          removed.push(rel);
        } catch (err) {
          // 删不掉就 fail loud —— 静默放过会让 EXE 又悄悄变大 7.93MB，
          // 而那正是本插件要防的事。
          throw new Error(
            `[prune-artifacts] 删除失败: ${rel} (${(err as Error).message})`,
          );
        }
      }
    }
  };

  for (const sub of SCAN_SUBDIRS) {
    walk(join(outDirAbs, sub), sub);
  }
  return removed;
}

function pruneArtifactPlugin(): Plugin {
  let outDirAbs = "";
  return {
    name: "hiveweave:prune-office-artifacts",
    apply: "build", // 只在 build 阶段生效；dev 下不动 publicDir
    configResolved(cfg) {
      outDirAbs = resolve(cfg.root, cfg.build.outDir);
    },
    closeBundle() {
      // closeBundle 在 dist 写完（含 publicDir 复制）之后触发，
      // 是唯一能看到「被复制的中间产物」的时机。
      const removed = pruneArtifacts(outDirAbs);
      if (removed.length) {
        const mb = removed.length;
        console.log(
          `\n[hiveweave] 已从构建产物剔除 ${mb} 个美术中间产物：\n` +
            removed.map((r) => `  - ${r}`).join("\n"),
        );
      }
    },
  };
}

export default defineConfig({
  base: "./",
  plugins: [react(), pruneArtifactPlugin()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://localhost:4000",
    },
  },
  optimizeDeps: {
    include: [
      "react",
      "react-dom",
      "zustand",
      "phoenix",
      "@xyflow/react",
      "pixi.js",
    ],
  },
});
