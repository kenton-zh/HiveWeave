/**
 * Sprite Sheet Processor
 *
 * Cuts horizontal sprite sheets into individual frames and removes dark backgrounds.
 *
 * Usage: cd apps/web && node --experimental-strip-types scripts/process-sprites.ts
 *   or:  cd apps/web && npx tsx scripts/process-sprites.ts
 *
 * Input:  ../../image/*.png  (sprite sheets from GPT Image 2)
 * Output: public/sprites/characters/<name>/<state>/frame_N.png
 */

import { Jimp, intToRGBA, rgbaToInt } from "jimp";
import * as path from "path";
import * as fs from "fs";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ─── Config ────────────────────────────────────────────────

const IMAGE_DIR = path.resolve(__dirname, "../../../image");
const OUTPUT_DIR = path.resolve(__dirname, "../public/sprites/characters");

// Map sprite sheet files to their state names and frame counts
const SPRITE_MAP: Record<string, { name: string; state: string; frames: number }> = {
  // walk - 7 frames
  "Create_a_pixel_art_sprite_sheet_showing_GPT_Image_2_dc7f45ea6f59.png": {
    name: "bearded_man",
    state: "walk",
    frames: 7,
  },
  // celebrate - 6 frames
  "Create_a_pixel_art_sprite_sheet_showing_GPT_Image_2_43c1a1cdc826.png": {
    name: "bearded_man",
    state: "celebrate",
    frames: 6,
  },
  // thinking - 6 frames
  "Create_a_pixel_art_sprite_sheet_showing_GPT_Image_2_e9bd89f661cc.png": {
    name: "bearded_man",
    state: "thinking",
    frames: 6,
  },
  // talking - 5 frames (frame 6 has wrong direction)
  "Create_a_pixel_art_sprite_sheet_showing_GPT_Image_2_dfa8cdb6433b.png": {
    name: "bearded_man",
    state: "talking",
    frames: 5,
  },
  // typing - 6 frames (includes desk, needs redo, process for now)
  "Create_a_pixel_art_sprite_sheet_showing_GPT_Image_2_fd9e26701f8f.png": {
    name: "bearded_man",
    state: "typing",
    frames: 6,
  },
};

const BG_TOLERANCE = 40;

// ─── Helpers ───────────────────────────────────────────────

function colorDistance(
  r1: number, g1: number, b1: number,
  r2: number, g2: number, b2: number,
): number {
  return Math.sqrt((r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2);
}

function sampleBackgroundColor(img: any): { r: number; g: number; b: number } {
  const w = img.width;
  const h = img.height;
  const samples: Array<{ r: number; g: number; b: number }> = [];

  const corners = [
    [2, 2], [w - 7, 2], [2, h - 7], [w - 7, h - 7],
  ];

  for (const [cx, cy] of corners) {
    for (let dy = 0; dy < 5; dy++) {
      for (let dx = 0; dx < 5; dx++) {
        const pixel = img.getPixelColor(cx + dx, cy + dy);
        const { r, g, b } = intToRGBA(pixel);
        samples.push({ r, g, b });
      }
    }
  }

  const avg = samples.reduce(
    (acc: any, c: any) => ({ r: acc.r + c.r, g: acc.g + c.g, b: acc.b + c.b }),
    { r: 0, g: 0, b: 0 },
  );
  const n = samples.length;
  return { r: Math.round(avg.r / n), g: Math.round(avg.g / n), b: Math.round(avg.b / n) };
}

function removeBackground(img: any, bgColor: { r: number; g: number; b: number }) {
  const w = img.width;
  const h = img.height;

  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const pixel = img.getPixelColor(x, y);
      const { r, g, b } = intToRGBA(pixel);

      if (colorDistance(r, g, b, bgColor.r, bgColor.g, bgColor.b) < BG_TOLERANCE) {
        img.setPixelColor(rgbaToInt(r, g, b, 0), x, y);
      }
    }
  }
}

// ─── Main ──────────────────────────────────────────────────

async function processSpriteSheet(filename: string, config: { name: string; state: string; frames: number }) {
  const inputPath = path.join(IMAGE_DIR, filename);

  if (!fs.existsSync(inputPath)) {
    console.log(`  Skipping ${filename} — file not found`);
    return;
  }

  console.log(`\n  Processing: ${filename}`);
  console.log(`  -> ${config.name}/${config.state} (${config.frames} frames)`);

  const img = await Jimp.read(inputPath);
  const frameWidth = Math.floor(img.width / config.frames);
  const frameHeight = img.height;
  console.log(`  Sheet: ${img.width}x${img.height}, frame: ${frameWidth}x${frameHeight}`);

  const bgColor = sampleBackgroundColor(img);
  console.log(`  BG color: rgb(${bgColor.r}, ${bgColor.g}, ${bgColor.b})`);

  const outputDir = path.join(OUTPUT_DIR, config.name, config.state);
  fs.mkdirSync(outputDir, { recursive: true });

  for (let i = 0; i < config.frames; i++) {
    // Clone and crop
    const frame = img.clone();
    frame.crop({ x: i * frameWidth, y: 0, width: frameWidth, height: frameHeight });

    // Remove background
    removeBackground(frame, bgColor);

    // Write frame
    const outPath = path.join(outputDir, `frame_${i}.png`);
    await frame.write(outPath as any);
    console.log(`  Frame ${i} saved`);
  }

  console.log(`  Done: ${config.frames} frames -> ${outputDir}`);
}

async function main() {
  console.log("Sprite Sheet Processor");
  console.log(`Input:  ${IMAGE_DIR}`);
  console.log(`Output: ${OUTPUT_DIR}`);

  if (!fs.existsSync(IMAGE_DIR)) {
    console.error(`Input directory not found: ${IMAGE_DIR}`);
    process.exit(1);
  }

  for (const [filename, config] of Object.entries(SPRITE_MAP)) {
    await processSpriteSheet(filename, config);
  }

  console.log("\nAll done!");
}

main().catch((e) => { console.error("Error:", e.message); process.exit(1); });
