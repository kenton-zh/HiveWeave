"""新工位套件 + 3/4 视角人物的合成预览（离线，用于定标定值）。

素材：ws_alpha_C.png（参照图风格工位，含空椅，柔和接地阴影）
角色：Qwen 视角变换产出的 3/4 视角帧
"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = r"D:\PC_AI\Project\HiveWeave\tasks\qwen-edit-test"


def load_actor(path):
    im = Image.open(path).convert("RGBA")
    mask = im.convert("L").point(lambda v: 255 if v < 240 else 0)
    return im.crop(mask.getbbox())


def compose(ws, actor, ws_scale, actor_h, ax, ay):
    """ws_scale: 素材缩放；actor_h: 人物内容高度（最终画布像素）；
    ax, ay: 人物底部中心，**素材原始坐标系**（函数内部乘 ws_scale）。"""
    W = int(ws.width * ws_scale)
    H = int(ws.height * ws_scale)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    wsr = ws.resize((W, H), Image.LANCZOS)
    canvas.alpha_composite(wsr)
    a = actor.resize((max(1, int(round(actor.width * actor_h / actor.height))), int(round(actor_h))), Image.LANCZOS)
    canvas.alpha_composite(a, (int(ax * ws_scale - a.width / 2), int(ay * ws_scale - a.height)))
    return canvas


def checker_bg(size, cell=16):
    img = Image.new("RGB", size, (226, 226, 232))
    px = img.load()
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if ((x // cell) + (y // cell)) % 2 == 0:
                for yy in range(y, min(y + cell, size[1])):
                    for xx in range(x, min(x + cell, size[0])):
                        px[xx, yy] = (242, 242, 247)
    return img


def main():
    ws = Image.open(os.path.join(HERE, "ws_alpha_C.png")).convert("RGBA")
    actor = load_actor(os.path.join(HERE, "front34_f_1.5.png"))
    print("工位素材", ws.size, "人物帧", actor.size)

    WS = 0.62                     # 素材缩放
    variants = [
        ("A_小", 300, 470, 560),
        ("B_中", 370, 470, 575),
        ("C_大", 440, 470, 585),
    ]
    Z = 1.0
    imgs = []
    for name, ah, ax, ay in variants:
        img = compose(ws, actor, WS, ah, ax, ay)
        imgs.append((name, img))
    cw = max(i.width for _, i in imgs)
    ch = max(i.height for _, i in imgs)
    canvas = Image.new("RGB", (cw * len(imgs) + 20 * (len(imgs) + 1), ch + 60), (250, 250, 252))
    d = ImageDraw.Draw(canvas)
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 20)
    except Exception:
        f = None
    x = 20
    for name, img in imgs:
        bg = checker_bg(img.size)
        bg.paste(img, (0, 0), img)
        canvas.paste(bg, (x, 44))
        d.text((x, 16), f"{name} 人物高={dict(zip(['A_小','B_中','C_大'],[300,370,440]))[name]}", fill=(20, 20, 30), font=f)
        x += cw + 20
    p = os.path.join(HERE, "ws_actor_preview.png")
    canvas.save(p)
    print("saved", p, canvas.size)


if __name__ == "__main__":
    main()
