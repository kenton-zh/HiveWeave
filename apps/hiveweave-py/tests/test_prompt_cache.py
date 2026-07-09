"""Prompt cache 单元测试 — 验证 Anthropic prompt caching 断点注入。

参考 opencode cache-policy.test.ts 的测试策略：
- auto 策略在 3 个位置注入 cache_control: {type: "ephemeral"}
- OpenAI/Gemini 是 no-op（隐式缓存协议）
- 断点不超过 Anthropic 4 个上限

被测文件: src/hiveweave/llm/provider.py
"""
import pytest
import json
from hiveweave.llm.provider import (
    ApiFormat,
    AnthropicHandler,
    OpenAIHandler,
    ProviderConfig,
    ProviderFactory,
)


def _cache_count(body: dict) -> int:
    """统计 body 中 cache_control 出现次数（json.dumps 用双引号）。"""
    return json.dumps(body).count('"cache_control"')


# ── 测试数据 ────────────────────────────────────────────────

SAMPLE_MESSAGES = [
    {"role": "system", "content": "你是 HiveWeave CEO。"},
    {"role": "user", "content": "第一条用户消息"},
    {"role": "assistant", "content": "助手回复"},
    {"role": "user", "content": "最新用户消息"},
]

SAMPLE_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取文件",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "写入文件",
        "parameters": {"type": "object", "properties": {}},
    }},
]


# ── AnthropicHandler.build_body 断点注入 ────────────────────


class TestAnthropicCacheBreakpoints:
    """测试 Anthropic prompt cache 断点注入。"""

    def test_cache_disabled_no_breakpoints(self):
        """supports_prompt_cache=False 时不应有 cache_control。"""
        handler = AnthropicHandler()
        body = handler.build_body(
            messages=SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-5",
            tools=SAMPLE_TOOLS,
            supports_prompt_cache=False,
        )
        # 整个 body 不应包含 cache_control
        assert _cache_count(body) == 0

    def test_cache_enabled_marks_last_system_block(self):
        """auto 策略标记最后一个 system block。"""
        handler = AnthropicHandler()
        body = handler.build_body(
            messages=SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-5",
            supports_prompt_cache=True,
        )
        system = body["system"]
        assert system[-1]["cache_control"] == {"type": "ephemeral"}
        # 前面的 system block 不应被标记（本例只有一个）

    def test_cache_enabled_marks_latest_user_message(self):
        """auto 策略标记最后一条 user 消息的最后一个 text block。"""
        handler = AnthropicHandler()
        body = handler.build_body(
            messages=SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-5",
            supports_prompt_cache=True,
        )
        messages = body["messages"]
        # 最后一条 user 消息是 messages[-1]
        last_user_msg = None
        for msg in reversed(messages):
            if msg["role"] == "user":
                last_user_msg = msg
                break
        assert last_user_msg is not None
        # 最后一个 block 应有 cache_control
        last_block = last_user_msg["content"][-1]
        assert last_block["cache_control"] == {"type": "ephemeral"}

    def test_cache_enabled_marks_last_tool(self):
        """auto 策略标记最后一个 tool 定义。"""
        handler = AnthropicHandler()
        body = handler.build_body(
            messages=SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-5",
            tools=SAMPLE_TOOLS,
            supports_prompt_cache=True,
        )
        tools = body["tools"]
        assert tools[-1]["cache_control"] == {"type": "ephemeral"}
        # 前面的 tool 不应被标记
        if len(tools) > 1:
            assert "cache_control" not in tools[0]

    def test_cache_marks_exactly_3_breakpoints(self):
        """auto 策略注入恰好 3 个断点（system + user + tool），不超过 4 上限。"""
        handler = AnthropicHandler()
        body = handler.build_body(
            messages=SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-5",
            tools=SAMPLE_TOOLS,
            supports_prompt_cache=True,
        )
        count = _cache_count(body)
        assert count == 3, f"期望 3 个断点，实际 {count}"

    def test_cache_no_system_still_marks_user_and_tools(self):
        """无 system 消息时，仍标记 user 和 tools（2 个断点）。"""
        handler = AnthropicHandler()
        body = handler.build_body(
            messages=[{"role": "user", "content": "hello"}],
            model_id="claude-sonnet-4-5",
            tools=SAMPLE_TOOLS,
            supports_prompt_cache=True,
        )
        count = _cache_count(body)
        assert count == 2, f"期望 2 个断点，实际 {count}"

    def test_cache_no_tools_still_marks_system_and_user(self):
        """无 tools 时，仍标记 system 和 user（2 个断点）。"""
        handler = AnthropicHandler()
        body = handler.build_body(
            messages=SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-5",
            supports_prompt_cache=True,
        )
        count = _cache_count(body)
        assert count == 2, f"期望 2 个断点，实际 {count}"


# ── OpenAIHandler.build_body no-op ──────────────────────────


class TestOpenAICacheNoOp:
    """OpenAI 用隐式缓存，supports_prompt_cache 应为 no-op。"""

    def test_openai_no_cache_control_markers(self):
        """OpenAI 格式不应有 cache_control markers。"""
        handler = OpenAIHandler()
        body = handler.build_body(
            messages=SAMPLE_MESSAGES,
            model_id="gpt-4o",
            tools=SAMPLE_TOOLS,
            supports_prompt_cache=True,  # 即使开启也不应注入
        )
        assert _cache_count(body) == 0


# ── ProviderConfig 传递 supports_prompt_cache ───────────────


class TestProviderConfigCacheFlag:
    """测试 ProviderConfig 正确传递 supports_prompt_cache。"""

    def test_anthropic_config_enables_cache_by_default(self):
        """Anthropic 格式默认开启 prompt cache。"""
        factory = ProviderFactory()
        config = factory.create({
            "base_url": "https://api.anthropic.com",
            "api_key": "test-key",
            "model_id": "claude-sonnet-4-5",
            "provider_type": "anthropic",
        })
        assert config.supports_prompt_cache is True

    def test_openai_config_disables_cache(self):
        """OpenAI 格式不支持 prompt cache（用隐式缓存）。"""
        factory = ProviderFactory()
        config = factory.create({
            "base_url": "https://api.openai.com/v1",
            "api_key": "test-key",
            "model_id": "gpt-4o",
            "provider_type": "openai",
        })
        assert config.supports_prompt_cache is False

    def test_longcat_anthropic_format_enables_cache(self):
        """LongCat 用 Anthropic 格式，应开启 prompt cache。"""
        factory = ProviderFactory()
        config = factory.create({
            "base_url": "https://api.longcat.chat/anthropic",
            "api_key": "ak_test",
            "model_id": "longcat-2.0",
        })
        assert config.api_format == ApiFormat.ANTHROPIC
        assert config.supports_prompt_cache is True

    def test_explicit_disable_overrides_default(self):
        """supports_prompt_cache=False 显式关闭。"""
        factory = ProviderFactory()
        config = factory.create({
            "base_url": "https://api.anthropic.com",
            "api_key": "test-key",
            "model_id": "claude-sonnet-4-5",
            "provider_type": "anthropic",
            "supports_prompt_cache": False,
        })
        assert config.supports_prompt_cache is False

    def test_config_build_body_passes_cache_flag(self):
        """ProviderConfig.build_body 正确传递 supports_prompt_cache 到 handler。"""
        factory = ProviderFactory()
        config = factory.create({
            "base_url": "https://api.anthropic.com",
            "api_key": "test-key",
            "model_id": "claude-sonnet-4-5",
            "provider_type": "anthropic",
        })
        body = config.build_body(messages=SAMPLE_MESSAGES, tools=SAMPLE_TOOLS)
        # 应有 cache_control 断点
        assert "cache_control" in str(body)


# ── extract_usage cache 字段提取 ────────────────────────────


class TestExtractUsageCache:
    """测试 AnthropicHandler.extract_usage 提取 cache 字段。"""

    def test_extract_usage_with_cache_read(self):
        """命中缓存时 cache_read > 0。"""
        chunk = {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 2000,
            }
        }
        usage = AnthropicHandler.extract_usage(chunk)
        assert usage["cache_read"] == 2000
        assert usage["cache_creation"] == 0
        assert usage["input"] == 100
        assert usage["output"] == 50

    def test_extract_usage_with_cache_creation(self):
        """写入缓存时 cache_creation > 0。"""
        chunk = {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 1500,
                "cache_read_input_tokens": 0,
            }
        }
        usage = AnthropicHandler.extract_usage(chunk)
        assert usage["cache_creation"] == 1500
        assert usage["cache_read"] == 0

    def test_extract_usage_no_cache_fields(self):
        """无 cache 字段时返回 0（不报错）。"""
        chunk = {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
            }
        }
        usage = AnthropicHandler.extract_usage(chunk)
        assert usage["cache_read"] == 0
        assert usage["cache_creation"] == 0

    def test_extract_usage_from_message_level(self):
        """Anthropic message_start 中的 usage 也能提取。"""
        chunk = {
            "message": {
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 800,
                    "cache_read_input_tokens": 0,
                }
            }
        }
        usage = AnthropicHandler.extract_usage(chunk)
        assert usage["cache_creation"] == 800
        assert usage["input"] == 200
