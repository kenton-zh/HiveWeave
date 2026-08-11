"""One-shot non-streaming LLM completion with a fixed model tier.

Unlike ``agents/agent.py:_review_llm_callback`` (which uses the agent's own
model config), this helper resolves the model by a **fixed tier** so cheap
review/audit sub-calls (e.g. ``request_code_audit``) never burn a
management-tier model. Never raises — every failure is logged and returned
as ``None``.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


async def llm_oneshot(
    project_id: str,
    tier: str,
    system_prompt: str,
    user_prompt: str,
    timeout_s: int = 60,
    temperature: float = 0.3,
    max_attempts: int = 1,
) -> str | None:
    """Single non-streaming LLM call resolved by tier; return raw text or None.

    - Model resolved via ``ModelService.resolve_model(tier=...)`` (primary →
      backup, strict, no cross-tier); ``None`` when unavailable.
    - Retryable HTTP failures (429 + 5xx, network errors) retried through
      ``llm/retry.py`` (``max_attempts`` total attempts; default 1 = no retry).
    - Holds the global LLM semaphore during the request — same concurrency
      cap as streaming calls (``HIVEWEAVE_LLM_MAX_CONCURRENT``), so parallel
      audits cannot bypass it.
    - Read timeout ``timeout_s`` — the upstream tool pipeline caps the whole
      call at 120s, so the defaults (60s read + 10s connect, no retry) keep
      the worst case well under that.
    - Any exception is logged and converted to ``None`` (soft-fail contract).
    """
    from hiveweave.llm.provider import provider_factory
    from hiveweave.services.model import ModelService

    model = await ModelService().resolve_model(tier=tier)
    if not model:
        log.warning("oneshot_no_model", project_id=project_id, tier=tier)
        return None

    try:
        provider = provider_factory.create(model)
    except Exception as exc:
        log.warning("oneshot_provider_failed", tier=tier, error=str(exc))
        return None

    import httpx

    from hiveweave.llm.streamer.constants import _get_llm_semaphore

    from hiveweave.llm.retry import (
        PermanentError,
        RetryHandler,
        RetryableError,
        classify_http_error,
    )

    try:
        body = provider.build_body(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
            temperature=temperature,
        )
    except Exception as exc:
        log.warning("oneshot_body_failed", tier=tier, error=str(exc))
        return None

    url = provider.build_url()
    headers = provider.build_headers()
    headers["Accept"] = "application/json"

    async def _do_request() -> str:
        sem = _get_llm_semaphore()
        async with sem:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0, read=timeout_s, write=10.0, pool=10.0
                )
            ) as client:
                resp = await client.post(url, json=body, headers=headers)
                if resp.status_code >= 400:
                    raise classify_http_error(
                        resp.status_code, resp.text[:500], dict(resp.headers)
                    )
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    return ""
                content = choices[0].get("message", {}).get("content")
                return content if isinstance(content, str) else ""

    handler = RetryHandler(max_retries=max(0, max_attempts - 1))
    try:
        return await handler.with_retry(_do_request)
    except (RetryableError, PermanentError) as exc:
        log.warning(
            "oneshot_request_failed", tier=tier, error=str(exc)
        )
        return None
    except Exception as exc:  # noqa: BLE001 — soft-fail contract
        log.warning("oneshot_unknown_error", tier=tier, error=str(exc))
        return None
