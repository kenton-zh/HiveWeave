import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { readdirSync, rmSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

/**
 * 从构建产物中剔除 office-assets 的美术中间产物。
 *
 * 为什么需要（2026-09-17 实测）：vite 的 `publicDir` 默认把 `public/` **整目录
 * 原样复制**到 `dist/`，**不读 .gitignore**。而 `public/office-assets/` 里为
 * 美术迭代方便留了 LaMa 修图的备份图与中间产物（`.gitignore` 的
 * 「Office scene working artifacts」节已排除，注释写明「working artifacts」）：
 *     *.bak.png  *.patched.png  *.pre-lama*.png  *-nofurniture.png
 * 实测 7 个文件 / **7.93 MB**，被 vite 复制进 dist 后又被 build-exe.bat 的
 * robocopy 打进 EXE 产物 —— 纯浪费（运行时零引用：`ASSET_URLS` 只指向无后缀
 * 生产图；全仓提到 `.bak` 名的是 `components/office/constants.ts` 里的**注释**
 * ——说明「原带桌版备份在哪个文件」——不是 URL）。
 *
 * ⚠ `*-nofurniture.png` 是 2026-09-24 补的第 5 条，加它的原因值得记住：
 *   当时这条命名**两侧都没登记**，实测它已经躺在
 *   `apps/desktop/dist/HiveWeave/web/office-assets/`（54634 B）里 ——
 *   正是本文件下面警告过的那个形态（既进 git 又进 EXE），且 bat 的出厂门禁
 *   （只列 `*.bak.png *.patched.png`）也放行了它。三处一起补齐的当天才闭合。
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

/**
 * 美术中间产物的文件名模式。
 *
 * ⚠⚠ **真值源是本表**（构建侧按它剔除 dist），`.gitignore` 的
 * 「Office scene working artifacts」节与它同源。
 * ⚠ 引用那边时说**节名**、别写行号 —— 两边都在长，行号必漂（2026-09-24 修）。
 * 2026-09-17 审计发现两处曾漂移：`.gitignore` 只列了 `.bak.png` /
 * `.patched.png`，而 `.pre-lama2.png`（无 `.bak` 那类）**只在构建侧拦** ⇒
 * 该文件既会进 git、又会被打进产物。
 * ⚠ 2026-09-24 复查发现：当年"已把 `.gitignore` 补齐为四条"的说法在
 *   `d557345`（09-23 美术资源迁至 `assets/art/office/working/`）之后**已不成立**
 *   —— 那四条被换成了一条目录规则，`patched` / `pre-lama` 遂重新只剩构建侧有。
 *   现已按本表逐条补齐为 5 条，且**不再写行号**（行号引用正是它上次静默失效的原因）。
 * **新增一条中间产物命名时，两处都要加**（漏一处 = 文件绕过对应那道防线）。
 *
 * 本表比 `.gitignore` 严格是**允许**的方向（多拦不会漏），但不同源本身
 * 是本仓反复栽的形态（「每处各列一份清单」）—— 故写死这条对应关系。
 */
const ARTIFACT_PATTERNS: RegExp[] = [
  /\.bak\.png$/i,
  /\.patched\.png$/i,
  /\.pre-lama2?\.png$/i,
  /\.pre-lama2?\.bak\.png$/i,
  // 拆分中间产物（2026-09-23 前台分层那轮引入）：原片去掉某器件后的**对照片**，
  // 运行时不用（只是让切分脚本的「两片 alpha 之和 == 原片」自检可复跑）。
  // 命名约定：`<原名>-nofurniture.png`。⚠ 本条是 2026-09-24 补的 —— 当时
  // `office-frontdesk-front-nofurniture.png` 已在 `public/` 下并且**两处都没登记**，
  // 正落在 09-17 审计描述的那个形态上（进 git 且同时进 EXE）。
  /-nofurniture\.png$/i,
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
