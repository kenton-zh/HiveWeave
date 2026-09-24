/**
 * 办公室视觉实测 —— 唯一权威截图入口（Playwright）。
 *
 * 为什么有它：`docs/前端设计规格.md` 曾把「视觉实测」标为 ⚠ 未做，理由是
 * 「Chrome DevTools MCP 与 Playwright 均不可用」（§19.3/§19.4）。2026-09-22 夜实测**证伪**：
 * `apps/web/package.json` devDeps 有 `playwright ^1.61.1`，`node_modules/playwright` 与
 * `%LOCALAPPDATA%\ms-playwright\chromium-12xx` 均在；headless Chromium 走
 * `--use-angle=swiftshader` 能正常出 PixiJS 画面。于是把这条验证路径固定成脚本。
 *
 * 用法：
 *   node scripts/screenshot-office.mjs                          # 全默认（base 5173，不切项目）
 *   node scripts/screenshot-office.mjs --project=TEST_DSH_47    # 先切项目再拍
 *   node scripts/screenshot-office.mjs --mock-agents=6          # 不依赖后端：mock /api/** 塞 6 个假 agent
 *   node scripts/screenshot-office.mjs --out=test-screenshots/x --strict
 *
 * 产出（--out 下）：01-full.png / 02-desks.png / 03-hud.png / 04-window.png / diag.json
 * 退出码：--strict 时若「无 canvas」「有 pageerror」或「疑似白屏」则 1；默认恒 0。
 *
 * ⚠ 两个前置坑（都踩过，写在这里省得再踩）：
 *   1. **改过 `tailwind.config.js` 必须重启 dev server。** Vite 会缓存 tailwind 配置，
 *      旧进程会让用到新令牌的 CSS 报 `does not exist in your theme config` 500 ⇒
 *      **整页白屏**，而 `pnpm build` / vitest 依然全绿（看不出来）。
 *   2. **默认项目可能一个 agent 都没有**（本机 meta DB 有 1106 个 pytest 残留项目，
 *      默认选中 `projects[0]`）。要看小人就得 `--project=<真实项目>`。
 */
import { chromium } from "playwright";
import path from "node:path";
import fs from "node:fs";

const arg = (name, dflt) => {
  const hit = process.argv.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : dflt;
};
const has = (name) => process.argv.includes(`--${name}`);

const BASE = arg("base", "http://127.0.0.1:5173").replace(/\/$/, "");
const PROJECT = arg("project", null);
const OUT = arg("out", "test-screenshots/office-latest");
const STRICT = has("strict");
/**
 * `--mock-agents=N`：用假数据把办公室填满，**不依赖后端**也能看角色与名牌。
 * 为什么需要：后端启动会被 meta DB 里的脏项目拖到 6 分钟以上（见本脚本顶部注释与
 * `docs/前端设计规格.md` 的"当前进展"表），而"角色落座/名牌是否重叠"这类**纯前端视觉**
 * 的东西本来就不该被后端卡住。mock 只拦 `/api/**` 的响应，不碰产品代码。
 */
const MOCK_AGENTS = Number(arg("mock-agents", 0)) || 0;
const MOCK_NAMES = ["归零", "天线", "验真", "棱镜", "潮汐", "快门", "折纸", "摆烂", "青岩"];
const MOCK_ROLES = ["ceo", "hr", "qa_lead", "Three.js技术负责人", "微缩小镇场景工程师", "test_engineer", "developer", "architect", "security_auditor"];

async function installMocks(page, n) {
  const agents = Array.from({ length: n }, (_, i) => ({
    id: `mock-agent-${i + 1}`,
    name: MOCK_NAMES[i % MOCK_NAMES.length],
    role: MOCK_ROLES[i % MOCK_ROLES.length],
    status: "active",
  }));  // 记下**实际被拦的 API 路径**：mock 形状猜错时（前端请求的接口与预期不同），
  // 这是唯一能看出"到底请求了什么"的证据，随 diag.json 落盘。
  page.__mockHits = [];
  // ⚠ 匹配用 **pathname 前缀**，不能用 `**/api/**` 这种子串模式：
  // Vite 的模块请求 `/src/api/rest.ts`、`/src/api.ts` 都含 `/api/`，被 glob 拦下后
  // 会以 `application/json` 返回脚本 ⇒ 浏览器报 MIME 错误 ⇒ **整页白屏**
  // （2026-09-23 实测踩到，与仓库"禁用文本子串判据"同族：子串会误伤同形路径）。
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    (route) => {
      const url = route.request().url();
      try { page.__mockHits.push(new URL(url).pathname + new URL(url).search); } catch { /* ignore */ }
      const json = (body) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      if (/\/api\/projects(\?|$)/.test(url)) {
        return json({ projects: [{ id: "mock-proj", name: "MOCK 项目", workspacePath: "/mock", isActive: true, isStarted: false, language: "zh" }] });
      }
      // 顺序要紧：`live-status` 也含 `/api/org/agents` 前缀，先单独处理。
      // 形状照前端实际消费点给：`getOrgTree` 接受「数组 / {tree:[]} / 单对象」三种
      // （见 OfficeView.tsx:139-147），返回 `{agents:[...]}` **不在**其中 ⇒ 会被当成空树。
      if (/live-status/.test(url)) return json([]);
      // 单体查询（`getAgent`）与待办（`getAgentTodos`）形状不同，必须在"列表"分支之前判：
      // 否则 `/api/org/agents/mock-agent-1` 会拿到数组 ⇒ `agent payload missing id`、
      // `TodoBar` 读 `todos.todos.length` 直接 TypeError（前者实测踩到）。
      if (/todos/.test(url)) return json({ todos: [] });
      const one = url.match(/\/api\/org\/agents\/([^/?]+)/);
      if (one) return json(agents.find((a) => a.id === one[1]) ?? agents[0]);
      if (/\/api\/org(\?|$)/.test(url)) return json(agents);
      if (/\/api\/org\/agents/.test(url)) return json(agents);
      if (/\/api\/communications|\/api\/chat\/questions/.test(url)) return json([]);
      if (/game-time/.test(url)) return json({ day: 1, hour: 10, minute: 30 });
      return json({});
    },
  );
}

// 4 工位的 world 坐标 —— **回退用**。首选路径是运行时读 `__officeScene.actorMap`
// 拿角色真实屏幕框（见 probeActorPoints）；只有拿不到（非 DEV / 场景未挂载）才落到这里。
// 真值在 src/components/office/constants.ts（DESKS / WORLD_W / WORLD_H），改动时两边都要动。
const DESK_WORLD = [
  { id: "lead-1", x: 670, y: 405 },
  { id: "build-1", x: 420, y: 470 },
  { id: "build-2", x: 620, y: 545 },
  { id: "review-1", x: 870, y: 480 },
];
const WORLD_W = 1280;
const WORLD_H = 720;
const PROBE_OFFSETS = [[0, -60], [0, -30], [36, -40], [36, -10], [-28, -40]];

// 目录建不出来不炸：后面每次截图都会失败并记 [shot:]，--strict 下照样能拦住。
try {
  fs.mkdirSync(OUT, { recursive: true });
} catch (e) {
  problems.push("[fatal] 无法创建输出目录 " + OUT + ": " + String(e?.message || e).slice(0, 120));
}
const problems = [];
const browser = await chromium.launch({
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 }, locale: "zh-CN", deviceScaleFactor: 2 });
const page = await ctx.newPage();
page.on("pageerror", (e) => problems.push("[pageerror] " + String(e.message).slice(0, 240)));
page.on("console", (m) => { if (m.type() === "error") problems.push("[console] " + m.text().slice(0, 240)); });
page.on("requestfailed", (r) => problems.push("[reqfail] " + r.url().slice(0, 140) + " " + (r.failure()?.errorText || "")));

let fatal = false;
let clickLog = [];
let windowOpened = null;
/** 点第二个角色后窗口内的可见文本 —— 验「同一面板切换内容」 */
let windowTextAfterSecondClick = null;
/** 点第二个角色后场景快照里的 selectedAgentId —— 用来区分「没选中」与「选中了但面板没换」 */
let selectedAfterSecondClick = null;
/** 点第二个角色后的窗口数 —— 单例回归判据：应与第一次相同（1），变大即"每人一窗"复发 */
let windowCountAfterSecondClick = null;
let geom = null;   // 运行时几何（canvas rect + world transform）
let diag = null;   // 页面侧诊断快照
let projectSwitched = null;

/**
 * 输出诊断。**必须在 finally 里调** —— 中途任何一步抛错（evaluate 拿到结构变了、
 * 页面崩了）都不能让 stdout 与 diag.json 一起消失，否则「没输出」比「报错」更难查。
 */
const emit = () => {
  const payload = { base: BASE, project: PROJECT, projectSwitched, geom, problems, diag, clickLog, windowOpened, windowCountAfterSecondClick, windowTextAfterSecondClick, selectedAfterSecondClick, mockHits: page.__mockHits ? [...new Set(page.__mockHits)] : [] };
  try {
    fs.writeFileSync(path.join(OUT, "diag.json"), JSON.stringify(payload, null, 2));
  } catch (e) {
    console.error("[emit] 写 diag.json 失败: " + String(e?.message || e));
  }
  console.log(JSON.stringify({ out: OUT, projectSwitched, diag, clickCount: clickLog.length, windowOpened, problemCount: problems.length, problems: problems.slice(0, 8) }, null, 2));
};

/** 截图失败不终止流程（clip 越界/页面崩溃都可能），只记账。 */
const shot = (name, opts = {}) =>
  page.screenshot({ path: path.join(OUT, name), ...opts }).catch((e) => problems.push(`[shot:${name}] ` + String(e.message).slice(0, 140)));

const winTitles = () =>
  page.evaluate(() => [...document.querySelectorAll(".winbox .wb-title")].map((e) => (e.textContent || "").trim())).catch(() => []);

try {
  if (MOCK_AGENTS > 0) await installMocks(page, MOCK_AGENTS);
  try {
    await page.goto(BASE + "/", { waitUntil: "domcontentloaded", timeout: 60000 });
  } catch (err) {
    fatal = true;
    problems.push("[fatal] 打不开 " + BASE + " —— dev server 起了吗？ " + String(err.message).slice(0, 140));
    console.error(`[fatal] 打不开 ${BASE} —— dev server 起了吗？\n${err.message}`);
  }

  if (!fatal) {
    // 等 canvas + 资源（底图 1.5 MB + 角色 sheet）。拿不到 canvas 是「白屏」的第一信号。
    await page.waitForSelector("canvas", { timeout: 30000 }).catch(() => {
      problems.push("[fatal] 30s 内没有 canvas —— 页面白屏？先看 diag.json 的 console 错误");
      fatal = true;
    });
    await page.waitForTimeout(8000);
  }

  // ── 切项目 ───────────────────────────────────────────────────────
  // 用 data-testid（App.tsx 的项目触发器/菜单容器）而不是文本正则：项目名含正则元
  // 字符会炸，且顶栏与菜单项文本同名 ⇒ 文本定位会命中顶栏、点了个寂寞。
  // ── 切项目 ───────────────────────────────────────────────────────
  if (!fatal && PROJECT && MOCK_AGENTS === 0) {
    const trigger = page.getByTestId("project-trigger").first();
    const before = (await trigger.textContent().catch(() => null))?.trim() ?? null;
    if (!before) {
      problems.push("[warn] 找不到 project-trigger，跳过切项目");
    } else {
      try {
        await trigger.click({ timeout: 6000 });
        await page.waitForTimeout(700);
        const menu = page.getByTestId("project-menu").first();
        const hits = menu.getByText(PROJECT, { exact: true });
        const n = await hits.count();
        if (n === 0) throw new Error("菜单里没有该名字");
        if (n > 1) problems.push(`[warn] 菜单里有 ${n} 个同名「${PROJECT}」，取第一个（可能是同名残留项目）`);
        await hits.first().scrollIntoViewIfNeeded({ timeout: 6000 });
        await page.waitForTimeout(300);
        await hits.first().click({ timeout: 6000 });
        await page.waitForTimeout(8000);
        // 回读断言：真的切了才算成功（否则「点了个寂寞」会被当成成功）
        const after = (await trigger.textContent().catch(() => null))?.trim() ?? null;
        if (after === PROJECT) projectSwitched = PROJECT;
        else problems.push(`[warn] 切项目未生效：期望「${PROJECT}」，顶栏仍是「${after}」`);
      } catch (err) {
        problems.push(`[warn] 切项目到 ${PROJECT} 失败：${String(err.message).slice(0, 140)}`);
      }
    }
  }

  if (!fatal) await shot("01-full.png");

  // ── 几何：优先读运行时场景，避免复刻 _fit 公式 ───────────────────
  // 三处 evaluate 都必须 catch：页面状态不确定（页面崩了/结构变了）时不能让整段
  // 流程跳出到外层，否则连 diag.json 都拿不到。
  geom = await page
    .evaluate(() => {
      const c = document.querySelector("canvas");
      if (!c) return null;
      const r = c.getBoundingClientRect();
      const s = window.__officeScene; // dev 专用调试钩子（OfficeScene.ts:184）
      const world = s?.world ? { scale: s.world.scale.x, x: s.world.x, y: s.world.y } : null;
      return { canvas: { left: r.left, top: r.top, width: r.width, height: r.height }, world };
    })
    .catch((e) => { problems.push("[geom] " + String(e.message).slice(0, 120)); return null; });
  const canvasBox = geom?.canvas ?? null;
  const world = geom?.world ?? null;

  // 角色屏幕坐标：首选运行时 actor 的实际包围盒；拿不到才退回 desk 基准点 + 小网格探测。
  const probeActorPoints = async () =>
    page
      .evaluate(() => {
        const s = window.__officeScene;
        const c = document.querySelector("canvas");
        if (!s || !s.actorMap || !c) return null;
        const r = c.getBoundingClientRect();
        const pts = [];
        // actorMap 是 TS private（运行时可读）但**结构不保证**：换成员名/换类型都会
        // 让下面这行抛错，所以整段包 try —— 抛了只是拿不到点位，退化到回退路径。
        try {
          for (const [id, actor] of s.actorMap) {
            // ⚠ 取 **角色本体（sprite）** 的包围盒，不要用 actor.container：
            // container 还挂着名牌（96 宽、悬在头顶）、气泡、rings ⇒ 它的 bounds 中心
            // 会被名牌拉高拉宽，落点常在角色上空 ⇒ 点了个寂寞（2026-09-23 实测：
            // 点第二个角色没换选中，`selectedAgentId` 原地不动）。
            const body = actor?.sprite ?? actor?.container;
            if (!body || body.visible === false) continue;
            const b = body.getBounds?.();
            if (!b) continue;
            const rect = b.rectangle ?? b;
            if (!rect || !(rect.width > 4) || !(rect.height > 4)) continue;
            pts.push({
              id: String(id),
              x: Math.round(r.left + rect.x + rect.width / 2),
              y: Math.round(r.top + rect.y + rect.height * 0.45), // 略偏上：命中躯干（子元素点击会冒泡到 pointertap）
            });
          }
        } catch {
          return null;
        }
        return pts.length ? pts : null;
      })
      .catch(() => null);

  if (!fatal && canvasBox) {
    const scale = world?.scale ?? Math.min(canvasBox.width / WORLD_W, canvasBox.height / WORLD_H);
    const offX = world?.x ?? Math.round((canvasBox.width - WORLD_W * scale) / 2);
    const offY = world?.y ?? Math.round((canvasBox.height - WORLD_H * scale) / 2);
    const toScreen = (wx, wy) => ({
      x: Math.round(canvasBox.left + offX + wx * scale),
      y: Math.round(canvasBox.top + offY + wy * scale),
    });
    // 工位特写：world (300,330) → (1000,620)
    const a = toScreen(300, 330);
    const b = toScreen(1000, 620);
    await shot("02-desks.png", { clip: { x: a.x, y: a.y, width: b.x - a.x, height: b.y - a.y } });
    // HUD 条：world y=0 起，宽度取 canvas 实际宽（不写死 1100）
    const hud = toScreen(0, 0);
    await shot("03-hud.png", { clip: { x: Math.round(canvasBox.left), y: Math.max(0, hud.y - 4), width: Math.round(canvasBox.width), height: 44 } });

    // ── 点小人 → 应开「聊天」窗 ────────────────────────────────────
    // 判据用「窗口数 > 基线」：OfficeWorkspace 在 selectedAgentId 非空时会自动开窗，
    // 只看绝对数量会假阳。
    const baseline = (await winTitles()).length;
    const actors = await probeActorPoints();
    const candidates = actors
      ? actors.map((p) => ({ label: `actor:${p.id}`, at: p }))
      : DESK_WORLD.flatMap((d) => PROBE_OFFSETS.map(([dx, dy]) => ({ label: `desk:${d.id}`, at: toScreen(d.x + dx, d.y + dy) })));

    for (const cand of candidates.slice(0, 12)) {
      await page.mouse.click(cand.at.x, cand.at.y);
      await page.waitForTimeout(1200);
      const titles = await winTitles();
      clickLog.push({ target: cand.label, at: cand.at, titles });
      if (titles.length > baseline) {
        windowOpened = titles;
        await shot("04-window.png");
        // ── 单例回归（用户 2026-09-23 钦定「像微信一样」）────────────────
        // 再点**另一个**角色：窗口数必须不变（同一个面板换内容）。
        // 变大 ⇒ 退回"每人一窗"，那正是用户实拍到的那个毛病。
        const other = candidates.find((c) => c.label !== cand.label && c.label.startsWith("actor:"));
        if (other) {
          // ⚠ 第一次点击开出的窗口**盖在画面中央**，而角色就在那儿 ⇒ 直接点第二个角色
          // 会被 WinBox 吃掉（实测：`selectedAgentId` 原地不动，看起来像"内容没切换"）。
          // 所以先把窗口拖到左下角让出区域 —— 这是测试脚手架动作，不是产品行为。
          const wb = await page.locator(".winbox").first().boundingBox().catch(() => null);
          if (wb) {
            await page.mouse.move(wb.x + 80, wb.y + 18); // 标题栏（.wb-drag 区域）
            await page.mouse.down();
            await page.mouse.move(Math.max(24, wb.x + 80 - 430), Math.min(850, wb.y + 18 + 330), { steps: 10 });
            await page.mouse.up();
            await page.waitForTimeout(700);
          }
          await page.mouse.click(other.at.x, other.at.y);
          await page.waitForTimeout(1500);
          const after = await winTitles();
          windowCountAfterSecondClick = after.length;
          // 光"窗口没多开"不够 —— 用户要的是「同一面板里**切换内容**」，
          // 所以把窗口可见文本抓下来：里面应出现第二个 agent 的名字。
          windowTextAfterSecondClick = await page
            .evaluate(() => (document.querySelector(".winbox")?.innerText || "").replace(/\s+/g, " ").slice(0, 160))
            .catch(() => null);
          selectedAfterSecondClick = await page
            .evaluate(() => window.__officeScene?.snapshot?.selectedAgentId ?? null)
            .catch(() => null);
          clickLog.push({ target: other.label, at: other.at, titles: after, text: windowTextAfterSecondClick, note: "第二次点击：验窗口单例 + 内容切换" });
          if (after.length !== titles.length) {
            problems.push(`[warn] 点第二个角色后窗口数 ${titles.length} → ${after.length}（单例失效？应为同一窗口换内容）`);
          }
          await shot("05-window-after-second-click.png");
        }
        break;
      }
    }
    if (!windowOpened) problems.push("[warn] 点小人未开窗（12 次尝试内）—— 见 diag.clickLog");
  }

  diag = await page
    .evaluate(() => {
      const cv = [...document.querySelectorAll("canvas")].map((c) => ({ w: c.width, h: c.height, cssW: c.clientWidth, cssH: c.clientHeight }));
      const texts = [...document.querySelectorAll("*")].filter((e) => e.children.length === 0).map((e) => (e.textContent || "").trim());
      return {
        canvas: cv,
        agentHint: texts.find((t) => t.includes("PixiJS")) || null,
        bodyTextLen: (document.body?.innerText || "").length,
        /** 「N 人无座」提示（OfficeView 在 agents > 席位数时显示）——结构化取证，免得靠读图 */
        noSeatHint: texts.find((t) => t.includes("无座")) || null,
        winboxTitles: [...document.querySelectorAll(".winbox .wb-title")].map((e) => (e.textContent || "").trim()),
      };
    })
    .catch((e) => { problems.push("[fatal] 取页面诊断失败: " + String(e.message).slice(0, 120)); return null; });

  // 「canvas 在但页面白」：tailwind 令牌缺失时 JS 照跑、canvas 仍在，只有文本量为 0 才暴露
  if (diag && diag.bodyTextLen === 0) problems.push("[fatal] 页面 innerText 为空 —— 疑似白屏（dev server 是否重启过？）");
} catch (err) {
  // 默认模式「尽量不炸」：异常只记账，退出码交给 --strict 决定。
  problems.push("[fatal] 脚本异常: " + String(err?.message || err).slice(0, 200));
} finally {
  emit(); // 无论成败都要有 diag.json 与 stdout
  await browser.close();
}

// --strict：白屏 / 无 canvas / 运行期异常 / 截图失败 都要能拦住
const hardFail = problems.some((p) => p.startsWith("[fatal]") || p.startsWith("[pageerror]") || p.startsWith("[shot:"));
if (STRICT && hardFail) process.exit(1);
