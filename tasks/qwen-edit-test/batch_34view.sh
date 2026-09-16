#!/usr/bin/env bash
# 批量：H3 原始视频抽帧 -> Qwen-Image-Edit 转等距 3/4 视角（脸可见）
# 输出：view34_<clip>_<idx>.png（白底大幅，待抠像组 sheet）
#
# 坑记录：
#  - ffmpeg 是 Windows 程序，输出路径必须 Windows 格式（用 cygpath 转换）
#  - curl 上传必须用**相对文件名**（cd 到本目录后），用 /d/... 绝对路径会上传失败，
#    进而导致 ComfyUI 报 400（LoadImage 找不到图），表现为「校验失败」
set -u

FF="/c/Users/99744/.workbuddy/binaries/python/envs/default/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"
PY="/c/Users/99744/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
DIR="/d/PC_AI/Project/HiveWeave/tasks/qwen-edit-test"
SPRITE="D:/Comfy-Desktop/ComfyUI-Shared/output/sprite"

PROMPT="Re-shoot this character from a different camera angle: an isometric 3/4 top-down view, camera 45 degrees above her, so her face and both eyes are clearly visible to the viewer. Keep the identical character and outfit: same girl, same black hair with the same hairstyle, same purple hoodie jacket, same dark blue pinstripe pants, same white sneakers. She stays in the same seated pose with knees bent, both feet flat on the ground, both arms extended forward and slightly downward in front of her body. No desk, no table, no keyboard, no mouse, no chair, no props - only the girl on a plain pure white background. Keep the same flat cel-shaded anime art style."

cd "$DIR" || exit 1

ok=0; fail=0
for spec in "purple_girl_typing_side_v3_00001_.mp4:typing" "purple_girl_sitting_idle_v3_00001_.mp4:sitting"; do
  video="${spec%%:*}"
  clip="${spec##*:}"
  for pair in "28:1.1667" "50:2.0833" "72:3.0" "94:3.9167"; do
    idx="${pair%%:*}"
    tsec="${pair##*:}"
    raw="raw_${clip}_${idx}.png"
    out="view34_${clip}_${idx}.png"

    if [ ! -f "$raw" ]; then
      winpath="$(cygpath -w "$DIR/$raw" 2>/dev/null || echo "$DIR/$raw")"
      "$FF" -y -ss "$tsec" -i "$SPRITE/$video" -frames:v 1 -update 1 "$winpath" >/dev/null 2>&1
    fi
    if [ ! -f "$raw" ]; then echo "[FAIL] 抽帧 $clip#$idx"; fail=$((fail+1)); continue; fi

    # 关键：相对文件名上传
    up=$(curl -s -m 30 -X POST -F "image=@$raw" http://127.0.0.1:8188/upload/image)
    case "$up" in
      *"\"name\""*) : ;;
      *) echo "[FAIL] 上传 $raw -> $up"; fail=$((fail+1)); continue ;;
    esac

    res=$("$PY" run_edit.py --image "$raw" --prompt "$PROMPT" --steps 4 --cfg 1.0 --seed 77 --out "$out" 2>&1)
    if echo "$res" | grep -q "\[saved\]"; then
      echo "[OK] $out  $(echo "$res" | grep -o '耗时 [0-9.]*s')"
      ok=$((ok+1))
    else
      echo "[FAIL] $clip#$idx"
      echo "$res" | head -12
      fail=$((fail+1))
    fi
  done
done
echo "=== 完成: OK=$ok FAIL=$fail ==="
ls -la view34_*.png 2>/dev/null | awk '{printf "%s  %.0f KB\n", $NF, $5/1024}'
