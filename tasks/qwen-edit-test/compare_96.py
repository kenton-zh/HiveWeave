"""验证：编辑提质后的帧压到 96×96 sprite 规格后，是否真的比 H3 原帧清晰。

做法：原帧与编辑帧按同一规则（内容 bbox → 缩放到固定高度 → 96×96 画布底对齐 y=84）
处理，再并排放大，观察线稿可读性差异。
"""
import os
from PIL import Image

HERE = r"D:\PC_AI\Project\HiveWeave\tasks\qwen-edit-test"


def to_sprite(path, content_h=80, canvas=96, base_y=84):
    im = Image.open(path).convert("RGB")
    # 内容 bbox：非白像素
    gray = im.convert("L")
    mask = gray.point(lambda v: 255 if v < 240 else 0)
    bbox = mask.getbbox()
    im = im.crop(bbox)
    scale = content_h / im.height
    nw, nh = max(1, round(im.width * scale)), content_h
    im = im.resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGB", (canvas, canvas), (255, 255, 255))
    x = (canvas - nw) // 2
    y = base_y - nh
    out.paste(im, (max(0, x), max(0, y)))
    return out


def main():
    a = to_sprite(os.path.join(HERE, "frame50.png"))
    b = to_sprite(os.path.join(HERE, "test_clean.png"))
    Z = 8
    gap = 40
    W = 96 * Z * 2 + gap * 3
    H = 96 * Z + 70
    canvas = Image.new("RGB", (W, H), (246, 246, 250))
    canvas.paste(a.resize((96 * Z, 96 * Z), Image.NEAREST), (gap, 50))
    canvas.paste(b.resize((96 * Z, 96 * Z), Image.NEAREST), (96 * Z + gap * 2, 50))
    from PIL import ImageDraw
    d = ImageDraw.Draw(canvas)
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 22)
    except Exception:
        f = None
    d.text((gap, 16), "H3 原帧 -> 96x96", fill=(20, 20, 30), font=f)
    d.text((96 * Z + gap * 2, 16), "Qwen 编辑后 -> 96x96", fill=(20, 20, 30), font=f)
    p = os.path.join(HERE, "compare_96.png")
    canvas.save(p)
    print("saved", p, canvas.size)

    # 客观指标：边缘密度（清晰度代理）+ 文件信息量
    for name, img in (("原帧", a), ("编辑帧", b)):
        g = img.convert("L")
        # 相邻像素差绝对值均值，越大越锐
        px = list(g.getdata())
        w = 96
        diff = sum(abs(px[i] - px[i + 1]) for i in range(len(px) - 1) if (i + 1) % w != 0)
        cnt = sum(1 for i in range(len(px) - 1) if (i + 1) % w != 0)
        print(f"  {name}: 水平相邻像素平均差 = {diff / cnt:.2f}")


if __name__ == "__main__":
    from PIL import ImageFont  # noqa: E402

    main()
