"""三轮探测：对 executor/management 实际槽位模型重放真实请求形状，二分 400 触发物。
 executor_primary = DeepSeek V4 Flash (ARK Coding)  [03d2186c]
 management_primary = glm-5.2 CODE                  [72b5cad6]
"""
import asyncio
import json
import sqlite3
import sys

sys.path.insert(0, r"D:\PC_AI\Project\HiveWeave\apps\hiveweave-py\src")

META_DB = r"D:\PC_AI\Project\HiveWeave\apps\hiveweave-py\data\hiveweave.db"
ROWS = {
    "executor(deepseek-coding)": "03d2186c-7023-4d61-b96a-e7d23ff3fedd",
    "management(glm-coding)": "72b5cad6-12e2-4bd6-92ec-445fe09640f5",
}
FAKE_IMG = {"media_type": "image/png", "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="}


def load(row_id):
    db = sqlite3.connect(META_DB, timeout=5)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM llm_models WHERE id = ?", (row_id,)).fetchone()
    db.close()
    return dict(row)


async def main():
    import httpx
    from hiveweave.llm.provider import provider_factory

    for label, rid in ROWS.items():
        model = load(rid)
        cfg = provider_factory.create(model)
        url = cfg.build_url()
        headers = cfg.build_headers()
        headers["Accept"] = "application/json"
        print(f"\n=== {label} | {model['model_id']} ===")

        async def probe(tag, body):
            body = dict(body)
            body.setdefault("model", cfg.model_name)
            body.setdefault("stream", False)
            body.setdefault("max_tokens", 16)
            try:
                async with httpx.AsyncClient(timeout=40.0) as cli:
                    r = await cli.post(url, json=body, headers=headers)
                txt = r.text[:180].replace("\n", " ")
                print(f"  [{tag}] HTTP {r.status_code} | {txt}")
            except Exception as e:
                print(f"  [{tag}] EXC {type(e).__name__} {e}")

        u = [{"role": "user", "content": "say hi"}]
        # 1 基线
        await probe("1-baseline", {"messages": u})
        # 2 + 真实工具定义（平台 11 个内置工具的 schema 子集，结构一致）
        tools = [{"type": "function", "function": {
            "name": "bash", "description": "run shell command",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        }}, {"type": "function", "function": {
            "name": "browse", "description": "browser",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "screenshot": {"type": "boolean"}}},
        }}]
        await probe("2-tools", {"messages": u, "tools": tools})
        # 3 + assistant tool_calls(空 content) + tool result + 哨兵
        m3 = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            {"role": "user", "content": "(continue)"},
        ]
        await probe("3-toolhistory", {"messages": m3, "tools": tools})
        # 4 assistant content=null + tool_calls
        m4 = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
        await probe("4-null-content", {"messages": m4, "tools": tools})
        # 5 平台 build_body 全形状（带图工具结果，应剥图）+ reasoning_effort + stream
        plat = cfg.build_body(messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "qa task"},
            {"role": "assistant", "content": "browse now", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "browse", "arguments": "{\"url\":\"http://x\"}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "shot saved", "images": [FAKE_IMG]},
            {"role": "user", "content": "(continue)"},
        ], stream=False, tools=tools)
        leaked = "image_url" in json.dumps(plat)
        print(f"  [5-platform-body] leaked_image={leaked}")
        await probe("5-platform-body", plat)
        # 6 手动强制 supports_images=True（对照组，验证错误串一致）
        cfg2 = provider_factory.create(model)
        cfg2.supports_images = True
        plat2 = cfg2.build_body(messages=[
            {"role": "user", "content": "look"},
            {"role": "user", "content": "shot", "images": [FAKE_IMG]},
        ], stream=False)
        await probe("6-force-image(对照)", plat2)


asyncio.run(main())
