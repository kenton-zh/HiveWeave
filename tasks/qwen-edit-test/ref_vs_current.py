"""参照图（KAIROSOFT 目标风格）与当前 OfficeView 实现的并排对比。

对齐基准：显示器宽度（两图中都在 ~30px 量级），据此判断人物/桌椅的相对比例差。
"""
import os
from PIL import Image, ImageDraw, ImageFont

REF = r"C:\Users\99744\.workbuddy\clipboard-images\clipboard-2026-09-10T14-25-16-728Z-c97986ad.jpg"
CUR = r"C:\Users\99744\.workbuddy\clipboard-images\clipboard-2026-09-10T11-12-30-901Z-00479947.png"
OUT = r"D:\PC_AI\Project\HiveWeave\tasks\qwen-edit-test"


def main():
    ref = Image.open(REF).convert("RGB")
    cur = Image.open(CUR).convert("RGB")

    # 参照图：取一个含"坐姿人物 + 桌椅 + 显示器"的工位
    ref_crop = ref.crop((560, 380, 900, 640))          # 340x260
    cur_crop = cur                                       # 295x205

    Z = 3
    ref_big = ref_crop.resize((ref_crop.width * Z, ref_crop.height * Z), Image.LANCZOS)
    cur_big = cur_crop.resize((cur_crop.width * Z, cur_crop.height * Z), Image.LANCZOS)

    gap = 30
    W = ref_big.width + cur_big.width + gap * 3
    H = max(ref_big.height, cur_big.height) + 60
    canvas = Image.new("RGB", (W, H), (248, 248, 252))
    canvas.paste(ref_big, (gap, 50))
    canvas.paste(cur_big, (ref_big.width + gap * 2, 50))

    d = ImageDraw.Draw(canvas)
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 24)
        fs = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 18)
    except Exception:
        f = fs = None
    d.text((gap, 14), "参照目标（KAIROSOFT 风格）", fill=(20, 20, 30), font=f)
    d.text((ref_big.width + gap * 2, 14), "当前 OfficeView 实现", fill=(180, 40, 40), font=f)
    d.text((gap, H - 40), "人物正面 3/4 朝观众 / 与桌椅同源 / 柔和接地阴影", fill=(60, 60, 70), font=fs)
    d.text((ref_big.width + gap * 2, H - 40), "人物侧视 / 与桌椅不同源 / 纯黑硬边阴影", fill=(180, 40, 40), font=fs)

    p = os.path.join(OUT, "ref_vs_current.png")
    canvas.save(p)
    print("saved", p, canvas.size)


if __name__ == "__main__":
    main()
