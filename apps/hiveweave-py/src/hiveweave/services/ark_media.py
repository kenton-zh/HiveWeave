"""Ark Agent Plan media endpoint helpers (image generations).

Base URL in Settings should be the Plan root
``https://ark.cn-beijing.volces.com/api/plan/v3`` (never mix with
``/api/v3`` or Coding Plan). Tools append capability-specific paths.
"""

from __future__ import annotations

_IMAGE_GENERATIONS_SUFFIX = "/images/generations"
_KNOWN_SUFFIXES = (
    "/images/generations",
    "/contents/generations/tasks",
)


def normalize_plan_root(base_url: str | None) -> str:
    """Normalize a configured Base URL to the Agent Plan API root.

    Accepts either the Plan root or a full endpoint URL that includes a
    known media path suffix; strips those suffixes so callers can re-append.
    """
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        return ""
    lower = raw.lower()
    for suffix in _KNOWN_SUFFIXES:
        if lower.endswith(suffix):
            return raw[: -len(suffix)].rstrip("/")
    return raw


def is_agent_plan_root(base_url: str | None) -> bool:
    """True iff URL is (or normalizes to) an Agent Plan root containing ``/api/plan/``."""
    root = normalize_plan_root(base_url)
    if not root:
        return False
    lower = root.lower()
    if "/api/coding/" in lower:
        return False
    # Plan channel must include /api/plan/ — reject bare /api/v3
    return "/api/plan/" in lower


def images_generations_url(base_url: str | None) -> str | None:
    """Return ``{plan_root}/images/generations``, or None if not a Plan root."""
    if not is_agent_plan_root(base_url):
        return None
    root = normalize_plan_root(base_url)
    return f"{root}{_IMAGE_GENERATIONS_SUFFIX}"
