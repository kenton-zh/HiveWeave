"""诊断 ARK 网关 400「Model only support text input」的真实触发点。

1) 用平台 provider 代码构建带图工具消息的请求体，验证剥图是否生效（离线）。
2) 对 A013 实际使用的端点发 4 个最小探测（每个 <50 token）：
   a. 纯字符串 content（基线，应 200）
   b. content 为 text 数组（无图）
   c. 带 image_url 部分（预期复现 400）
   d. 平台真实形状：stream + tools + reasoning_effort + stream_options
"""
import asyncio
import json
import sqlite3
import sys

sys.path.insert(0, r"D:\PC_AI\Project\HiveWeave\apps\hiveweave-py\src")

META_DB = r"D:\PC_AI\Project\HiveWeave\apps\hiveweave-py\data\hiveweave.db"
MODEL_ROW_ID = "paid-1782888378452"  # A013 潮汐实际使用：MiniMax-M3 (ARK Coding)


def load_model():
    db = sqlite3.connect(META_DB, timeout=5)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM llm_models WHERE id = ?", (MODEL_ROW_ID,)).fetchone()
    db.close()
    return dict(row)


async def main():
    import httpx
    from hiveweave.llm.provider import provider_factory

    model = load_model()
    print(f"model: {model['name']} | {model['model_id']} | {model['base_url']}")
    print(f"supports_images={model['supports_images']} supports_thinking={model['supports_thinking']}")

    cfg = provider_factory.create(model)
    print(f"provider: format={cfg.api_format.value} supports_images={cfg.supports_images} "
          f"max_out={cfg.max_output_tokens} ctx={cfg.context_window}")

    # ── 离线验证：带图工具消息经平台 build_body 后是否残留 image_url ──
    fake_img = {"media_type": "image/png", "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="}
    msgs_with_img = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do qa"},
        {"role": "assistant", "content": "calling browse", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "browse", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "screenshot saved to x.png", "images": [fake_img]},
    ]
    body = cfg.build_body(messages=msgs_with_img, stream=True, tools=[{"type": "function", "function": {"name": "browse", "description": "x", "parameters": {"type": "object", "properties": {}}}}])
    body_str = json.dumps(body)
    print(f"\n[offline] body has image_url: {'image_url' in body_str} | has images key: {'\"images\"' in body_str}")
    print(f"[offline] message roles: {[m.get('role') for m in body['messages']]}")
    for m in body["messages"]:
        c = m.get("content")
        print(f"  - {m.get('role')}: content_type={type(c).__name__} preview={str(c)[:90]}")

    # ── 在线最小探测 ──
    url = cfg.build_url()
    headers = cfg.build_headers()
    headers["Accept"] = "application/json"

    async def probe(tag, messages, **extra):
        b = {"model": cfg.model_name, "messages": messages, "stream": False, "max_tokens": 16}
        b.update(extra)
        try:
            async with httpx.AsyncClient(timeout=30.0) as cli:
                r = await cli.post(url, json=b, headers=headers)
            txt = r.text[:220].replace("\n", " ")
            print(f"[probe {tag}] HTTP {r.status_code} | {txt}")
        except Exception as e:
            print(f"[probe {tag}] EXC {e}")

    await probe("a-string", [{"role": "user", "content": "say hi"}])
    await probe("b-text-array", [{"role": "user", "content": [{"type": "text", "text": "say hi"}]}])
    await probe("c-image", [{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{fake_img['data']}"}},
    ]}])
    await probe("d-platform-shape", [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "say hi"},
    ], stream=True, stream_options={"include_usage": True},
        reasoning_effort="high",
        tools=[{"type": "function", "function": {"name": "bash", "description": "run", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}])

    # e. 完整平台 build_body（含 tool_calls 历史 + 工具结果 + 哨兵）真实发出
    full_body = cfg.build_body(messages=[
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "user", "content": "(continue)"},
    ], stream=False, tools=[{"type": "function", "function": {"name": "bash", "description": "run", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}])
    try:
        async with httpx.AsyncClient(timeout=30.0) as cli:
            r = await cli.post(url, json=full_body, headers=headers)
        print(f"[probe e-full-platform-body] HTTP {r.status_code} | {r.text[:220]}")
    except Exception as e:
        print(f"[probe e] EXC {e}")


asyncio.run(main())
