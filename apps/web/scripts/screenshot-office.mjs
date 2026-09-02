/**
 * 办公室视觉验证：打开 Office 视图，等场景渲染后连拍多帧。
 * 用途：验证单角色动画演示 —— 第一个角色（dev sheet）视角/遮挡是否正确、
 * 是否在「打字 ↔ 坐姿呼吸」间切换（连拍时间间隔 > 半个演示周期）。
 *
 * 用法：node scripts/screenshot-office.mjs [outPrefix]
 * 产出：<outPrefix>-1.png / -2.png（两帧相隔约 5s，覆盖演示周期两态）
 */
import { chromium } from "playwright";

const prefix = process.argv[2] || "test-screenshots/office-anim";
const URL = "http://localhost:5173/";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
page.on("console", (m) => {
  if (m.type() === "error") console.log("[console.error]", m.text().slice(0, 200));
});
page.on("pageerror", (e) => console.log("[pageerror]", String(e).slice(0, 300)));

await page.goto(URL, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(2500);

// 若有项目选择页，选第一个项目
const firstProject = page.locator("text=TEST_DSH_38").first();
if (await firstProject.isVisible().catch(() => false)) {
  await firstProject.click();
  await page.waitForTimeout(1500);
  console.log("[nav] project selected");
}

// 若有 Office/办公室 tab，点进去
for (const label of ["办公室", "Office", "office"]) {
  const tab = page.locator(`text=${label}`).first();
  if (await tab.isVisible().catch(() => false)) {
    await tab.click();
    console.log(`[nav] tab "${label}" clicked`);
    await page.waitForTimeout(1200);
    break;
  }
}

// 等 PixiJS canvas 出现 + 场景加载
await page.waitForSelector("canvas", { timeout: 15000 });
await page.waitForTimeout(3500);

const out1 = `${prefix}-1.png`;
await page.screenshot({ path: out1 });
console.log("[shot]", out1);

// 5 秒后第二帧：演示周期 9s（5.5s 打字 + 3.5s 呼吸），5s 间隔大概率跨态
await page.waitForTimeout(5000);
const out2 = `${prefix}-2.png`;
await page.screenshot({ path: out2 });
console.log("[shot]", out2);

// 第三帧：再等 4.5s ≈ 下一个周期起点，尽量覆盖另一半状态
await page.waitForTimeout(4500);
const out3 = `${prefix}-3.png`;
await page.screenshot({ path: out3 });
console.log("[shot]", out3);

await browser.close();
console.log("done");
