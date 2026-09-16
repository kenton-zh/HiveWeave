"""把 3/4 视角帧组装成 OfficeView 规格的 sprite sheet。

帧契约（见 skills/comfyui-sprite-sheet/SKILL.md）：
  96×96 帧、行优先 4×2；内容底（鞋底）落在 y=84 = anchor 0.875×96；水平居中；
  同场景多动作共用联合 bbox 缩放，防切换跳动。
"""
import os
import sys
import numpy as np
from PIL import Image

HERE = r"D:\PC_AI\Project\HiveWeave\tasks\qwen-edit-test"
sys.path.insert(0, HERE)
from white_to_alpha import white_to_alpha  # noqa: E402

FRAME = 96
BASE_Y = 84          # anchor 0.875
COLS, ROWS = 4, 2
CLIPS = {
    "typing": [28, 50, 72, 94],
    "sitting": [28, 50, 72, 94],
}


def load_alpha(path):
    im = white_to_alpha(Image.open(path))
    bbox = im.split()[3].getbbox()
    return im.crop(bbox)


def main():
    order = [("typing", i) for i in CLIPS["typing"]] + [("sitting", i) for i in CLIPS["sitting"]]
    frames = []
    for clip, idx in order:
        p = os.path.join(HERE, f"view34_{clip}_{idx}.png")
        frames.append((f"{clip}#{idx}", load_alpha(p)))
    print("帧:", ", ".join(f"{n}{im.size}" for n, im in frames))

    # 联合 bbox：取所有帧内容的最大宽高（比例统一，防抖动）
    max_w = max(im.width for _, im in frames)
    max_h = max(im.height for _, im in frames)
    content_h = 79                      # 与旧 sheet 一致：头顶~5 → 鞋底 84
    scale = content_h / max_h

    canvas = Image.new("RGBA", (FRAME * COLS, FRAME * ROWS), (0, 0, 0, 0))
    for i, (name, im) in enumerate(frames):
        w = max(1, int(round(im.width * scale)))
        h = max(1, int(round(im.height * scale)))
        r = im.resize((w, h), Image.LANCZOS)
        col, row = i % COLS, i // COLS
        x = col * FRAME + (FRAME - w) // 2
        y = row * FRAME + (BASE_Y - h)
        canvas.alpha_composite(r, (x, y))

    out = os.path.join(HERE, "agent-purple-anim-sheet-NEW.png")
    canvas.save(out)
    print("saved", out, canvas.size, f"scale={scale:.4f}")

    # 与旧 sheet 并排（均放大 3x 便于观看）
    old = Image.open(r"D:\PC_AI\Project\HiveWeave\apps\web\public\office-assets\agent-purple-anim-sheet.png").convert("RGBA")
    Z = 3
    W, H = FRAME * COLS * Z, FRAME * ROWS * Z
    side = Image.new("RGB", (W * 2 + 30, H + 46), (250, 250, 252))
    from PIL import ImageDraw, ImageFont
    d = ImageDraw.Draw(side)
    f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 22)
    for k, (img, label, color) in enumerate([(old, "旧 sheet（侧视背影）", (190, 40, 40)), (canvas, "新 sheet（3-4 视角）", (20, 120, 60))]):
        big = img.resize((W, H), Image.NEAREST)
        bg = Image.new("RGB", (W, H), (240, 240, 246))
        # 棋盘
        for yy in range(0, H, 16):
            for xx in range(0, W, 16):
                if ((xx // 16) + (yy // 16)) % 2 == 0:
                    for y2 in range(yy, min(yy + 16, H)):
                        for x2 in range(xx, min(xx + 16, W)):
                            bg.putpixel((x2, y2), (252, 252, 255))
        bg.paste(big, (0, 0), big)
        side.paste(bg, (k * (W + 30), 40))
        d.text((k * (W + 30) + 6, 10), label, fill=color, font=f)
    p = os.path.join(HERE, "sheet_before_after.png")
    side.save(p)
    print("saved", p, side.size)


if __name__ == "__main__":
    main()
