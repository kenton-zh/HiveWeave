#!/usr/bin/env python3
"""Pass 3: 色数收敛到 ≤8（§10.3 第 4 条）。

多余色全局合并到最近标准色：
  D(29,43,83)  → V(66,33,54)   深蓝发/腿 → 暗紫
  N(17,29,53)  → k(41,24,20)   更深蓝 → 暗棕
  v(73,51,59)  → k(41,24,20)   灰紫鞋 → 暗棕
  W(255,241,232) → g(95,87,79) 残余白 → 灰
"""

from pathlib import Path

import numpy as np
from PIL import Image

FRAMES_DIR = Path(r"D:\PC_AI\Project\HiveWeave\assets\mvp\frames\role")

D = (29, 43, 83)
N = (17, 29, 53)
V = (66, 33, 54)
k_ = (41, 24, 20)
v_ = (73, 51, 59)
g_ = (95, 87, 79)
W = (255, 241, 232)

MERGE = {
    D: V,
    N: k_,
    v_: k_,
    W: g_,
}


def reduce_frame(path: Path) -> int:
    im = Image.open(path).convert("RGBA")
    a = np.array(im)
    changed = 0
    for src, dst in MERGE.items():
        hit = np.all(a[:, :, :3] == src, axis=2) & (a[:, :, 3] > 0)
        if hit.any():
            a[hit, :3] = dst
            changed += int(hit.sum())
    if changed:
        Image.fromarray(a, "RGBA").save(path)
    return changed


def main() -> None:
    total = 0
    for p in sorted(FRAMES_DIR.glob("*.png")):
        n = reduce_frame(p)
        total += n
        if n:
            print(f"  {p.stem:30s} {n:4d} px merged")
    print(f"\n合计合并 {total} 像素")

    # 验证色数
    print("\n色数验证:")
    over = 0
    for p in sorted(FRAMES_DIR.glob("*.png")):
        a = np.array(Image.open(p).convert("RGBA"))
        op = a[:, :, 3] > 0
        if not op.any():
            continue
        n = len(np.unique(a[op][:, :3].reshape(-1, 3), axis=0))
        if n > 8:
            print(f"  ⚠ {p.stem}: {n} colors")
            over += 1
    if not over:
        print("  全部 ≤8 ✓")


if __name__ == "__main__":
    main()
