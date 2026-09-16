"""白底泛洪抠像：从四边吃掉近白背景，保留主体与其柔和接地阴影。

与 skill 里 make_spritesheet.white_to_alpha 同思路：
泛洪而非全局阈值（保护桌面/白衬衫等内部白色），再羽化 alpha 去白边。
"""
import os
from collections import deque

import numpy as np
from PIL import Image, ImageFilter


def white_to_alpha(img: Image.Image, thresh: int = 236, feather: int = 1) -> Image.Image:
    rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
    h, w, _ = rgb.shape
    # "近白"候选
    near_white = (rgb[:, :, 0] >= thresh) & (rgb[:, :, 1] >= thresh) & (rgb[:, :, 2] >= thresh)

    # 从四边泛洪，只连通到边缘的背景才算背景
    bg = np.zeros((h, w), dtype=bool)
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if near_white[y, x] and not bg[y, x]:
                bg[y, x] = True
                dq.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if near_white[y, x] and not bg[y, x]:
                bg[y, x] = True
                dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and near_white[ny, nx] and not bg[ny, nx]:
                bg[ny, nx] = True
                dq.append((ny, nx))

    alpha = np.where(bg, 0, 255).astype(np.uint8)
    out = Image.fromarray(np.dstack([rgb, alpha]), mode="RGBA")
    if feather > 0:
        # 轻微收缩 + 模糊，去掉白边残留
        a = out.split()[3].filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.6))
        out.putalpha(a)
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("ws_white_C.png", "ws_white_F.png"):
        src = os.path.join(here, name)
        im = Image.open(src)
        rgba = white_to_alpha(im)
        bbox = rgba.split()[3].getbbox()
        cropped = rgba.crop(bbox)
        dst = os.path.join(here, name.replace("ws_white_", "ws_alpha_"))
        cropped.save(dst)
        total = cropped.width * cropped.height
        opaque = int((np.asarray(cropped.split()[3]) > 128).sum())
        print(f"{name}: {im.size} -> alpha {cropped.size} bbox={bbox} 不透明占比 {opaque/total:.1%} -> {os.path.basename(dst)}")


if __name__ == "__main__":
    main()
