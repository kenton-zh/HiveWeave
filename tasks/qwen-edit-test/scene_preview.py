"""场景级预览：把新工位套件 + 3/4 人物帧放进真实背景（1280×720）的槽位上，
用于判断缩放是否与场景协调。

对照基准：DESKS[0] = lead-1 (747, 326)。
"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = r"D:\PC_AI\Project\HiveWeave\tasks\qwen-edit-test"
BG = r"D:\PC_AI\Project\HiveWeave\apps\web\public\office-assets\office-scene-bg.png"
WORLD_W, WORLD_H = 1280, 720
SLOT = (747, 326)


def build(bg, ws, actor, ws_scale, actor_h_ratio, slot=SLOT,
          ws_anchor=(250, 560), chair_ground=(320, 560)):
    canvas = bg.copy()
    W = int(ws.width * ws_scale)
    H = int(ws.height * ws_scale)
    wsr = ws.resize((W, H), Image.LANCZOS)
    # 素材锚点 -> 槽位
    ax = slot[0] - ws_anchor[0] * ws_scale
    ay = slot[1] - ws_anchor[1] * ws_scale
    canvas.alpha_composite(wsr, (int(ax), int(ay)))
    # 人物：底部落在素材内椅子所在地面
    ah = int(ws.height * ws_scale * actor_h_ratio)
    a = actor.resize((max(1, int(round(actor.width * ah / actor.height))), ah), Image.LANCZOS)
    fx = ax + chair_ground[0] * ws_scale
    fy = ay + chair_ground[1] * ws_scale
    canvas.alpha_composite(a, (int(fx - a.width / 2), int(fy - a.height)))
    return canvas


def main():
    bg = Image.open(BG).convert("RGBA").resize((WORLD_W, WORLD_H), Image.LANCZOS)
    ws = Image.open(os.path.join(HERE, "ws_cut400.png")).convert("RGBA")
    from white_to_alpha import white_to_alpha
    actor = white_to_alpha(Image.open(os.path.join(HERE, "front34_f_1.5.png")))
    actor = actor.crop(actor.split()[3].getbbox())
    print("工位素材", ws.size, "人物", actor.size)

    variants = [("S=0.20", 0.20, 0.54), ("S=0.28", 0.28, 0.54), ("S=0.36", 0.36, 0.54)]
    imgs = [(n, build(bg, ws, actor, s, r)) for n, s, r in variants]
    cw, ch = WORLD_W, WORLD_H
    canvas = Image.new("RGB", (cw, ch * len(imgs) + 20 * (len(imgs) + 1) + 30), (250, 250, 252))
    d = ImageDraw.Draw(canvas)
    f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 20)
    d.text((10, 6), "新工位 + 3/4 人物 在真实场景中的观感（槽位 lead-1）", fill=(20, 20, 30), font=f)
    y = 30
    for n, img in imgs:
        canvas.paste(img.convert("RGB"), (0, y))
        d.rectangle([6, y + 6, 120, y + 32], fill=(255, 255, 255))
        d.text((10, y + 8), n, fill=(200, 40, 40), font=f)
        y += ch + 20
    p = os.path.join(HERE, "scene_preview.png")
    canvas.save(p)
    print("saved", p, canvas.size)


if __name__ == "__main__":
    main()
