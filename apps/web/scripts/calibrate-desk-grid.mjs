/**
 * 工位网格标定：在办公室底图上搜「整齐网格」布局（用户要求：桌椅摆整齐）。
 *
 * 与 _desk_scan.mjs 的区别：那个是"能塞就塞"的自由候选；这个要求 **规则网格**
 * （等列距 + 等行距 + 行列对齐），先判定每个候选格位是否能放下整套桌椅，
 * 再枚举 (列距, 行距, 起点) 组合，输出**全部格位同时可行**的方案。
 *
 * 判定同源：木地板基色 RGB(242,170,96) ±26；桌套件包围盒 x∈[-78,+81] / y∈[-62,+67]
 * 必须整块落在空地上（world 1:1 于底图 1280×720）。
 */
import { Jimp } from "jimp";
import fs from "node:fs";

const img = await Jimp.read("public/office-assets/office-scene-bg.png");
const W = img.bitmap.width;
const H = img.bitmap.height;
const { data } = img.bitmap;
const at = (x, y) => {
  const i = (y * W + x) * 4;
  return [data[i], data[i + 1], data[i + 2]];
};
const samples = [[640, 400], [700, 350], [600, 450], [800, 400], [500, 500]];
const ref = [0, 1, 2].map((c) => samples.map(([x, y]) => at(x, y)[c]).sort((a, b) => a - b)[1]);
const TOL = 26;
const isFloor = (x, y) => {
  const [r, g, b] = at(x, y);
  return Math.abs(r - ref[0]) < TOL && Math.abs(g - ref[1]) < TOL && Math.abs(b - ref[2]) < TOL;
};

const BOX = { x0: -78, x1: 81, y0: -62, y1: 67 };
const boxClear = (cx, cy) => {
  if (cx + BOX.x0 < 0 || cx + BOX.x1 >= W || cy + BOX.y0 < 0 || cy + BOX.y1 >= H) return false;
  for (let y = cy + BOX.y0; y <= cy + BOX.y1; y += 3) {
    for (let x = cx + BOX.x0; x <= cx + BOX.x1; x += 3) {
      if (!isFloor(x, y)) return false;
    }
  }
  return true;
};

// ── 逐轮缓存试过的格位，避免重复算（boxClear 很贵）
const cache = new Map();
const ok = (x, y) => {
  const k = x + "," + y;
  if (!cache.has(k)) cache.set(k, boxClear(x, y));
  return cache.get(k);
};

// ── 诊断模式：逐行列出「可用 x 段」，回答"为什么某排列不下"
if (process.env.DIAG) {
  console.log("行诊断（每个 y 上 boxClear 的 x 连续段；段内数字=段长 world px）");
  for (let y = 270; y <= 650; y += 20) {
    const runs = [];
    let start = null;
    for (let x = 300; x <= 1240; x += 5) {
      const good = ok(x, y);
      if (good && start === null) start = x;
      if (!good && start !== null) { runs.push([start, x - 5]); start = null; }
    }
    if (start !== null) runs.push([start, 1240]);
    const nice = runs
      .filter(([a, b]) => b - a >= 100)
      .map(([a, b]) => `${a}..${b}(${b - a + 1})`)
      .join("  ");
    console.log(`  y=${String(y).padStart(3)}: ${nice || "（无 ≥100 的可用段）"}`);
  }
  process.exit(0);
}

// ── 枚举「整齐网格」：COLS 列 × ROWS 排
const COLS = Number(process.env.COLS || 4);
const ROWS = Number(process.env.ROWS || 2);
const DXS = [];
for (let d = 162; d <= 300; d += 2) DXS.push(d);
const DYS = [];
for (let d = 130; d <= 260; d += 2) DYS.push(d);
const X0S = [];
for (let x = 340; x <= 800; x += 5) X0S.push(x);
const Y0S = [];
for (let y = 240; y <= 470; y += 5) Y0S.push(y);

const found = [];
for (const dx of DXS) {
  for (const dy of DYS) {
    for (const x0 of X0S) {
      for (const y0 of Y0S) {
        let all = true;
        for (let r = 0; r < ROWS && all; r++) {
          for (let c = 0; c < COLS; c++) {
            if (!ok(x0 + c * dx, y0 + r * dy)) { all = false; break; }
          }
        }
        if (!all) continue;
        // 打分：间距越大越"松快"，整体越居中越好看；再惩罚过于靠边
        const spanX = (COLS - 1) * dx + 159;
        const cxMid = x0 + ((COLS - 1) * dx) / 2;
        const cyMid = y0 + ((ROWS - 1) * dy) / 2;
        const score = dx * 0.6 + dy * 0.4 - Math.abs(cxMid - 800) * 1.2 - Math.abs(cyMid - 455) * 0.8;
        found.push({ dx, dy, x0, y0, spanX, cxMid, cyMid, score });
      }
    }
  }
}
found.sort((a, b) => b.score - a.score);
console.log(`可行整齐网格方案：${found.length}（${COLS} 列 × ${ROWS} 排）`);
for (const f of found.slice(0, 8)) {
  const pts = [];
  for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) pts.push(`${f.x0 + c * f.dx},${f.y0 + r * f.dy}`);
  console.log(
    `  dx=${f.dx} dy=${f.dy} 起点(${f.x0},${f.y0}) 跨度X=${f.spanX} 中心(${Math.round(f.cxMid)},${Math.round(f.cyMid)}) score=${f.score.toFixed(1)}`,
    "\n    点:", pts.join(" | ")
  );
}
fs.writeFileSync("test-screenshots/_desk-grid.json", JSON.stringify(found.slice(0, 40), null, 2));
if (found.length) {
  const best = found[0];
  const pts = [];
  for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) pts.push({ x: best.x0 + c * best.dx, y: best.y0 + r * best.dy });
  console.log("\n推荐方案点位（排优先）:", JSON.stringify(pts));
}
