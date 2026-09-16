"""标定扫描：正面帧在工位里的 dx/dy 组合，找最接近参照图观感（露上半身、不嵌进桌子）的摆位。"""
import os
from PIL import Image, ImageDraw, ImageFont

ASSETS = r"D:\PC_AI\Project\HiveWeave\apps\web\public\office-assets"
HERE = r"D:\PC_AI\Project\HiveWeave\tasks\qwen-edit-test"
ACTOR = os.path.join(HERE, "front34_f_1.5.png")

DESK_SET = {
    "back": {"w": 159.2, "h": 127.8, "leftTop": (-78.2, -62.1)},
    "front": {"w": 159.2, "h": 88.8, "leftTop": (-78.2, -20.8)},
}
SCALE = 1.6
FRAME_CONTENT_H = 79
ENGINE_SCALE = 0.8


def prep_character(path):
    im = Image.open(path).convert("RGBA")
    mask = im.convert("L").point(lambda v: 255 if v < 240 else 0)
    im = im.crop(mask.getbbox())
    target_h = FRAME_CONTENT_H * ENGINE_SCALE * SCALE
    return im.resize((max(1, round(im.width * target_h / im.height)), int(target_h)), Image.LANCZOS)


def render(actor, dx, dy, w=300, h=280):
    canvas = Image.new("RGBA", (w, h), (236, 214, 178, 255))
    ox, oy = w // 2, int(h * 0.62)
    back = Image.open(os.path.join(ASSETS, "office-desk-back.png")).convert("RGBA")
    front = Image.open(os.path.join(ASSETS, "office-desk-front.png")).convert("RGBA")
    b, f = DESK_SET["back"], DESK_SET["front"]

    def put(img, x, y, ww, hh):
        r = img.resize((int(round(ww * SCALE)), int(round(hh * SCALE))), Image.LANCZOS)
        canvas.alpha_composite(r, (int(x), int(y)))

    put(back, ox + b["leftTop"][0] * SCALE, oy + b["leftTop"][1] * SCALE, b["w"], b["h"])
    ax, ay = ox + dx * SCALE, oy + dy * SCALE
    canvas.alpha_composite(actor, (int(ax - actor.width / 2), int(ay - actor.height)))
    put(front, ox + f["leftTop"][0] * SCALE, oy + f["leftTop"][1] * SCALE, f["w"], f["h"])
    return canvas.convert("RGB")


def main():
    actor = prep_character(ACTOR)
    dxs = [-50, -15, 20]
    dys = [18, 2, -14]
    cell_w, cell_h = 300, 280
    lab = 26
    W = cell_w * len(dxs) + 20 * (len(dxs) + 1)
    H = (cell_h + lab) * len(dys) + 20 * (len(dys) + 1) + 30
    canvas = Image.new("RGB", (W, H), (250, 250, 252))
    d = ImageDraw.Draw(canvas)
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 17)
    except Exception:
        f = None
    d.text((20, 8), "正面帧标定扫描（行=dy 鞋底锚点，列=dx 水平偏移）", fill=(20, 20, 30), font=f)
    y = 30
    for dy in dys:
        x = 20
        for dx in dxs:
            img = render(actor, dx, dy)
            canvas.paste(img, (x, y + lab))
            d.text((x, y + 4), f"dx={dx}  dy={dy}", fill=(60, 60, 80), font=f)
            x += cell_w + 20
        y += cell_h + lab + 20
    p = os.path.join(HERE, "calib_sweep.png")
    canvas.save(p)
    print("saved", p, canvas.size)


if __name__ == "__main__":
    main()
