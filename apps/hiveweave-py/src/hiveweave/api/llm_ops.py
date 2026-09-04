"""LLM 熔断器诊断与手动解除 API（40 轮待办 #13）。

- GET  /api/llm/breakers                      — 全部 provider 熔断状态快照
- POST /api/llm/breakers/{provider}/reset     — 手动闭合指定 provider
                                                （充值后立即恢复，不再等冷却）

402（余额不足）等确定性计费错误混进 5 连败统计 → 熔断 open，充值后只能
等探针或重启。本路由让状态可见、解除可控。
"""

from __future__ import annotations

from fastapi import APIRouter

from hiveweave.llm.circuit_breaker import circuit_breaker

router = APIRouter(prefix="/api/llm", tags=["llm"])


@router.get("/breakers")
async def list_breakers() -> dict:
    """全部 provider 的熔断状态快照（state/fail_count/冷却剩余/fallback）。"""
    return {"breakers": circuit_breaker.snapshot()}


@router.post("/breakers/{provider}/reset")
async def reset_breaker(provider: str) -> dict:
    """手动闭合指定 provider 的熔断器（充值后立即恢复，无需重启后端）。"""
    known = any(b["provider"] == provider for b in circuit_breaker.snapshot())
    if not known:
        return {
            "ok": False,
            "error": f"unknown provider: {provider}",
            "known": [b["provider"] for b in circuit_breaker.snapshot()],
        }
    await circuit_breaker.reset(provider)
    entry = next(
        (b for b in circuit_breaker.snapshot() if b["provider"] == provider), {}
    )
    return {"ok": True, "provider": provider, "state": entry.get("state")}
