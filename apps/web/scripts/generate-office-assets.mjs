import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { Jimp, rgbaToInt } from "jimp";

const OUT_DIR = join(process.cwd(), "public", "office-assets");

function rgba(hex, alpha = 255) {
  const clean = hex.replace("#", "");
  const value = Number.parseInt(clean, 16);
  const r = (value >> 16) & 255;
  const g = (value >> 8) & 255;
  const b = value & 255;
  return rgbaToInt(r, g, b, alpha);
}

async function image(w, h) {
  return new Jimp({ width: w, height: h, color: rgba("#000000", 0) });
}

function rect(img, x, y, w, h, color) {
  img.scan(x, y, w, h, (_x, _y, idx) => {
    img.bitmap.data.writeUInt32BE(color, idx);
  });
}

function isoDiamond(img, cx, cy, w, h, color, outline) {
  for (let yy = 0; yy < h; yy++) {
    const dy = Math.abs(yy - h / 2) / (h / 2);
    const rowW = Math.max(1, Math.floor((1 - dy) * w));
    const x0 = Math.floor(cx - rowW / 2);
    rect(img, x0, cy + yy - Math.floor(h / 2), rowW, 1, color);
  }
  for (let yy = 0; yy < h; yy++) {
    const dy = Math.abs(yy - h / 2) / (h / 2);
    const rowW = Math.max(1, Math.floor((1 - dy) * w));
    const x0 = Math.floor(cx - rowW / 2);
    const y = cy + yy - Math.floor(h / 2);
    rect(img, x0, y, 1, 1, outline);
    rect(img, x0 + rowW - 1, y, 1, 1, outline);
  }
}

async function save(img, name) {
  await img.write(join(OUT_DIR, name));
}

async function makeFloorTile() {
  const img = await image(64, 32);
  isoDiamond(img, 32, 16, 62, 30, rgba("#8b5e3c"), rgba("#5f3d27"));
  isoDiamond(img, 32, 15, 54, 24, rgba("#9a6a43"), rgba("#805434"));
  rect(img, 31, 1, 2, 30, rgba("#7b4d2d", 150));
  rect(img, 4, 15, 56, 1, rgba("#b48255", 170));
  await save(img, "floor-tile.png");
}

async function makeWallTile() {
  const img = await image(64, 64);
  rect(img, 6, 8, 52, 42, rgba("#d8d5c7"));
  rect(img, 6, 46, 52, 8, rgba("#b8afa0"));
  rect(img, 10, 14, 17, 26, rgba("#a7d7e8"));
  rect(img, 30, 14, 17, 26, rgba("#a7d7e8"));
  rect(img, 12, 16, 13, 22, rgba("#d9f7ff"));
  rect(img, 32, 16, 13, 22, rgba("#d9f7ff"));
  rect(img, 10, 27, 37, 2, rgba("#8bbacc"));
  rect(img, 5, 7, 54, 2, rgba("#f4efe2"));
  await save(img, "wall-window.png");
}

async function makeDesk() {
  const img = await image(96, 72);
  isoDiamond(img, 48, 26, 88, 42, rgba("#8a6148"), rgba("#50372b"));
  rect(img, 14, 31, 68, 20, rgba("#6c4738"));
  rect(img, 20, 34, 10, 14, rgba("#8d6653"));
  rect(img, 65, 34, 10, 14, rgba("#8d6653"));
  rect(img, 37, 5, 28, 18, rgba("#172033"));
  rect(img, 40, 8, 22, 12, rgba("#38bdf8"));
  rect(img, 48, 23, 6, 6, rgba("#263142"));
  rect(img, 30, 28, 34, 8, rgba("#1f2937"));
  await save(img, "desk-computer.png");
}

async function makeChair() {
  const img = await image(48, 56);
  rect(img, 14, 8, 24, 25, rgba("#394150"));
  rect(img, 10, 28, 30, 13, rgba("#252b36"));
  rect(img, 21, 40, 4, 10, rgba("#111827"));
  rect(img, 14, 49, 19, 3, rgba("#111827"));
  await save(img, "office-chair.png");
}

async function makePlant() {
  const img = await image(48, 64);
  rect(img, 16, 44, 18, 12, rgba("#955f32"));
  rect(img, 18, 55, 14, 4, rgba("#704321"));
  rect(img, 22, 21, 5, 24, rgba("#236b3a"));
  rect(img, 9, 23, 18, 8, rgba("#2f9e44"));
  rect(img, 24, 18, 18, 8, rgba("#37b24d"));
  rect(img, 12, 12, 16, 8, rgba("#51cf66"));
  rect(img, 25, 8, 13, 10, rgba("#2b8a3e"));
  rect(img, 5, 34, 16, 7, rgba("#2f9e44"));
  rect(img, 28, 33, 15, 8, rgba("#51cf66"));
  await save(img, "plant.png");
}

async function makeWhiteboard() {
  const img = await image(112, 72);
  rect(img, 8, 9, 96, 50, rgba("#f3f5ef"));
  rect(img, 8, 9, 96, 3, rgba("#d0d4ca"));
  rect(img, 8, 56, 96, 3, rgba("#b0b7ae"));
  rect(img, 18, 22, 18, 2, rgba("#ef4444"));
  rect(img, 22, 30, 28, 2, rgba("#ef4444"));
  rect(img, 62, 20, 20, 2, rgba("#38bdf8"));
  rect(img, 66, 28, 26, 2, rgba("#22c55e"));
  rect(img, 20, 62, 4, 10, rgba("#6b7280"));
  rect(img, 88, 62, 4, 10, rgba("#6b7280"));
  await save(img, "whiteboard.png");
}

async function makeSpeechBubble() {
  const img = await image(96, 48);
  rect(img, 8, 8, 74, 25, rgba("#ffffff"));
  rect(img, 10, 6, 70, 2, rgba("#ffffff"));
  rect(img, 10, 33, 66, 2, rgba("#ffffff"));
  rect(img, 20, 35, 8, 5, rgba("#ffffff"));
  rect(img, 8, 8, 2, 25, rgba("#3264d9"));
  rect(img, 80, 8, 2, 25, rgba("#3264d9"));
  rect(img, 10, 6, 70, 2, rgba("#3264d9"));
  rect(img, 10, 33, 66, 2, rgba("#3264d9"));
  rect(img, 18, 17, 10, 3, rgba("#3264d9"));
  rect(img, 34, 17, 10, 3, rgba("#3264d9"));
  rect(img, 50, 17, 10, 3, rgba("#3264d9"));
  await save(img, "speech-bubble.png");
}

async function makeCharacterSheet(name, hair, shirt, pants) {
  const frameW = 32;
  const frameH = 48;
  const img = await image(frameW * 4, frameH * 3);
  const skin = rgba("#f2b184");
  const outline = rgba("#3a2a24");

  function drawFrame(col, row, step, facing) {
    const ox = col * frameW;
    const oy = row * frameH;
    const lean = step === 1 ? -1 : step === 3 ? 1 : 0;
    rect(img, ox + 13 + lean, oy + 7, 9, 10, skin);
    rect(img, ox + 11 + lean, oy + 5, 13, 6, hair);
    rect(img, ox + 12 + lean, oy + 16, 12, 15, shirt);
    rect(img, ox + 10 + lean, oy + 22, 16, 4, rgba("#ffffff", 90));
    rect(img, ox + 9 + lean, oy + 18, 4, 12, shirt);
    rect(img, ox + 24 + lean, oy + 18, 4, 12, shirt);
    rect(img, ox + 13 + lean, oy + 30, 5, 11, pants);
    rect(img, ox + 20 + lean, oy + 30, 5, 11, pants);
    if (step === 1) rect(img, ox + 9, oy + 40, 8, 3, outline);
    else if (step === 3) rect(img, ox + 20, oy + 40, 8, 3, outline);
    else {
      rect(img, ox + 12, oy + 40, 7, 3, outline);
      rect(img, ox + 20, oy + 40, 7, 3, outline);
    }
    rect(img, ox + 15 + lean, oy + 11, 2, 2, outline);
    rect(img, ox + 21 + lean, oy + 11, 2, 2, outline);
    if (facing === "side") {
      rect(img, ox + 23 + lean, oy + 12, 3, 2, skin);
      rect(img, ox + 14 + lean, oy + 11, 2, 2, outline);
    }
  }

  for (let row = 0; row < 3; row++) {
    for (let col = 0; col < 4; col++) drawFrame(col, row, col, row === 1 ? "side" : "front");
  }
  await save(img, name);
}

async function makeReferenceBoard() {
  const img = await image(512, 320);
  rect(img, 0, 0, 512, 320, rgba("#10151d"));
  for (let y = 0; y < 5; y++) {
    for (let x = 0; x < 7; x++) {
      isoDiamond(img, 70 + x * 58, 80 + y * 29, 56, 28, rgba("#875a38"), rgba("#4f3524"));
    }
  }
  rect(img, 42, 34, 380, 10, rgba("#d8d5c7"));
  rect(img, 50, 44, 360, 32, rgba("#a7d7e8"));
  await save(img, "office-reference-board.png");
}

await mkdir(OUT_DIR, { recursive: true });
await makeFloorTile();
await makeWallTile();
await makeDesk();
await makeChair();
await makePlant();
await makeWhiteboard();
await makeSpeechBubble();
await makeCharacterSheet("agent-dev-sheet.png", rgba("#1f2937"), rgba("#ef4444"), rgba("#111827"));
await makeCharacterSheet("agent-manager-sheet.png", rgba("#7c2d12"), rgba("#2563eb"), rgba("#111827"));
await makeCharacterSheet("agent-qa-sheet.png", rgba("#111827"), rgba("#eab308"), rgba("#111827"));
await makeReferenceBoard();

console.log(`Generated office assets in ${OUT_DIR}`);
