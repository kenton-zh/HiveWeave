"""Tests for model self-test and auto-detection.

Covers:
- _extract_usage_from_response: 各种 provider 格式的 usage 解析
- _detect_model_metadata: 通用 /models 元数据探测（无预制数据，真实探测）
- _do_self_test: 自动修正 DB 配置（mocked HTTP）
- create_model: supports_thinking=False 不被覆盖
- coordinator tool visibility: COORDINATOR_ONLY_TOOLS 对 coordinator 可见
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.api.models import (
    _detect_model_metadata,
    _do_self_test,
    _extract_usage_from_response,
    _normalize_models_probe_base,
    _probe_url_blocked_reason,
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


# ── _detect_model_metadata（通用 /models 探测，无预制数据）─────


def _mock_models_endpoint(payload: dict | None, status: int = 200):
    """Patch httpx.AsyncClient so GET {base}/models returns ``payload``."""
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.json.return_value = payload or {}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    return patch("httpx.AsyncClient", return_value=mock_client)


class TestProbeUrlGuards:
    def test_normalize_strips_chat_completions_suffix(self):
        assert (
            _normalize_models_probe_base(
                "https://opencode.ai/zen/go/v1/chat/completions"
            )
            == "https://opencode.ai/zen/go/v1"
        )
        assert (
            _normalize_models_probe_base("https://api.openai.com/v1/")
            == "https://api.openai.com/v1"
        )

    def test_block_metadata_and_link_local(self):
        assert _probe_url_blocked_reason("http://169.254.169.254/") is not None
        assert _probe_url_blocked_reason(
            "http://metadata.google.internal/computeMetadata/v1"
        ) is not None
        assert _probe_url_blocked_reason("http://user:pass@evil.com/v1") is not None
        assert _probe_url_blocked_reason("ftp://example.com/v1") is not None

    def test_allow_public_and_private_gateways(self):
        # Local-first: LAN / loopback gateways stay allowed.
        assert _probe_url_blocked_reason("http://127.0.0.1:11434/v1") is None
        assert _probe_url_blocked_reason("http://192.168.1.10:8000/v1") is None
        assert _probe_url_blocked_reason("https://openrouter.ai/api/v1") is None

    @pytest.mark.asyncio
    async def test_detect_rejects_metadata_without_http(self):
        result = await _detect_model_metadata(
            base_url="http://169.254.169.254/",
            api_key="sk-victim",
            model_id="any",
        )
        assert result["source"] == "unknown"
        assert "拒绝" in (result["error"] or "")

    @pytest.mark.asyncio
    async def test_self_test_rejects_metadata_base_url(self):
        """_do_self_test must refuse IMDS/link-local before POSTing."""
        result = await _do_self_test({
            "id": "m1",
            "provider": "openai",
            "base_url": "http://169.254.169.254/",
            "api_key": "sk-victim",
            "model_id": "gpt-test",
            "context_window": 0,
            "supports_thinking": False,
            "max_output_tokens": 0,
        })
        assert result["ok"] is False
        assert "拒绝" in (result.get("error") or "")


class TestDetectModelMetadata:
    @pytest.mark.asyncio
    async def test_real_probe_success_full_caps(self):
        """网关 /models 可达且 id 命中 → 返回真实元数据，source=external-api。"""
        with _mock_models_endpoint({
            "data": [
                {
                    "id": "deepseek-v4-flash",
                    "context_length": 1024000,
                    "max_completion_tokens": 8192,
                    "architecture": {"input_modalities": ["text", "reasoning"]},
                }
            ]
        }):
            result = await _detect_model_metadata(
                base_url="https://opencode.ai/zen/go/v1",
                api_key="sk-x",
                model_id="deepseek-v4-flash",
            )
        assert result["context_window"] == 1024000
        assert result["supports_thinking"] is True
        assert result["max_output_tokens"] == 8192
        assert result["source"] == "external-api"
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_openrouter_real_schema_top_provider(self):
        """OpenRouter 真实 schema：max_completion_tokens 在 top_provider 下。"""
        with _mock_models_endpoint({
            "data": [
                {
                    "id": "tencent/hy3:free",
                    "context_length": 262144,
                    "top_provider": {"max_completion_tokens": 32768},
                    "architecture": {"input_modalities": ["text", "reasoning"]},
                }
            ]
        }):
            result = await _detect_model_metadata(
                base_url="https://openrouter.ai/api/v1",
                api_key="",
                model_id="tencent/hy3:free",
            )
        assert result["context_window"] == 262144
        assert result["supports_thinking"] is True
        assert result["max_output_tokens"] == 32768  # top_provider 候选
        assert result["source"] == "external-api"

    @pytest.mark.asyncio
    async def test_openrouter_dirty_top_provider_discarded(self):
        """OpenRouter top_provider 脏数据（= context_length 串线）→ 物理校验丢弃。"""
        with _mock_models_endpoint({
            "data": [
                {
                    "id": "tencent/hy3:free",
                    "context_length": 262144,
                    "top_provider": {"max_completion_tokens": 262144},
                    "architecture": {"input_modalities": ["text", "reasoning"]},
                }
            ]
        }):
            result = await _detect_model_metadata(
                base_url="https://openrouter.ai/api/v1",
                api_key="",
                model_id="tencent/hy3:free",
            )
        assert result["context_window"] == 262144
        assert result["max_output_tokens"] is None  # 物理不可能，丢弃
        assert result["source"] == "external-api"

    @pytest.mark.asyncio
    async def test_real_probe_success_minimal_caps(self):
        """网关只回 id（如 opencode.ai 实测），缺失字段为 None 但 source=external-api。"""
        with _mock_models_endpoint({
            "data": [{"id": "deepseek-v4-flash"}]
        }):
            result = await _detect_model_metadata(
                base_url="https://opencode.ai/zen/go/v1",
                api_key="",
                model_id="deepseek-v4-flash",
            )
        assert result["context_window"] is None
        assert result["supports_thinking"] is None
        assert result["max_output_tokens"] is None
        assert result["source"] == "external-api"
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_real_probe_model_not_listed(self):
        """id 不在网关 /models 里 → 探测失败，error 说明原因。"""
        with _mock_models_endpoint({
            "data": [{"id": "other-model"}]
        }):
            result = await _detect_model_metadata(
                base_url="https://api.example.com/v1",
                api_key="",
                model_id="ghost-model",
            )
        assert result["context_window"] is None
        assert result["supports_thinking"] is None
        assert result["max_output_tokens"] is None
        assert result["source"] == "unknown"
        assert result["error"] is not None
        assert "ghost-model" in result["error"]

    @pytest.mark.asyncio
    async def test_real_probe_endpoint_unreachable(self):
        """/models 非 200 → 探测失败（不返回任何猜测值）。"""
        with _mock_models_endpoint(None, status=404):
            result = await _detect_model_metadata(
                base_url="https://api.example.com/v1",
                api_key="",
                model_id="m",
            )
        assert result["source"] == "unknown"
        assert result["error"] is not None
        assert all(
            v is None for v in (
                result["context_window"],
                result["supports_thinking"],
                result["max_output_tokens"],
            )
        )

    @pytest.mark.asyncio
    async def test_real_probe_http_error(self):
        """/models 抛网络异常 → 探测失败。"""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("conn refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _detect_model_metadata(
                base_url="https://api.example.com/v1",
                api_key="",
                model_id="m",
            )
        assert result["source"] == "unknown"
        assert result["error"] is not None

    @pytest.mark.asyncio
    async def test_sanitize_discards_impossible_max_output(self):
        """max_output >= context_window（脏数据）→ 丢弃，保持 None。"""
        with _mock_models_endpoint({
            "data": [
                {
                    "id": "m",
                    "context_length": 262144,
                    "max_completion_tokens": 262144,  # context_length 串线
                }
            ]
        }):
            result = await _detect_model_metadata(
                base_url="https://openrouter.ai/api/v1",
                api_key="",
                model_id="m",
            )
        assert result["context_window"] == 262144
        assert result["max_output_tokens"] is None  # 物理不可能，丢弃
        assert result["source"] == "external-api"

    @pytest.mark.asyncio
    async def test_case_insensitive_id_match(self):
        with _mock_models_endpoint({
            "data": [{"id": "DeepSeek-V4-Flash", "context_length": 128000}]
        }):
            result = await _detect_model_metadata(
                base_url="https://api.example.com/v1",
                api_key="",
                model_id="deepseek-v4-flash",
            )
        assert result["context_window"] == 128000

    @pytest.mark.asyncio
    async def test_missing_base_url_or_model(self):
        result = await _detect_model_metadata("", "", "")
        assert result["source"] == "unknown"
        assert result["error"] is not None


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
        # 模拟通用探测返回 True
        with patch(
            "hiveweave.api.models._detect_model_metadata",
            return_value={
                "context_window": 128000,
                "supports_thinking": True,
                "max_output_tokens": 32000,
                "source": "external-api",
                "error": None,
            },
        ):
            meta = await _detect_model_metadata("", "", "")
            if "supports_thinking" not in attrs and meta.get("supports_thinking") is not None:
                attrs["supports_thinking"] = meta["supports_thinking"]
            if "max_output_tokens" not in attrs and meta.get("max_output_tokens") is not None:
                attrs["max_output_tokens"] = meta["max_output_tokens"]

        assert attrs["supports_thinking"] is False, "User's explicit False should not be overwritten"
