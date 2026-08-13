#!/usr/bin/env python3
"""从修正后的 frames/role/ 重打包 role_sheet.png + .json。

walk_left 从修正后的 walk_right 重新镜像，确保完全一致。
其余 sheet（obj/tile/fx）不受影响。
"""

import json
import math
import re
from pathlib import Path

from PIL import Image, ImageOps

FRAMES_DIR = Path(r"D:\PC_AI\Project\HiveWeave\assets\mvp\frames\role")
OUT_SHEETS = Path(r"D:\PC_AI\Project\HiveWeave\assets\mvp\sheets")

CELL_W, CELL_H = 16, 24  # role cell
SHEET_NAME = "role_sheet"


def pack_sheet(frames: list[tuple[str, Image.Image]]) -> tuple[Image.Image, dict]:
    n = len(frames)
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = math.ceil(n / cols)
    sheet = Image.new("RGBA", (cols * CELL_W, rows * CELL_H), (0, 0, 0, 0))
    assert cols * CELL_W <= 1024 and rows * CELL_H <= 1024

    frames_json = []
    for i, (name, im) in enumerate(frames):
        assert im.width <= CELL_W and im.height <= CELL_H, f"{name} 超格"
        x = (i % cols) * CELL_W + (CELL_W - im.width) // 2
        y = (i // cols) * CELL_H + (CELL_H - im.height) // 2
        sheet.paste(im, (x, y), im)
        frames_json.append({
            "filename": f"{name}.png",
            "frame": {"x": x, "y": y, "w": im.width, "h": im.height},
            "rotated": False, "trimmed": False,
            "spriteSourceSize": {"x": 0, "y": 0, "w": im.width, "h": im.height},
            "sourceSize": {"w": im.width, "h": im.height},
            "duration": 100,
        })

    # 动画 tag：按组名聚合，多帧组打 forward tag
    tags = []
    groups: dict[str, list[int]] = {}
    for i, (name, _) in enumerate(frames):
        groups.setdefault(re.sub(r"_\d+$", "", name), []).append(i)
    for key, idxs in groups.items():
        if len(idxs) > 1:
            tags.append({"name": key, "from": min(idxs), "to": max(idxs), "direction": "forward"})

    doc = {
        "frames": frames_json,
        "meta": {
            "app": "hiveweave-pixel-pipeline",
            "image": f"{SHEET_NAME}.png",
            "format": "RGBA8888",
            "size": {"w": sheet.width, "h": sheet.height},
            "scale": "1",
            "frameTags": tags,
        },
    }
    return sheet, doc


def frame_colors(im: Image.Image) -> int:
    import numpy as np
    arr = np.array(im)
    opaque = arr[:, :, 3] > 0
    if not opaque.any():
        return 0
    return int(np.unique(arr[opaque][:, :3].reshape(-1, 3), axis=0).shape[0])


def anim_diff(frames: list[Image.Image]) -> float:
    import numpy as np
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


def main() -> None:
    # 加载所有 role 帧
    frames: dict[str, Image.Image] = {}
    for p in sorted(FRAMES_DIR.glob("*.png")):
        frames[p.stem] = Image.open(p).convert("RGBA")

    # 重新镜像 walk_left（从修正后的 walk_right）
    for i in range(4):
        src = f"worker_walk_right_{i}"
        dst = f"worker_walk_left_{i}"
        if src in frames:
            frames[dst] = ImageOps.mirror(frames[src])

    # 排序：先按动画组名，再按帧号
    def sort_key(name: str) -> tuple[str, int]:
        m = re.match(r"^(.*)_(\d+)$", name)
        return (m.group(1), int(m.group(2))) if m else (name, -1)

    sorted_frames = [(name, frames[name]) for name in sorted(frames, key=sort_key)]

    # 打包
    sheet, doc = pack_sheet(sorted_frames)
    OUT_SHEETS.mkdir(parents=True, exist_ok=True)
    sheet.save(OUT_SHEETS / f"{SHEET_NAME}.png")
    (OUT_SHEETS / f"{SHEET_NAME}.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"{SHEET_NAME}: {len(sorted_frames)} 帧 → {sheet.width}×{sheet.height}")

    # 校验
    max_colors = 0
    for name, im in sorted_frames:
        c = frame_colors(im)
        max_colors = max(max_colors, c)
    print(f"最大色数: {max_colors}（上限 8）")

    # 帧间差异
    groups: dict[str, list[Image.Image]] = {}
    for name, im in sorted_frames:
        groups.setdefault(re.sub(r"_\d+$", "", name), []).append(im)
    print("\n帧间差异:")
    for key, gfs in sorted(groups.items()):
        if len(gfs) > 1:
            d = anim_diff(gfs)
            warn = " ⚠" if d > 0.9 else ""
            print(f"  {key}: {len(gfs)} 帧, diff={d:.2f}{warn}")


if __name__ == "__main__":
    main()
