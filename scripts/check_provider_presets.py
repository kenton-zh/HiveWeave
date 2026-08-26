"""Smoke assertions for provider presets + anthropic build_url path handling.

Run: uv run python scripts/check_provider_presets.py
"""

from hiveweave.llm.provider import FORMAT_HANDLERS
from hiveweave.llm.provider_presets import PRESETS, get_preset

failures: list[str] = []

anthropic = FORMAT_HANDLERS["anthropic"]
chat = FORMAT_HANDLERS["openai-compatible"]

# 1. anthropic handler must not double-append paths for path-prefixed base_urls
cases = {
    "https://api.minimax.io/anthropic": "https://api.minimax.io/anthropic/v1/messages",
    "https://api.minimaxi.com/anthropic": "https://api.minimaxi.com/anthropic/v1/messages",
    "https://api.kimi.com/coding": "https://api.kimi.com/coding/v1/messages",
}
for base, expected in cases.items():
    got = anthropic.build_url(base, "MiniMax-M3")
    if got != expected:
        failures.append(f"anthropic build_url({base}) = {got}, want {expected}")

# 2. chat handler appends /chat/completions onto preset roots
got = chat.build_url("https://api.deepseek.com", "deepseek-v4-flash")
if got != "https://api.deepseek.com/chat/completions":
    failures.append(f"chat build_url(deepseek) = {got}")

# 3. every preset model carries the fields the form needs
for p in PRESETS:
    if p["api_format"] not in ("openai-compatible", "anthropic"):
        failures.append(f"{p['id']}: bad api_format {p['api_format']}")
    if not p["base_url"].startswith("https://"):
        failures.append(f"{p['id']}: base_url not https")
    for m in p["models"]:
        for key in ("id", "name", "context_window", "max_output_tokens", "reasoning", "vision", "thinking_format"):
            if key not in m:
                failures.append(f"{p['id']}/{m.get('id')}: missing {key}")
        # service 层物理不变量：max_output_tokens 必须严格小于 context_window
        if m["max_output_tokens"] >= m["context_window"]:
            failures.append(
                f"{p['id']}/{m['id']}: max_output {m['max_output_tokens']} >= ctx {m['context_window']} (invariant)"
            )

# 4. get_preset round-trip
if get_preset("deepseek") is None or get_preset("nope") is not None:
    failures.append("get_preset round-trip broken")

# 5. every anthropic preset model builds a sane URL
for p in PRESETS:
    if p["api_format"] != "anthropic":
        continue
    for m in p["models"]:
        url = anthropic.build_url(p["base_url"], m["id"])
        if url.count("/v1/") != 1 or not url.endswith("/messages"):
            failures.append(f"{p['id']}/{m['id']}: weird url {url}")

if failures:
    print("FAIL")
    for f in failures:
        print(" -", f)
    raise SystemExit(1)
print(f"OK: {len(PRESETS)} presets, {sum(len(p['models']) for p in PRESETS)} models")
