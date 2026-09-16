"""场景级前后对比：同一背景、同一槽位（lead-1），旧工位 vs 新工位。"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = r"D:\PC_AI\Project\HiveWeave\tasks\qwen-edit-test"
ASSETS = r"D:\PC_AI\Project\HiveWeave\apps\web\public\office-assets"
BG = os.path.join(ASSETS, "office-scene-bg.png")
WORLD_W, WORLD_H = 1280, 720
SLOT = (747, 326)

# 旧几何（constants.DESK_SET）
OLD_BACK = {"w": 159.2, "h": 127.8, "leftTop": (-78.2, -62.1)}
OLD_FRONT = {"w": 159.2, "h": 88.8, "leftTop": (-78.2, -20.8)}
OLD_REAR_CHAIR = {"dx": -50.0, "dy": 18.0}
FRAME = 96
ENGINE_SCALE = 0.8


def old_scene(bg):
    c = bg.copy()
    back = Image.open(os.path.join(ASSETS, "office-desk-back.png")).convert("RGBA")
    front = Image.open(os.path.join(ASSETS, "office-desk-front.png")).convert("RGBA")
    sheet = Image.open(os.path.join(ASSETS, "agent-purple-anim-sheet.png")).convert("RGBA")
    frame = sheet.crop((0, 0, FRAME, FRAME))

    def put(img, x, y, ww, hh):
        r = img.resize((int(round(ww)), int(round(hh))), Image.LANCZOS)
        c.alpha_composite(r, (int(x), int(y)))

    put(back, SLOT[0] + OLD_BACK["leftTop"][0], SLOT[1] + OLD_BACK["leftTop"][1], OLD_BACK["w"], OLD_BACK["h"])
    disp = FRAME * ENGINE_SCALE
    ax = SLOT[0] + OLD_REAR_CHAIR["dx"]
    ay = SLOT[1] + OLD_REAR_CHAIR["dy"]
    put(frame, ax - disp / 2, ay - disp * 0.875, disp, disp)
    put(front, SLOT[0] + OLD_FRONT["leftTop"][0], SLOT[1] + OLD_FRONT["leftTop"][1], OLD_FRONT["w"], OLD_FRONT["h"])
    return c


def new_scene(bg, ws_scale, anchor=(240, 250), chair_ground=(320, 560), actor_ratio=0.54):
    from white_to_alpha import white_to_alpha
    c = bg.copy()
    ws = Image.open(os.path.join(HERE, "ws_cut400.png")).convert("RGBA")
    actor = white_to_alpha(Image.open(os.path.join(HERE, "front34_f_1.5.png")))
    actor = actor.crop(actor.split()[3].getbbox())
    W, H = int(ws.width * ws_scale), int(ws.height * ws_scale)
    ax = SLOT[0] - anchor[0] * ws_scale
    ay = SLOT[1] - anchor[1] * ws_scale
    c.alpha_composite(ws.resize((W, H), Image.LANCZOS), (int(ax), int(ay)))
    ah = int(W * actor_ratio)
    a = actor.resize((max(1, int(round(actor.width * ah / actor.height))), ah), Image.LANCZOS)
    fx = ax + chair_ground[0] * ws_scale
    fy = ay + chair_ground[1] * ws_scale
    c.alpha_composite(a, (int(fx - a.width / 2), int(fy - a.height)))
    return c


def main():
    bg = Image.open(BG).convert("RGBA").resize((WORLD_W, WORLD_H), Image.LANCZOS)
    left = old_scene(bg).convert("RGB")
    right = new_scene(bg, 0.28).convert("RGB")
    box = (470, 120, 1170, 630)
    l, r = left.crop(box), right.crop(box)
    w, h = l.size
    canvas = Image.new("RGB", (w * 2 + 30, h + 50), (250, 250, 252))
    canvas.paste(l, (0, 44))
    canvas.paste(r, (w + 30, 44))
    d = ImageDraw.Draw(canvas)
    f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 24)
    d.text((6, 10), "旧工位（硬黑阴影 / 侧视背影）", fill=(190, 40, 40), font=f)
    d.text((w + 36, 10), "新工位（柔和接地影 / 3-4 视角可见脸）", fill=(20, 120, 60), font=f)
    p = os.path.join(HERE, "SCENE_BEFORE_AFTER.png")
    canvas.save(p)
    print("saved", p, canvas.size)


if __name__ == "__main__":
    to = os.path.dirname(os.path.abspath(__file__))
    import sys

    sys.path.insert(0, to)
    os.chdir(to)
    main()
