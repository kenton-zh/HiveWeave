/**
 * 精灵图帧内容包围盒分析器 —— 用于校准动画帧序列与落座锚点。
 *
 * ── 为什么需要它 ──────────────────────────────────────────────
 * 等距办公室里角色「坐没坐进椅子、有没有浮空」完全取决于帧内容在帧格里的
 * 垂直位置，而这**看代码看不出来、肉眼在 sheet 上也很难量准**。
 * 本脚本把每帧的非透明像素包围盒量出来，把姿态判断变成可复现的数字。
 *
 * ── 核心判据 ──────────────────────────────────────────────────
 * 所有角色帧共用 FOOT_ANCHOR_Y = 0.875（脚底/锚点位于帧高的 87.5% 处）。
 *   bottom == frameH * 0.875  → 该帧内容触到锚点线，**姿态与锚点自洽**
 *   bottom 明显小于锚点线      → 内容悬空，对齐锚点后角色会**浮空** (站立/跳跃帧)
 *   top 越大                   → 头顶越低（前倾/坐姿）；top 越小 → 站得越高
 *
 * 因此「一个动画序列内 bottom 是否一致」= 序列是否混入了异姿态帧。
 * 混入会导致播放时角色上下跳动，且浮空帧会破坏与桌面的遮挡关系。
 *
 * 用法: node scripts/analyze-frames.mjs
 */
import { Jimp } from "jimp";

const FOOT_ANCHOR_Y = 0.875;

/** 待分析的 sheet 配置（与 constants.ts 的 SHEET_LAYOUTS / *_ANIM_SEQS 对应） */
const SHEETS = [
  {
    label: "dev 女孩（满帧动画表）",
    path: "public/office-assets/agent-dev-sheet.png",
    cols: 8, rows: 4, fw: 64, fh: 96,
    seqs: {
      idle: [0, 1], walking: [2, 3, 4, 5], working: [8, 9], talking: [14, 15],
      alert: [18, 19], sitdown: [20, 21, 22, 23], sitting: [24, 25],
      sitdown_b: [26, 27, 28, 29], sitting_b: [30, 31], getup: [23, 22, 21, 20],
    },
  },
  {
    label: "紫衣女孩（4 帧打字表）",
    path: "public/office-assets/agent-purple-typing-sheet.png",
    cols: 2, rows: 2, fw: 96, fh: 96,
    seqs: { idle: [0, 1, 2, 3], working: [0, 1, 2, 3], sitting: [0, 1, 2, 3] },
  },
];

/** 量出一帧内非透明像素的包围盒 */
function bbox(img, fx, fy, fw, fh) {
  let minX = 1e9, minY = 1e9, maxX = -1, maxY = -1, count = 0;
  for (let y = 0; y < fh; y++) {
    for (let x = 0; x < fw; x++) {
      const px = fx + x, py = fy + y;
      if (px >= img.bitmap.width || py >= img.bitmap.height) continue;
      const a = img.bitmap.data[(py * img.bitmap.width + px) * 4 + 3];
      if (a > 24) {
        count++;
        if (x < minX) minX = x;
        if (y < minY) minY = y;
        if (x > maxX) maxX = x;
        if (y > maxY) maxY = y;
      }
    }
  }
  return count ? { minX, minY, maxX, maxY, h: maxY - minY + 1, count } : null;
}

const report = {};

for (const cfg of SHEETS) {
  const img = await Jimp.read(cfg.path);
  const anchor = Math.round(cfg.fh * FOOT_ANCHOR_Y); // 锚点线在帧内的 y
  const frames = [];
  for (let i = 0; i < cfg.cols * cfg.rows; i++) {
    frames.push(bbox(img, (i % cfg.cols) * cfg.fw, Math.floor(i / cfg.cols) * cfg.fh, cfg.fw, cfg.fh));
  }

  console.log(`\n${"=".repeat(72)}`);
  console.log(`■ ${cfg.label}  ${cfg.path}`);
  console.log(`  sheet ${img.bitmap.width}x${img.bitmap.height} | ${cfg.cols}x${cfg.rows} 帧 x ${cfg.fw}x${cfg.fh} | 锚点线 y=${anchor}`);
  console.log(`${"=".repeat(72)}`);

  console.log("\n帧  | top bottom  h  | 触锚点? | 说明");
  console.log("-".repeat(64));
  frames.forEach((f, i) => {
    if (!f) { console.log(`${String(i).padStart(3)} | (空帧)`); return; }
    const touch = Math.abs(f.maxY - anchor) <= 2;
    const gap = f.maxY - anchor; // 正=内容超出锚点线，负=悬空
    const note = touch ? "内容触到锚点线" : `离锚点线 ${gap > 0 ? "+" : ""}${gap}px ${gap < 0 ? "→ 浮空!" : ""}`;
    console.log(
      `${String(i).padStart(3)} | ${String(f.minY).padStart(3)} ${String(f.maxY).padStart(6)} ${String(f.h).padStart(3)} | ` +
      `${touch ? "  ✓   " : "  ✗   "} | ${note}`
    );
  });

  console.log("\n── 序列内姿态一致性（bottom 是否稳定在锚点线）──");
  for (const [name, seq] of Object.entries(cfg.seqs)) {
    const bs = seq.map((i) => frames[i]?.maxY).filter((v) => v != null);
    const ts = seq.map((i) => frames[i]?.minY).filter((v) => v != null);
    if (!bs.length) { console.log(`  ${name.padEnd(11)} 空帧!`); continue; }
    const bMin = Math.min(...bs), bMax = Math.max(...bs);
    const tMin = Math.min(...ts), tMax = Math.max(...ts);
    const bad = seq.filter((i) => frames[i] && Math.abs(frames[i].maxY - anchor) > 2);
    console.log(
      `  ${name.padEnd(11)} [${seq.join(",").padEnd(15)}] bottom ${bMin}..${bMax} (Δ${bMax - bMin}) ` +
      `top ${tMin}..${tMax} | ${bad.length ? `⚠ 混入非坐姿帧: ${bad.join(",")}` : "✓ 全部触锚点线"}`
    );
  }

  report[cfg.label] = { cfg, frames, anchor };
}

// ── 跨 sheet 落座对齐校准 ────────────────────────────────────────
// 两种 sheet 的坐姿帧若共用同一 seatPos()，视觉高度差 = 两者「锚点线到头顶」之差。
// 头顶越高（top 越小）→ 人显得越大/坐得越高 → 需要更大的下压偏移。
const dev = report["dev 女孩（满帧动画表）"];
const purple = report["紫衣女孩（4 帧打字表）"];
if (dev && purple) {
  console.log(`\n${"=".repeat(72)}`);
  console.log("■ 落座 y 偏移校准（DEV_SHEET_SEAT_Y_OFFSET）");
  console.log(`${"=".repeat(72)}`);
  const devSit = [8, 9, 24, 25].map((i) => dev.frames[i]).filter(Boolean);
  const purSit = [0, 1, 2, 3].map((i) => purple.frames[i]).filter(Boolean);
  const avg = (arr, k) => arr.reduce((s, f) => s + f[k], 0) / arr.length;

  const devTop = avg(devSit, "minY"), devBot = avg(devSit, "maxY");
  const purTop = avg(purSit, "minY"), purBot = avg(purSit, "maxY");
  console.log(`  dev   坐姿帧[8,9,24,25]  平均 top=${devTop.toFixed(1)}  bottom=${devBot.toFixed(1)}  锚点线=${dev.anchor}`);
  console.log(`  紫衣  坐姿帧[0,1,2,3]     平均 top=${purTop.toFixed(1)}  bottom=${purBot.toFixed(1)}  锚点线=${purple.anchor}`);
  // 两者都把 anchor 对齐到同一座位点，则头顶位置差 = top 之差
  const delta = purTop - devTop;
  console.log(`\n  头顶位置差 (紫衣top - devtop) = ${purTop.toFixed(1)} - ${devTop.toFixed(1)} = ${delta.toFixed(1)}px`);
  console.log(`  → dev 头顶比紫衣${delta > 0 ? "高" : "低"} ${Math.abs(delta).toFixed(1)}px`);
  console.log(`  → 若要两者头顶齐平，dev 需${delta > 0 ? "下压" : "上抬"} ${Math.abs(delta).toFixed(1)}px`);
  console.log(`  → 建议 DEV_SHEET_SEAT_Y_OFFSET = ${Math.round(delta)}  (当前常量值需对照此数)`);
}
