#!/usr/bin/env python3
"""HiveWeave 像素办公室资产管线（规格：docs/前端像素办公室规格.md §10.1）。

AI 参考图 (JPG) → 显式单元格切图 → 白底键控 → 连通域清理（去文字/画框/噪点）
→ 下采样 → PICO-8 32 色板量化 → spritesheet 打包（Aseprite JSON）+ §10.3 校验报告。

设计说明：AI 参考图网格不规则、标签位置飘忽、白底斑驳，自动 blob 检测不可靠
（相邻精灵合并 / 文字误检 / 整列漏检）。改为按 debug 叠加图人工标定的分数坐标
单元格（IMAGES 清单），格内键控 + 连通域过滤，映射确定性 100%。

用法：
  python pixel_pipeline.py --detect   # 只画单元格 debug 叠加图（校准用）
  python pixel_pipeline.py            # 完整管线
"""
from __future__ import annotations

import json
import math
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from scipy import ndimage

SRC = Path(r"D:\PC_AI\Project\HiveWeave\assets\mvp")
OUT_SHEETS = SRC / "sheets"
OUT_FRAMES = SRC / "frames"
DEBUG_DIR = Path(r"c:\Users\99744\.trae-cn\work\6a7d5628982673ac32dd6f99\pipeline_debug")

ALPHA_THRESHOLD = 140
BG_BAND_KEY = 32      # 键控：背景泛洪容差（抗 AI 白底斑驳）
MIN_COMP_RATIO = 0.01  # 连通域清理：保留 ≥ 最大连通域 1% 的分离部件（10% 在源分辨率下误删问号点/烟团）
MIN_COMP_FLOOR = 20    # 连通域清理：绝对面积下限（px²）
MIN_OPAQUE_WARN = 12   # 成品帧不透明像素 < 此值 → 报告「疑似空帧」

# §4.1 PICO-8 32 色板
PALETTE_HEX = [
    "#000000", "#1D2B53", "#7E2553", "#008751", "#AB5236", "#5F574F", "#C2C3C7", "#FFF1E8",
    "#FF004D", "#FFA300", "#FFEC27", "#00E436", "#29ADFF", "#83769C", "#FF77A8", "#FFCCAA",
    "#291814", "#111D35", "#422136", "#125359", "#742F29", "#49333B", "#A28879", "#F3EF7D",
    "#BE1250", "#FF6C24", "#A8E72E", "#00B543", "#065AB5", "#754665", "#FF6E59", "#FF9D81",
]
PALETTE = np.array(
    # int32：int16 下色差平方溢出回绕（204²→-23920），近色判定全错
    [tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) for h in PALETTE_HEX], dtype=np.int32
)

ROLE, OBJ, TILE, FX = "role", "obj", "tile", "fx"
MAX_COLORS = {ROLE: 8, OBJ: 16, TILE: 16, FX: 8}
CELL = {ROLE: (16, 24), OBJ: (32, 32), TILE: (16, 16), FX: (24, 24)}
SHEET_NAME = {ROLE: "role_sheet", OBJ: "obj_sheet", TILE: "tile_sheet", FX: "fx_sheet"}


@dataclass(frozen=True)
class Slot:
    name: str
    size: tuple[int, int]
    cat: str
    opaque: bool = False        # True = 无透明（场景 tile）
    key_interior: bool = False  # True = 键掉所有近白像素（镂空窗框）
    key_band: int = BG_BAND_KEY  # 键控容差（白上白物件可调小保灰阶阴影）


@dataclass(frozen=True)
class Cell:
    rect: tuple[float, float, float, float]  # (x0, y0, x1, y1) 占图宽高的分数
    slot: Slot


@dataclass(frozen=True)
class ImgCfg:
    file: str
    cells: tuple[Cell, ...]


R16 = (16, 24)


def C(rect: tuple[float, float, float, float], name: str, size: tuple[int, int] = R16,
      cat: str = ROLE, **kw: object) -> Cell:
    return Cell(rect, Slot(name, size, cat, **kw))


# ---------------------------------------------------------------- 单元格清单
# 分数坐标按 pipeline_debug/ 叠加图标定；标签文字一律排除在 rect 外（或靠连通域清理丢弃）。

IMAGES: tuple[ImgCfg, ...] = (
    ImgCfg("characters/01_base_and_idle.jpg", cells=(
        C((0.15, 0.28, 0.33, 0.80), "worker_base_0"),
        C((0.48, 0.28, 0.65, 0.80), "worker_idle_0"),
        C((0.75, 0.28, 0.91, 0.80), "worker_idle_1"),
    )),
    ImgCfg("characters/02_typing_v2.jpg", cells=(  # 2560×1440，角色单体 4 帧一行
        C((0.06, 0.20, 0.24, 0.80), "worker_typing_0"),
        C((0.30, 0.20, 0.47, 0.80), "worker_typing_1"),
        C((0.545, 0.20, 0.715, 0.80), "worker_typing_2"),
        C((0.77, 0.20, 0.94, 0.80), "worker_typing_3"),
    )),
    ImgCfg("characters/03_walk_cycle.jpg", cells=(  # 只取 DOWN/RIGHT 两行；UP 由 07 替代，LEFT 由 RIGHT 镜像
        C((0.20, 0.02, 0.32, 0.215), "worker_walk_down_0"),
        C((0.39, 0.02, 0.53, 0.215), "worker_walk_down_1"),
        C((0.58, 0.02, 0.735, 0.215), "worker_walk_down_2"),
        C((0.81, 0.02, 0.95, 0.215), "worker_walk_down_3"),
        C((0.20, 0.755, 0.32, 0.98), "worker_walk_right_0"),
        C((0.39, 0.755, 0.53, 0.98), "worker_walk_right_1"),
        C((0.58, 0.755, 0.735, 0.98), "worker_walk_right_2"),
        C((0.81, 0.755, 0.95, 0.98), "worker_walk_right_3"),
    )),
    ImgCfg("characters/04_coffee_nod_jump_deliver.jpg", cells=(
        C((0.215, 0.03, 0.335, 0.25), "worker_coffee_0"),
        C((0.40, 0.03, 0.565, 0.25), "worker_coffee_1"),
        C((0.60, 0.03, 0.735, 0.25), "worker_coffee_2"),
        C((0.81, 0.03, 0.93, 0.25), "worker_coffee_3"),
        C((0.215, 0.265, 0.335, 0.495), "worker_nod_0"),
        C((0.415, 0.265, 0.54, 0.495), "worker_nod_1"),
        C((0.215, 0.51, 0.335, 0.73), "worker_jump_0"),
        C((0.415, 0.51, 0.54, 0.73), "worker_jump_1"),
        C((0.215, 0.755, 0.335, 0.98), "worker_deliver_0"),
        C((0.415, 0.755, 0.56, 0.98), "worker_deliver_1"),
    )),
    ImgCfg("characters/05_question_and_smoke.jpg", cells=(
        C((0.24, 0.11, 0.46, 0.45), "worker_question_0"),
        C((0.565, 0.11, 0.80, 0.45), "worker_question_1"),
        C((0.02, 0.565, 0.215, 0.95), "worker_smoke_0"),
        C((0.28, 0.565, 0.48, 0.95), "worker_smoke_1"),
        C((0.585, 0.565, 0.79, 0.95), "worker_smoke_2"),
        C((0.815, 0.565, 1.0, 0.95), "worker_smoke_3"),
    )),
    ImgCfg("characters/06_land_parachute.jpg", cells=(
        C((0.055, 0.24, 0.34, 0.84), "worker_land_0"),
        C((0.40, 0.34, 0.62, 0.84), "worker_land_1"),
        C((0.665, 0.44, 0.955, 0.85), "worker_land_2"),
    )),
    ImgCfg("characters/07_walk_up_backview.jpg", cells=(
        C((0.115, 0.29, 0.27, 0.73), "worker_walk_up_0"),
        C((0.34, 0.29, 0.49, 0.73), "worker_walk_up_1"),
        C((0.56, 0.29, 0.72, 0.73), "worker_walk_up_2"),
        C((0.79, 0.29, 0.95, 0.73), "worker_walk_up_3"),
    )),
    ImgCfg("characters/08_hair_variants.jpg", cells=(
        C((0.13, 0.055, 0.33, 0.475), "worker_curly_base_0"),
        C((0.48, 0.055, 0.68, 0.475), "worker_curly_idle_0"),
        C((0.78, 0.055, 0.98, 0.475), "worker_curly_idle_1"),
        C((0.13, 0.50, 0.33, 0.95), "worker_long_base_0"),
        C((0.48, 0.50, 0.68, 0.95), "worker_long_idle_0"),
        C((0.78, 0.50, 0.98, 0.95), "worker_long_idle_1"),
    )),
    ImgCfg("objects/01_workstation_set.jpg", cells=(
        C((0.05, 0.30, 0.34, 0.75), "desk", (32, 24), OBJ),
        C((0.41, 0.30, 0.62, 0.75), "computer", (16, 16), OBJ),
        C((0.70, 0.30, 0.92, 0.75), "chair", (16, 16), OBJ),
    )),
    ImgCfg("objects/02_decor_items.jpg", cells=(
        C((0.36, 0.01, 0.65, 0.245), "coffee_machine", (16, 24), OBJ),
        C((0.36, 0.325, 0.65, 0.55), "plant", (16, 24), OBJ),
        C((0.36, 0.65, 0.65, 0.865), "cabinet", (16, 24), OBJ),
    )),
    ImgCfg("objects/03_large_furniture.jpg", cells=(
        C((0.59, 0.10, 0.85, 0.34), "whiteboard", (32, 24), OBJ),
        C((0.15, 0.58, 0.42, 0.81), "sofa", (32, 16), OBJ, key_band=16),  # 小容差防泛洪漏进坐垫浅色扣面
        C((0.56, 0.57, 0.90, 0.80), "round_table", (32, 32), OBJ),
    )),
    ImgCfg("objects/04_architectural_tiles.jpg", cells=(
        C((0.515, 0.165, 0.735, 0.33), "cloud_0", (16, 8), TILE, key_band=8),
        C((0.765, 0.17, 0.985, 0.33), "cloud_1", (16, 8), TILE, key_band=8),
        C((0.03, 0.59, 0.18, 0.81), "floor_corridor", (16, 16), TILE, opaque=True),
        C((0.195, 0.59, 0.345, 0.81), "floor_workstation", (16, 16), TILE, opaque=True),
        C((0.37, 0.615, 0.495, 0.785), "floor_ceo", (16, 16), TILE, opaque=True),
        C((0.575, 0.59, 0.75, 0.81), "wall_beige", (16, 16), TILE, opaque=True),
        C((0.775, 0.59, 0.95, 0.81), "wall_brick", (16, 16), TILE, opaque=True),
    )),
    ImgCfg("objects/05_window_frame_hollow.jpg", cells=(
        C((0.22, 0.10, 0.78, 0.64), "window_frame", (32, 24), OBJ, key_interior=True, key_band=45),
        C((0.335, 0.65, 0.66, 0.925), "window_frame_alt", (32, 24), OBJ, key_interior=True, key_band=45),
    )),
    ImgCfg("effects/01_ui_icons_and_items.jpg", cells=(
        C((0.02, 0.15, 0.19, 0.35), "red_x_0", (16, 16), FX),
        C((0.265, 0.15, 0.42, 0.35), "red_x_1", (16, 16), FX),
        C((0.52, 0.15, 0.68, 0.35), "check_0", (16, 16), FX),
        C((0.76, 0.15, 0.92, 0.35), "check_1", (16, 16), FX),
        C((0.10, 0.485, 0.53, 0.85), "task_card", (24, 16), FX),
        C((0.63, 0.485, 0.82, 0.91), "report_roll", (16, 24), FX),
    )),
    ImgCfg("effects/02_smoke_dust_fire.jpg", cells=(  # rect 内缩避开手绘相框
        C((0.057, 0.193, 0.168, 0.380), "smoke_black_0", (16, 16), FX),
        C((0.218, 0.193, 0.340, 0.380), "smoke_black_1", (16, 16), FX),
        C((0.369, 0.193, 0.500, 0.380), "smoke_black_2", (16, 16), FX),
        C((0.584, 0.199, 0.715, 0.367), "dust_0", (16, 16), FX),
        C((0.789, 0.199, 0.920, 0.367), "dust_1", (16, 16), FX),
        C((0.203, 0.642, 0.363, 0.855), "fire_0", (16, 16), FX),
        C((0.428, 0.642, 0.588, 0.855), "fire_1", (16, 16), FX),
        C((0.647, 0.642, 0.807, 0.855), "fire_2", (16, 16), FX),
    )),
    ImgCfg("effects/03_celebration_effects.jpg", cells=(  # confetti 程序合成（见 synth_confetti）
        C((0.045, 0.20, 0.135, 0.32), "star_0", (8, 8), FX),
        C((0.175, 0.135, 0.315, 0.335), "star_1", (8, 8), FX),
        C((0.36, 0.195, 0.45, 0.315), "star_2", (8, 8), FX),
        C((0.107, 0.547, 0.381, 0.859), "glow_0", (24, 24), FX),
        C((0.625, 0.566, 0.864, 0.859), "glow_1", (24, 24), FX),
    )),
    ImgCfg("effects/04_door_and_window_frame.jpg", cells=(  # 角件弃用（程序合成九宫格）
        C((0.148, 0.13, 0.375, 0.49), "door_closed", (32, 32), OBJ),
        C((0.575, 0.13, 0.815, 0.50), "door_open", (32, 32), OBJ),
    )),
)

# 派生地板（规格 6 种，AI 只出 3 种，程序调色衍生）
DERIVED_FLOOR = (  # name, 源帧, 目标主色
    ("floor_carpet", "floor_workstation", "#422136"),
    ("floor_teal", "floor_workstation", "#125359"),
    ("floor_gray", "floor_corridor", "#49333B"),
)


# ---------------------------------------------------------------- 键控与清理

def bg_color(rgb: np.ndarray) -> np.ndarray:
    """边框带中位数作为背景色（int16 RGB 图 → 3 向量）。"""
    b = 20
    strip = np.concatenate([
        rgb[:b].reshape(-1, 3), rgb[-b:].reshape(-1, 3),
        rgb[:, :b].reshape(-1, 3), rgb[:, -b:].reshape(-1, 3),
    ])
    return np.median(strip, axis=0).astype(np.int16)


def key_background(crop: np.ndarray, slot: Slot) -> np.ndarray:
    """背景泛洪 → 透明。默认只键与边框相连的背景（保白衬衫/白伞盖）；
    key_interior 键全部近白像素（镂空窗框）。"""
    rgb = crop[:, :, :3].astype(np.int16)
    bglike = np.abs(rgb - bg_color(rgb)).max(axis=2) <= slot.key_band
    if slot.key_interior:
        bg = bglike
    else:
        lab, _ = ndimage.label(bglike, structure=np.ones((3, 3), bool))
        border = set(np.unique(np.concatenate([lab[0, :], lab[-1, :], lab[:, 0], lab[:, -1]])).tolist()) - {0}
        bg = np.isin(lab, list(border)) if border else np.zeros_like(bglike)
    out = crop.copy()
    out[bg, 3] = 0
    return out


def drop_artifacts(arr: np.ndarray) -> np.ndarray:
    """键控后清理连通域：
    1) 丢「贴边且非最大」连通域（手绘相框残片、贴边文字）——最大连通域即使贴边也保留
       （烟雾帧可能轻微触边，裁切可接受，整丢不可接受）；
    2) 丢面积 < max(MIN_COMP_FLOOR, 最大连通域×MIN_COMP_RATIO) 的连通域（文字/噪点），
       保留足够大的分离部件（问号 / 烟 / 咖啡杯 / 降落伞绳）。"""
    alpha = arr[:, :, 3] > 0
    lab, n = ndimage.label(alpha, structure=np.ones((3, 3), bool))
    if n == 0:
        return arr
    sizes = np.bincount(lab.ravel(), minlength=n + 1)
    sizes[0] = 0
    biggest = int(sizes[1:].max())
    border = set(np.unique(np.concatenate([lab[0, :], lab[-1, :], lab[:, 0], lab[:, -1]])).tolist()) - {0}
    keep = set()
    for i in range(1, n + 1):
        if i in border and sizes[i] < biggest:
            continue
        if sizes[i] < max(MIN_COMP_FLOOR, biggest * MIN_COMP_RATIO):
            continue
        keep.add(i)
    out = arr.copy()
    out[alpha & ~np.isin(lab, list(keep)), 3] = 0
    return out


def trim(arr: np.ndarray) -> np.ndarray:
    alpha = arr[:, :, 3] > 0
    if not alpha.any():
        return arr
    ys, xs = np.where(alpha)
    return arr[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]


def fit_and_anchor(im: Image.Image, size: tuple[int, int], anchor: str,
                   fill: bool = False) -> Image.Image:
    """等比缩放进目标格；角色脚底锚定，其余居中。最近邻缩小（§10.1），硬边由量化兜底。
    fill=True（不透明 tile）直接拉伸铺满——等比留白边会在强制不透明时变成黑边。"""
    tw, th = size
    if fill:
        return im.resize((tw, th), Image.NEAREST)
    scale = min(tw / im.width, th / im.height)
    nw, nh = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    im = im.resize((nw, nh), Image.NEAREST)
    canvas = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    x = (tw - nw) // 2
    y = th - nh if anchor == "bottom" else (th - nh) // 2
    canvas.paste(im, (x, y), im)
    return canvas


def quantize(im: Image.Image, max_colors: int) -> Image.Image:
    """映射到 §4.1 的 32 色板，并限制使用色数 ≤ max_colors（§10.3 第 4 条）。
    alpha 阈值归一：半透明边缘直接落透明，保证硬边无抗锯齿（§10.3 第 3 条）。"""
    arr = np.array(im)
    opaque = arr[:, :, 3] >= ALPHA_THRESHOLD
    out = np.zeros((*arr.shape[:2], 4), dtype=np.uint8)
    if not opaque.any():
        return Image.fromarray(out, "RGBA")
    px = arr[opaque][:, :3].astype(np.int32)  # int32：与 PALETTE 同步防溢出（H1）
    d = ((px[:, None, :] - PALETTE[None, :, :]) ** 2).sum(axis=2)
    idx = d.argmin(axis=1)
    counts = np.bincount(idx, minlength=len(PALETTE))
    keep = np.argsort(counts)[::-1][:max_colors]
    keep = keep[counts[keep] > 0]
    remap = ~np.isin(idx, keep)
    if remap.any():
        d2 = ((px[remap][:, None, :] - PALETTE[keep][None, :, :]) ** 2).sum(axis=2)
        idx[remap] = keep[d2.argmin(axis=1)]
    out[opaque, :3] = PALETTE[idx].astype(np.uint8)
    out[opaque, 3] = 255
    return Image.fromarray(out, "RGBA")


def extract_cell(img: np.ndarray, cell: Cell) -> Image.Image:
    """按分数 rect 裁切 → 键控 → 连通域清理 → trim → 缩放锚定 → 量化。"""
    slot = cell.slot
    h, w = img.shape[:2]
    x0f, y0f, x1f, y1f = cell.rect
    x0, y0 = max(0, int(x0f * w)), max(0, int(y0f * h))
    x1, y1 = min(w, math.ceil(x1f * w)), min(h, math.ceil(y1f * h))
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"{slot.name}: rect 退化 {cell.rect}")
    crop = np.dstack([img[y0:y1, x0:x1, :3], np.full((y1 - y0, x1 - x0), 255, np.uint8)])
    keyed = crop if slot.opaque else drop_artifacts(key_background(crop, slot))
    trimmed = trim(keyed)
    fitted = fit_and_anchor(Image.fromarray(trimmed, "RGBA"), slot.size,
                            "bottom" if slot.cat == ROLE else "center", fill=slot.opaque)
    if slot.opaque:
        arr = np.array(fitted)
        arr[:, :, 3] = 255  # tile 全格不透明
        fitted = Image.fromarray(arr, "RGBA")
    return quantize(fitted, MAX_COLORS[slot.cat])


# ---------------------------------------------------------------- 派生合成

def recolor(frame: Image.Image, target_hex: str) -> Image.Image:
    """把帧内出现次数最多的颜色替换为目标色（地板变体衍生）。"""
    arr = np.array(frame)
    opaque = arr[:, :, 3] > 0
    if not opaque.any():
        return frame
    px = arr[opaque][:, :3]
    colors, counts = np.unique(px.reshape(-1, 3), axis=0, return_counts=True)
    main = colors[counts.argmax()]
    tgt = np.array([int(target_hex[i:i + 2], 16) for i in (1, 3, 5)], np.uint8)
    hit = np.all(arr[:, :, :3] == main, axis=2) & opaque
    arr[hit, :3] = tgt
    return Image.fromarray(arr, "RGBA")


def synth_nine_grid() -> Image.Image:
    """窗框九宫格 24×24（3×3 个 8×8 单元）：4px 像素圆角，深蓝描边，中心透明。"""
    s = 24
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle([0, 0, s - 1, s - 1], radius=4,
                         outline=(17, 29, 53, 255), width=2)  # #111D35
    return im


def synth_confetti() -> list[tuple[str, Image.Image]]:
    """彩带 4×4 三变体（金/粉/蓝，§10.7）：散落小色块，程序绘制比从散点图抽取可靠。"""
    specs = (  # 颜色/形状变体非动画帧（§10.7），命名不带 _N 以免被打成 forward 动画 tag
        ("confetti_gold", "#FFEC27", ((1, 0), (2, 0), (1, 1), (2, 1), (1, 2), (2, 2))),  # 竖条
        ("confetti_pink", "#FF77A8", ((0, 1), (1, 1), (2, 1), (3, 1), (1, 2), (2, 2))),  # 横块
        ("confetti_blue", "#29ADFF", ((1, 1), (2, 1), (1, 2), (2, 2), (2, 3))),          # 折块
    )
    out = []
    for name, hex_color, px in specs:
        im = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
        rgb = tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        for x, y in px:
            im.putpixel((x, y), (*rgb, 255))
        out.append((name, im))
    return out


# ---------------------------------------------------------------- 校验

def frame_colors(im: Image.Image) -> int:
    arr = np.array(im)
    opaque = arr[:, :, 3] > 0
    if not opaque.any():
        return 0
    return int(np.unique(arr[opaque][:, :3].reshape(-1, 3), axis=0).shape[0])


def frame_opaque_px(im: Image.Image) -> int:
    return int((np.array(im)[:, :, 3] > 0).sum())


def anim_diff(frames: list[Image.Image]) -> float:
    """相邻帧不一致率均值（§10.3 第 6 条参考指标；>0.9 才告警，肢体动作本身即高差异）。"""
    ratios: list[float] = []
    for fa, fb in zip(frames, frames[1:]):
        a, b = np.array(fa), np.array(fb)
        ua, ub = a[:, :, 3] > 0, b[:, :, 3] > 0
        union = ua | ub
        if not union.any():
            continue
        mism = (np.abs(a[:, :, :3].astype(np.int16) - b[:, :, :3].astype(np.int16)).sum(axis=2) > 30) | (ua != ub)
        ratios.append(float((mism & union).sum()) / float(union.sum()))
    return float(np.mean(ratios)) if ratios else 0.0


# ---------------------------------------------------------------- 打包

def pack_sheet(cat: str, frames: list[tuple[str, Image.Image]]) -> tuple[Image.Image, dict]:
    cw, ch = CELL[cat]
    n = len(frames)
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = math.ceil(n / cols)
    sheet = Image.new("RGBA", (cols * cw, rows * ch), (0, 0, 0, 0))
    assert cols * cw <= 1024 and rows * ch <= 1024, f"{cat} sheet 超 §10.8 的 1024² 上限"
    frames_json = []
    for i, (name, im) in enumerate(frames):
        assert im.width <= cw and im.height <= ch, f"{name} 超格 {cw}×{ch}"
        x = (i % cols) * cw + (cw - im.width) // 2
        y = (i // cols) * ch + (ch - im.height) // 2
        sheet.paste(im, (x, y), im)
        frames_json.append({
            "filename": f"{name}.png",
            "frame": {"x": x, "y": y, "w": im.width, "h": im.height},
            "rotated": False, "trimmed": False,
            "spriteSourceSize": {"x": 0, "y": 0, "w": im.width, "h": im.height},
            "sourceSize": {"w": im.width, "h": im.height},
            "duration": 100,
        })
    tags = []
    groups: dict[str, list[int]] = {}
    for i, (name, _) in enumerate(frames):
        groups.setdefault(re.sub(r"_\d+$", "", name), []).append(i)
    for key, idxs in groups.items():
        if len(idxs) > 1:  # 单帧组（静态物件/变体）不打动画 tag
            tags.append({"name": key, "from": min(idxs), "to": max(idxs), "direction": "forward"})
    doc = {
        "frames": frames_json,
        "meta": {
            "app": "hiveweave-pixel-pipeline",
            "image": f"{SHEET_NAME[cat]}.png",
            "format": "RGBA8888",
            "size": {"w": sheet.width, "h": sheet.height},
            "scale": "1",
            "frameTags": tags,
        },
    }
    return sheet, doc


# ---------------------------------------------------------------- debug 叠加

def debug_overlay(name: str, img: np.ndarray, cfg: ImgCfg) -> None:
    im = Image.fromarray(img).convert("RGB")
    scale = 1200 / im.width
    im = im.resize((1200, int(im.height * scale)), Image.LANCZOS)
    dr = ImageDraw.Draw(im)
    h, w = img.shape[:2]
    for i, cell in enumerate(cfg.cells):
        x0f, y0f, x1f, y1f = cell.rect
        r = [x0f * w * scale, y0f * h * scale, x1f * w * scale, y1f * h * scale]
        dr.rectangle(r, outline=(255, 0, 0), width=2)
        dr.text((r[0] + 4, r[1] + 4), f"{i}:{cell.slot.name}", fill=(255, 0, 0))
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    im.save(DEBUG_DIR / f"{Path(name).stem}_debug.png")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    detect_only = "--detect" in sys.argv
    produced: dict[str, tuple[Slot, Image.Image]] = {}
    warnings: list[str] = []

    for cfg in IMAGES:
        path = SRC / cfg.file
        img = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
        debug_overlay(cfg.file, img, cfg)
        if detect_only:
            print(f"{cfg.file}: cells={len(cfg.cells)}")
            continue
        for cell in cfg.cells:
            frame = extract_cell(img, cell)
            if frame_opaque_px(frame) < MIN_OPAQUE_WARN:
                warnings.append(f"{cfg.file}: {cell.slot.name} 疑似空帧（rect 标定错误？）")
            produced[cell.slot.name] = (cell.slot, frame)

    if detect_only:
        print(f"debug overlays -> {DEBUG_DIR}")
        return 0

    # 派生：walk_left = 镜像 walk_right（§10.4 省 1 向）
    for i in range(4):
        src_name = f"worker_walk_right_{i}"
        if src_name in produced:
            slot, im = produced[src_name]
            produced[f"worker_walk_left_{i}"] = (slot, ImageOps.mirror(im))
        else:
            warnings.append(f"缺 {src_name}，无法镜像 walk_left_{i}")

    # 派生：地板变体
    tile_slot = Slot("floor_x", (16, 16), TILE, opaque=True)
    for name, src_name, hex_color in DERIVED_FLOOR:
        if src_name in produced:
            produced[name] = (tile_slot, recolor(produced[src_name][1], hex_color))
        else:
            warnings.append(f"缺 {src_name}，无法衍生 {name}")

    # 派生：合成窗框九宫格 + 彩带（AI 角件/散点不可靠，程序绘制）
    produced["window_frame_nine"] = (Slot("window_frame_nine", (24, 24), FX), synth_nine_grid())
    for name, im in synth_confetti():
        produced[name] = (Slot(name, (4, 4), FX), im)

    # 调色修正：glow_1 源图是淡绿（fade 帧），色板无浅绿被量化成灰 → 主色重映射 #A8E72E
    if "glow_1" in produced:
        produced["glow_1"] = (produced["glow_1"][0], recolor(produced["glow_1"][1], "#A8E72E"))

    # 单帧落盘（供 Aseprite 人工修关键帧）；先清空防旧帧残留
    shutil.rmtree(OUT_FRAMES, ignore_errors=True)
    for name, (slot, im) in sorted(produced.items()):
        d = OUT_FRAMES / slot.cat
        d.mkdir(parents=True, exist_ok=True)
        im.save(d / f"{name}.png")

    # 分集打包；排序保证动画帧连续：先按动画组名，再按帧号
    def sort_key(item: tuple[str, tuple[Slot, Image.Image]]) -> tuple[str, int]:
        name = item[0]
        m = re.match(r"^(.*)_(\d+)$", name)
        return (m.group(1), int(m.group(2))) if m else (name, -1)

    by_cat: dict[str, list[tuple[str, Image.Image]]] = {ROLE: [], OBJ: [], TILE: [], FX: []}
    for name, (slot, im) in sorted(produced.items(), key=sort_key):
        by_cat[slot.cat].append((name, im))

    OUT_SHEETS.mkdir(parents=True, exist_ok=True)
    report: list[str] = ["# 管线校验报告", ""]
    for cat, frames in by_cat.items():
        if not frames:
            continue
        sheet, doc = pack_sheet(cat, frames)
        sheet.save(OUT_SHEETS / f"{SHEET_NAME[cat]}.png")
        (OUT_SHEETS / f"{SHEET_NAME[cat]}.json").write_text(
            json.dumps(doc, indent=1), encoding="utf-8")
        used = max(frame_colors(im) for _, im in frames)
        report.append(
            f"## {SHEET_NAME[cat]}：{len(frames)} 帧，{sheet.width}×{sheet.height}，"
            f"实际最大色数 {used}（上限 {MAX_COLORS[cat]}）")
        groups: dict[str, list[Image.Image]] = {}
        for n, im in frames:
            groups.setdefault(re.sub(r"_\d+$", "", n), []).append(im)
        for key, gfs in groups.items():
            if len(gfs) > 1:
                diff = anim_diff(gfs)
                flag = " ⚠ 疑似标定错位" if diff > 0.9 else ""  # 肢体动画 0.5~0.9 属正常
                report.append(f"- {key}: {len(gfs)} 帧，帧间差异 {diff:.2f}{flag}")
        report.append("")

    if warnings:
        report.append("## 警告")
        report.extend(f"- {w}" for w in warnings)
    (OUT_SHEETS / "pipeline_report.md").write_text("\n".join(report), encoding="utf-8")
    print(f"done: {len(produced)} frames -> {OUT_SHEETS}")
    for w in warnings:
        print(f"WARN {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
