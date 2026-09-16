"""提交 Qwen-Image-Edit 工作流到本地 ComfyUI，轮询结果并落盘。

用法：
  python run_edit.py --prompt "..." [--image frame50.png] [--image2 ref.png]
                     [--workflow workflow_qwen_edit.json]
                     [--steps 4] [--cfg 1.0] [--seed 42] [--out out.png] [--timeout 900]

--image2 存在时自动写入双图工作流的节点 15（LoadImage）。
依赖：仅标准库（urllib）。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "http://127.0.0.1:8188"
HERE = os.path.dirname(os.path.abspath(__file__))


def post_json(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(HOST + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(path):
    with urllib.request.urlopen(HOST + path, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--image", default="frame50.png")
    ap.add_argument("--image2", default=None, help="第二张参考图（换人/风格参考）")
    ap.add_argument("--workflow", default="workflow_qwen_edit.json")
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--cfg", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="out.png")
    ap.add_argument("--prefix", default=None)
    ap.add_argument("--timeout", type=int, default=1200)
    args = ap.parse_args()

    wfpath = args.workflow if os.path.isabs(args.workflow) else os.path.join(HERE, args.workflow)
    wf = json.load(open(wfpath, encoding="utf-8"))
    wf["7"]["inputs"]["image"] = args.image
    if args.image2:
        if "15" in wf:
            wf["15"]["inputs"]["image"] = args.image2
        else:
            print("[WARN] 当前工作流没有节点 15，--image2 被忽略")
    wf["9"]["inputs"]["prompt"] = args.prompt
    wf["12"]["inputs"]["steps"] = args.steps
    wf["12"]["inputs"]["cfg"] = args.cfg
    wf["12"]["inputs"]["seed"] = args.seed
    if args.prefix:
        wf["14"]["inputs"]["filename_prefix"] = args.prefix

    print(f"[submit] wf={os.path.basename(wfpath)} steps={args.steps} cfg={args.cfg} seed={args.seed}")
    print(f"         image={args.image}" + (f"  image2={args.image2}" if args.image2 else ""))
    try:
        res = post_json("/prompt", {"prompt": wf, "client_id": "hiveweave-qwen-edit"})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"[ERROR] 校验失败 HTTP {e.code}")
        try:
            err = json.loads(body)
            for nid, info in (err.get("node_errors") or {}).items():
                print(f"   节点 {nid}: {json.dumps(info, ensure_ascii=False)[:500]}")
            if not err.get("node_errors"):
                print(json.dumps(err, ensure_ascii=False)[:1500])
        except Exception:
            print(body[:1500])
        return 1

    pid = res.get("prompt_id")
    print(f"[prompt_id] {pid}  number={res.get('number')}")

    t0 = time.time()
    notified = False
    while time.time() - t0 < args.timeout:
        time.sleep(3)
        hist = get_json(f"/history/{pid}")
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error" or not status.get("completed", True):
                print("[ERROR] 执行失败：")
                for m in status.get("messages", [])[-6:]:
                    print("   ", json.dumps(m, ensure_ascii=False)[:600])
                return 1
            outs = entry.get("outputs", {})
            imgs = []
            for node_out in outs.values():
                imgs.extend(node_out.get("images", []))
            if not imgs:
                print("[WARN] 完成但没有图片输出")
                return 1
            for i, im in enumerate(imgs):
                q = urllib.parse.urlencode(
                    {"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": im.get("type", "output")}
                )
                if len(imgs) == 1:
                    dst = args.out if os.path.isabs(args.out) else os.path.join(HERE, args.out)
                else:
                    base, ext = os.path.splitext(args.out)
                    dst = os.path.join(HERE, f"{base}_{i}{ext}")
                with urllib.request.urlopen(f"{HOST}/view?{q}", timeout=180) as r, open(dst, "wb") as f:
                    f.write(r.read())
                print(f"[saved] {dst}  ({os.path.getsize(dst)} bytes)")
            print(f"[done] 耗时 {time.time() - t0:.1f}s")
            return 0
        if time.time() - t0 > 30 and not notified:
            q = get_json("/queue")
            print(f"[queue] running={len(q.get('queue_running', []))} pending={len(q.get('queue_pending', []))}")
            notified = True
    print("[TIMEOUT]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
