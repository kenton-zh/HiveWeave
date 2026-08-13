#!/usr/bin/env python3
"""人工修正角色服装跨图不一致：按区域重映射到标准配色（worker_base_0）。

标准配色（01_base）：
  外套 g(95,87,79) + L(194,195,199) 高光，内搭 t(162,136,121)
  裤子 P(131,118,156)，发 V(66,33,54)+k(41,24,20)，肤 t(162,136,121)

不一致类型 → 处理：
  白衬衫 W(255,241,232)       → 躯干区 W→g
  深蓝外套 D(29,43,83)/N(17,29,53) → 躯干区 D/N→g
  紫外套 V(66,33,54)          → 仅躯干区 V→g（头部保留发色）
  灰紫 v(73,51,59)            → 躯干区 v→L，头部 v→V（发色统一）
  浅肤 S(255,204,170)/s(255,157,129) → 全区 S/s→t
"""

import shutil
from pathlib import Path

import numpy as np
from PIL import Image

FRAMES_DIR = Path(r"D:\PC_AI\Project\HiveWeave\assets\mvp\frames\role")
BACKUP_DIR = Path(r"c:\Users\99744\.trae-cn\work\6a7d5628982673ac32dd6f99\frames_backup")

# ---- 标准配色 (from worker_base_0) ----
STD_JACKET    = (95, 87, 79)      # g - 灰外套
STD_JACKET_HI = (194, 195, 199)   # L - 浅灰高光
STD_SKIN      = (162, 136, 121)   # t - 棕褐肤/内搭
STD_HAIR      = (66, 33, 54)      # V - 暗紫发
STD_PANTS     = (131, 118, 156)   # P - 紫灰裤

# ---- 变体颜色 ----
WHITE        = (255, 241, 232)  # W - 白衬衫
LIGHT_SKIN   = (255, 204, 170)  # S - 浅肤
LIGHTER_SKIN = (255, 157, 129)  # s - 更浅肤
DARK_NAVY    = (29, 43, 83)     # D - 深蓝外套
DARK_NAVY2   = (17, 29, 53)     # N - 更深蓝
PURPLE       = (66, 33, 54)     # V - 紫外套（=标准发色，需区域隔离）
GRAY_PURPLE  = (73, 51, 59)     # v - 灰紫

# ---- 映射表 ----
# 肤色标准化（全区域应用）
SKIN_MAP = {
    LIGHT_SKIN: STD_SKIN,
    LIGHTER_SKIN: STD_SKIN,
}

# 躯干外套映射（y head_end ~ 14）
JACKET_MAP = {
    WHITE: STD_JACKET,         # 白 → 灰
    DARK_NAVY: STD_JACKET,     # 深蓝 → 灰
    DARK_NAVY2: STD_JACKET,    # 更深蓝 → 灰
    PURPLE: STD_JACKET,        # 紫 → 灰（仅躯干！头部保留发色）
    GRAY_PURPLE: STD_JACKET_HI # 灰紫 → 浅灰高光
}

# 发色映射（仅头部 y < head_end）
HAIR_MAP = {
    GRAY_PURPLE: STD_HAIR,  # 灰紫发 → 暗紫发
}

# ---- 区域边界 ----
# 不同 pose 头/躯干边界不同（前倾 → 头延伸更低）
HEAD_END_MAP = {
    "worker_typing": 9,
    "worker_land": 8,
    "worker_jump": 7,
}
DEFAULT_HEAD_END = 6

# 躯干/腿边界：typing/curly_base/long_base 等白衫或手臂延伸到 y 15-17
TORSO_END_MAP = {
    "worker_typing": 18,
    "worker_curly_base": 18,
    "worker_long_base": 18,
    "worker_walk": 17,
    "worker_jump": 17,
    "worker_nod": 17,
    "worker_deliver": 17,
    "worker_coffee": 17,
}
DEFAULT_TORSO_END = 15


def get_head_end(name: str) -> int:
    for prefix, val in HEAD_END_MAP.items():
        if name.startswith(prefix):
            return val
    return DEFAULT_HEAD_END


def get_torso_end(name: str) -> int:
    for prefix, val in TORSO_END_MAP.items():
        if name.startswith(prefix):
            return val
    return DEFAULT_TORSO_END


def recolor(path: Path) -> int:
    """重映射单帧颜色，返回修改像素数。"""
    name = path.stem
    im = Image.open(path).convert("RGBA")
    a = np.array(im)
    h, w = a.shape[:2]
    he = get_head_end(name)
    te = get_torso_end(name)
    changed = 0

    for y in range(h):
        for x in range(w):
            if a[y, x, 3] == 0:
                continue
            c = tuple(a[y, x, :3])
            new_c = None

            # 全局肤色标准化
            if c in SKIN_MAP:
                new_c = SKIN_MAP[c]
            # 头部发色统一
            elif y < he and c in HAIR_MAP:
                new_c = HAIR_MAP[c]
            # 躯干外套统一
            elif he <= y < te and c in JACKET_MAP:
                new_c = JACKET_MAP[c]

            if new_c and new_c != c:
                a[y, x, :3] = new_c
                changed += 1

    if changed:
        Image.fromarray(a, "RGBA").save(path)
    return changed


def main() -> None:
    # 备份
    if BACKUP_DIR.exists():
        shutil.rmtree(BACKUP_DIR)
    shutil.copytree(FRAMES_DIR, BACKUP_DIR)
    print(f"备份 → {BACKUP_DIR}\n")

    total = 0
    for p in sorted(FRAMES_DIR.glob("*.png")):
        n = recolor(p)
        total += n
        tag = f"{n:4d} px" if n else "   OK"
        print(f"  {p.stem:30s} {tag}")
    print(f"\n合计修改 {total} 像素")


if __name__ == "__main__":
    main()
