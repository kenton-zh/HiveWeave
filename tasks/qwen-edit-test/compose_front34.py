"""把「正面 3/4 朝向」的角色帧放回工位，离线预览契合度变化。

复用 constants.DESK_SET 的偏移与层级（back → actor → front），
人物帧按「内容高 = 79 frame px」（旧 sheet 的鞋底锚点尺度）缩放，底部对齐锚点。
"""
import os
import sys
from PIL import Image

ASSETS = r"D:\PC_AI\Project\HiveWeave\apps\web\public\office-assets"
HERE = r"D:\PC_AI\Project\HiveWeave\tasks\qwen-edit-test"

DESK_SET = {
    "back": {"w": 159.2, "h": 127.8, "leftTop": (-78.2, -62.1)},
    "front": {"w": 159.2, "h": 88.8, "leftTop": (-78.2, -20.8)},
    "rearChair": {"dx": -50.0, "dy": 18.0},
    "frontChair": {"dx": 43.5, "dy": 28.5},
}
SCALE = 1.6
FRAME_CONTENT_H = 79      # frame px：旧 sheet 头顶~5 到鞋底 84
ENGINE_SCALE = 0.8        # 帧 → world 的缩放


def prep_character(path):
    """裁内容 bbox，缩放到与旧 sheet 内容同高。"""
    im = Image.open(path).convert("RGBA")
    gray = im.convert("L")
    mask = gray.point(lambda v: 255 if v < 240 else 0)
    bbox = mask.getbbox()
    im = im.crop(bbox)
    target_h = FRAME_CONTENT_H * ENGINE_SCALE * SCALE
    scale = target_h / im.height
    nw = max(1, round(im.width * scale))
    return im.resize((nw, int(target_h)), Image.LANCZOS)


def build(actor_path, out_name, variant="A"):
    W, H = 560, 440
    canvas = Image.new("RGBA", (W, H), (236, 214, 178, 255))
    ox, oy = W // 2, 250
    back = Image.open(os.path.join(ASSETS, "office-desk-back.png")).convert("RGBA")
    front = Image.open(os.path.join(ASSETS, "office-desk-front.png")).convert("RGBA")
    b, f = DESK_SET["back"], DESK_SET["front"]

    def put(img, x, y, w_world, h_world):
        r = img.resize((int(round(w_world * SCALE)), int(round(h_world * SCALE))), Image.LANCZOS)
        canvas.alpha_composite(r, (int(x), int(y)))

    # back 片
    put(back, ox + b["leftTop"][0] * SCALE, oy + b["leftTop"][1] * SCALE, b["w"], b["h"])

    # 角色（底部=鞋底 对齐锚点）
    actor = prep_character(actor_path)
    c = DESK_SET["rearChair"] if variant == "A" else DESK_SET["frontChair"]
    ax = ox + c["dx"] * SCALE
    ay = oy + c["dy"] * SCALE
    canvas.alpha_composite(actor, (int(ax - actor.width / 2), int(ay - actor.height)))

    # front 片
    put(front, ox + f["leftTop"][0] * SCALE, oy + f["leftTop"][1] * SCALE, f["w"], f["h"])
    p = os.path.join(HERE, out_name)
    canvas.convert("RGB").save(p)
    return p


if __name__ == "__main__":
    a = build(os.path.join(HERE, "front34_f_1.5.png"), "preview_front34_A.png", "A")
    b2 = build(os.path.join(HERE, "test_front34.png"), "preview_front34_A2.png", "A")
    side = Image.new("RGB", (560 * 2 + 20, 440), (255, 255, 255))
    side.paste(Image.open(a), (0, 0))
    side.paste(Image.open(b2), (580, 0))
    side.save(os.path.join(HERE, "preview_front34_pair.png"))
    print("saved preview_front34_pair.png", side.size)
