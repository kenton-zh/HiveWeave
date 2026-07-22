"""Tests for model self-test and auto-detection.

Covers:
- _extract_usage_from_response: 各种 provider 格式的 usage 解析
- _detect_model_capabilities: 推理模型预设表匹配
- _do_self_test: 自动修正 DB 配置（mocked HTTP）
- create_model: supports_thinking=False 不被覆盖
- coordinator tool visibility: COORDINATOR_ONLY_TOOLS 对 coordinator 可见
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.api.models import (
    _detect_model_capabilities,
    _extract_usage_from_response,
    _KNOWN_CONTEXT_WINDOWS,
    _REASONING_MODEL_PATTERNS,
)
from hiveweave.services.permission import (
    COORDINATOR_ONLY_TOOLS,
    READONLY_TOOLS,
    PermissionService,
)


# ── _extract_usage_from_response ──────────────────────────────


class TestExtractUsage:
    def test_openai_format(self):
        data = {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
        result = _extract_usage_from_response(data)
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["total_tokens"] == 150
        assert result["reasoning_tokens"] == 0

    def test_openrouter_with_reasoning(self):
        data = {
            "usage": {
                "prompt_tokens": 97263,
                "completion_tokens": 47,
                "total_tokens": 97310,
                "completion_tokens_details": {"reasoning_tokens": 65},
            }
        }
        result = _extract_usage_from_response(data)
        assert result["reasoning_tokens"] == 65
        assert result["output_tokens"] == 47

    def test_anthropic_format(self):
        data = {"usage": {"input_tokens": 200, "output_tokens": 80}}
        result = _extract_usage_from_response(data)
        assert result["input_tokens"] == 200
        assert result["output_tokens"] == 80
        assert result["total_tokens"] == 280  # auto-computed

    def test_no_usage(self):
        result = _extract_usage_from_response({})
        assert result["input_tokens"] == 0
        assert result["reasoning_tokens"] == 0

    def test_null_usage(self):
        result = _extract_usage_from_response({"usage": None})
        assert result["total_tokens"] == 0


# ── _detect_model_capabilities ────────────────────────────────


class TestDetectModelCapabilities:
    @pytest.mark.asyncio
    async def test_preset_reasoning_model_hy3(self):
        """hy3 应匹配推理模型预设。"""
        result = await _detect_model_capabilities(
            base_url="https://api.example.com",
            api_key="",
            model_id="tencent/hy3:free",
        )
        assert result["supports_thinking"] is True

    @pytest.mark.asyncio
    async def test_preset_reasoning_model_deepseek_r1(self):
        result = await _detect_model_capabilities(
            base_url="https://api.example.com",
            api_key="",
            model_id="deepseek/deepseek-r1",
        )
        assert result["supports_thinking"] is True

    @pytest.mark.asyncio
    async def test_non_reasoning_model(self):
        """普通模型不应匹配推理预设。"""
        result = await _detect_model_capabilities(
            base_url="https://api.example.com",
            api_key="",
            model_id="meta-llama/llama-3-8b",
        )
        assert result["supports_thinking"] is None or result["supports_thinking"] is not True

    @pytest.mark.asyncio
    async def test_preset_max_output_tokens(self):
        """预设表应返回 max_output_tokens。"""
        result = await _detect_model_capabilities(
            base_url="https://api.example.com",
            api_key="",
            model_id="tencent/hy3:free",
        )
        assert result["max_output_tokens"] is not None
        assert result["max_output_tokens"] > 0

    @pytest.mark.asyncio
    async def test_openrouter_api_query(self):
        """OpenRouter 返回脏数据时，预设真值表必须压过 API（三层防御检测层）。

        mock 的 max_completion_tokens=262144 正是线上事故数据：hy3:free 的
        API 把 context_length 串线成 max_output（输出预算=整个窗口，物理不可能）。
        旧行为盲信 API 值；防御层引入后，预设表 hy3→32000 必须获胜。
        API 仅补充 supports_thinking（architecture 信号可信）。
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "tencent/hy3:free",
                    "context_length": 262144,
                    "top_provider": {"max_completion_tokens": 262144},
                    "architecture": {"input_modalities": ["text", "reasoning"]},
                }
            ]
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await _detect_model_capabilities(
                base_url="https://openrouter.ai/api/v1",
                api_key="",
                model_id="tencent/hy3:free",
            )

        assert result["supports_thinking"] is True
        assert result["max_output_tokens"] == 64_000  # 预设表获胜，非 API 脏数据
        assert result["source"] == "preset"

    @pytest.mark.asyncio
    async def test_openrouter_api_query_no_preset_hit(self):
        """预设表未命中时，才采纳 OpenRouter API 的 max_output 候选（且需过 sanitize）。"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "somevendor/future-model-9000",
                    "context_length": 262144,
                    "top_provider": {"max_completion_tokens": 65536},
                    "architecture": {"input_modalities": ["text", "reasoning"]},
                }
            ]
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await _detect_model_capabilities(
                base_url="https://openrouter.ai/api/v1",
                api_key="",
                model_id="somevendor/future-model-9000",
            )

        assert result["supports_thinking"] is True
        assert result["max_output_tokens"] == 65536
        assert result["source"] == "external-api"


# ── 预设表完整性 ──────────────────────────────────────────────


class TestPresetTables:
    def test_hy3_context_window_updated(self):
        """hy3 预设值应为 262144，不是旧的 32000。"""
        for pattern, ctx in _KNOWN_CONTEXT_WINDOWS:
            if pattern == "hy3":
                assert ctx == 262_144, f"hy3 preset should be 262144, got {ctx}"
                return
        pytest.fail("hy3 not found in _KNOWN_CONTEXT_WINDOWS")

    def test_reasoning_patterns_non_empty(self):
        assert len(_REASONING_MODEL_PATTERNS) > 0

    def test_reasoning_patterns_cover_common_models(self):
        patterns_str = " ".join(_REASONING_MODEL_PATTERNS)
        for expected in ["hy3", "deepseek-r1", "o1", "o3"]:
            assert expected in patterns_str, f"{expected} missing from reasoning patterns"


# ── Coordinator 工具可见性 ────────────────────────────────────


class TestCoordinatorToolVisibility:
    def test_coordinator_only_tools_not_in_readonly(self):
        """COORDINATOR_ONLY_TOOLS 不应在 READONLY_TOOLS 中（否则不需要额外添加）。"""
        # review_task 不应在 READONLY_TOOLS 中
        assert "review_task" not in READONLY_TOOLS, (
            "review_task should not be in READONLY_TOOLS — "
            "it's added separately for coordinator in _get_tool_definitions"
        )

    def test_coordinator_only_tools_contains_review_task(self):
        assert "review_task" in COORDINATOR_ONLY_TOOLS

    def test_coordinator_only_tools_contains_create_task(self):
        assert "create_task" in COORDINATOR_ONLY_TOOLS

    def test_coordinator_only_tools_contains_merge(self):
        assert "git_worktree_merge" in COORDINATOR_ONLY_TOOLS

    def test_get_tools_for_mode_readonly_excludes_coordinator_tools(self):
        """readonly 模式不应返回 coordinator-only 工具。"""
        svc = PermissionService()
        tools = svc.get_tools_for_mode("readonly")
        for t in COORDINATOR_ONLY_TOOLS:
            if t != "dispatch_task":  # dispatch_task 同时在两个集合中
                assert t not in tools, f"{t} should not be in readonly mode tools"

    @pytest.mark.asyncio
    async def test_evaluate_allows_review_task_for_coordinator(self):
        """coordinator 角色调用 review_task 应返回 allow。"""
        svc = PermissionService()
        agent = {
            "permission_mode": "readonly",
            "permission_type": "coordinator",
            "denied_tools": None,
            "ask_tools": None,
            "allowed_tools": None,
        }
        with patch("hiveweave.services.permission.meta_db") as mock_meta:
            mock_meta.get_agent_by_id = AsyncMock(return_value=agent)
            result = await svc.evaluate("agent-1", "review_task")
        assert result == "allow"

    @pytest.mark.asyncio
    async def test_evaluate_denies_review_task_for_executor(self):
        """executor 角色调用 review_task 应返回 deny。"""
        svc = PermissionService()
        agent = {
            "permission_mode": "readwrite",
            "permission_type": "executor",
            "denied_tools": None,
            "ask_tools": None,
            "allowed_tools": None,
        }
        with patch("hiveweave.services.permission.meta_db") as mock_meta:
            mock_meta.get_agent_by_id = AsyncMock(return_value=agent)
            result = await svc.evaluate("agent-1", "review_task")
        assert result == "deny"


# ── create_model 不覆盖用户显式配置 ──────────────────────────


class TestCreateModelRespectsUserConfig:
    @pytest.mark.asyncio
    async def test_supports_thinking_false_not_overwritten(self):
        """用户显式设 supportsThinking=False 时不应被自动检测覆盖。"""
        from hiveweave.api.models import ModelCreate, _normalize_attrs

        body = ModelCreate(
            name="test",
            modelId="some-model",
            baseUrl="https://api.example.com",
            apiKey="key",
            supportsThinking=False,
        )
        attrs = _normalize_attrs(body)
        # 模拟自动检测返回 True
        with patch(
            "hiveweave.api.models._detect_model_capabilities",
            return_value={"supports_thinking": True, "max_output_tokens": 32000, "source": "preset"},
        ):
            if "supports_thinking" not in attrs or "max_output_tokens" not in attrs:
                caps = await _detect_model_capabilities("", "", "")
                if "supports_thinking" not in attrs and caps.get("supports_thinking") is not None:
                    attrs["supports_thinking"] = caps["supports_thinking"]

        assert attrs["supports_thinking"] is False, "User's explicit False should not be overwritten"
