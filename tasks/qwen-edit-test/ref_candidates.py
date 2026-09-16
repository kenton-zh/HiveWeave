"""从参照图里挑候选工位，放大对比，选最干净、最完整的一个做模板。"""
import os
from PIL import Image, ImageDraw, ImageFont

REF = r"C:\Users\99744\.workbuddy\clipboard-images\clipboard-2026-09-10T14-25-16-728Z-c97986ad.jpg"
OUT = r"D:\PC_AI\Project\HiveWeave\tasks\qwen-edit-test"

CANDIDATES = {
    "A_中央双人工位": (560, 390, 900, 660),
    "B_右下工位": (760, 430, 1040, 690),
    "C_右上工位": (1010, 250, 1290, 520),
    "D_左中工位": (330, 290, 620, 560),
    "E_中下工位": (560, 560, 900, 830),
    "F_右中工位": (1080, 480, 1360, 750),
}


def main():
    ref = Image.open(REF).convert("RGB")
    Z = 2
    cells = []
    for name, box in CANDIDATES.items():
        c = ref.crop(box)
        cells.append((name, c.resize((c.width * Z, c.height * Z), Image.LANCZOS)))
    cols = 3
    cw = max(c.width for _, c in cells) + 20
    ch = max(c.height for _, c in cells) + 34
    W, H = cw * cols + 20, ch * 2 + 40
    canvas = Image.new("RGB", (W, H), (250, 250, 252))
    d = ImageDraw.Draw(canvas)
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 20)
    except Exception:
        f = None
    for i, (name, img) in enumerate(cells):
        r, c = divmod(i, cols)
        x = 20 + c * cw
        y = 20 + r * ch
        canvas.paste(img, (x, y + 28))
        d.text((x, y + 2), name, fill=(20, 20, 30), font=f)
    p = os.path.join(OUT, "ref_candidates.png")
    canvas.save(p)
    print("saved", p, canvas.size)


if __name__ == "__main__":
    main()
