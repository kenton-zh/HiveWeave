"""二轮探测：找出哪个 模型x端点 组合会 400「Model only support text input」。
覆盖：MiniMax Plan / DeepSeek Coding / DeepSeek Plan 三行，各发 a/b/c/d 四探针。
"""
import asyncio
import sqlite3
import sys

sys.path.insert(0, r"D:\PC_AI\Project\HiveWeave\apps\hiveweave-py\src")

META_DB = r"D:\PC_AI\Project\HiveWeave\apps\hiveweave-py\data\hiveweave.db"
ROWS = [
    "d26886bd-96da-46f1-b8a7-553a2754778c",  # MiniMax-M3 (ARK Plan) tier=executor
    "03d2186c-7023-4d61-b96a-e7d23ff3fedd",  # DeepSeek V4 Flash (ARK Coding)
    "22a1c708-2249-4974-8a17-db90201933ee",  # DeepSeek V4 Flash (ARK Plan)
    "72b5cad6-12e2-4bd6-92ec-445fe09640f5",  # glm-5.2 CODE (CEO A011 绑定)
]

FAKE_IMG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


def load(row_id):
    db = sqlite3.connect(META_DB, timeout=5)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM llm_models WHERE id = ?", (row_id,)).fetchone()
    db.close()
    return dict(row)


async def main():
    import httpx
    from hiveweave.llm.provider import provider_factory

    for rid in ROWS:
        model = load(rid)
        cfg = provider_factory.create(model)
        url = cfg.build_url()
        headers = cfg.build_headers()
        headers["Accept"] = "application/json"
        print(f"\n=== {model['name']} | {model['model_id']} | {model['base_url']} ===")

        async def probe(tag, messages, **extra):
            b = {"model": cfg.model_name, "messages": messages, "stream": False, "max_tokens": 16}
            b.update(extra)
            try:
                async with httpx.AsyncClient(timeout=30.0) as cli:
                    r = await cli.post(url, json=b, headers=headers)
                txt = r.text[:200].replace("\n", " ")
                print(f"  [{tag}] HTTP {r.status_code} | {txt}")
            except Exception as e:
                print(f"  [{tag}] EXC {type(e).__name__} {e}")

        await probe("a-string", [{"role": "user", "content": "say hi"}])
        await probe("b-text-array", [{"role": "user", "content": [{"type": "text", "text": "say hi"}]}])
        await probe("c-image", [{"role": "user", "content": [
            {"type": "text", "text": "what?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{FAKE_IMG}"}},
        ]}])
        await probe("d-reasoning", [{"role": "user", "content": "say hi"}], reasoning_effort="high")


asyncio.run(main())
