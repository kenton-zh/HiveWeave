/**
 * 裁剪放大 office 截图中的演示角色区域（中排中桌 = DESKS[0] lead-1, world 747,326）。
 * world 1280x720 → canvas 绘制区，再映射整页截图。
 */
import { Jimp } from "jimp";

// 整页截图 1100x609（playwright viewport 1600x900 → 截图实际尺寸看文件）
// canvas 区域约占 x 8..638, y 85..610（从截图目测），world 1280x720 映射到 canvas
// 演示角色在 world(747, 326)，头顶再往上 ~80px
const shot = process.argv[2];
const out = process.argv[3];

const img = await Jimp.read(shot);
const W = img.bitmap.width, H = img.bitmap.height;
console.log(`page shot: ${W}x${H}`);

// canvas 元素在页面上的位置：左侧面板内部。粗略按比例：
// 面板 canvas 中心 ≈ (W*0.293, H*0.38) 处，canvas 显示宽度 ≈ W*0.575
// world→canvas: canvasX = 8 + (747/1280)*630, canvasY = 85 + (326/720)*525
// 以 1100x609 实测校准：cx ≈ 8+367=375, cy ≈ 85+238=323（W=1100 时）
const sx = W / 1100, sy = H / 609;
const cx = 375 * sx, cy = 323 * sy;
const halfW = 90 * sx, halfH = 80 * sy;
const cropX = Math.max(0, Math.round(cx - halfW));
const cropY = Math.max(0, Math.round(cy - halfH));
const cropW = Math.min(W - cropX, Math.round(halfW * 2));
const cropH = Math.min(H - cropY, Math.round(halfH * 2));

img.crop({ x: cropX, y: cropY, w: cropW, h: cropH });
// 放大 3 倍便于观察
img.resize({ w: cropW * 3, h: cropH * 3, scaleMode: Jimp.RESIZE_BILINEAR });
await img.write(out);
console.log(`cropped -> ${out} (${cropW}x${cropH} @3x)`);
