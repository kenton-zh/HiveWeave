"""Multimodal screenshot helpers — inject real pixels into LLM context.

Screenshots used to be path-only tool text. Multimodal models still cannot
"see" a file path. This module loads PNGs/JPEGs as base64 image payloads and
keeps history from exploding by retaining only the newest few images.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

import structlog

from hiveweave.util.tree_label import tree_tag
from hiveweave.util.tree_scope import (
    hit_note_for,
    ordered_tree_roots,
    shared_subdir_of,
)

log = structlog.get_logger()

# Soft cap — many gateways reject multi-MB inline images.
MAX_SCREENSHOT_BYTES = 2_000_000
# Keep only the newest N image-bearing messages in an active tool loop.
KEEP_LAST_IMAGES = 2

# 用户上传图（data URL → LLM）限幅：条数与 message_user 对齐；单张按
# base64 字符数封顶（2.8M chars ≈ 2.1MB 原始字节，略宽于 2MB 文件上限）。
MAX_USER_IMAGES = 5
MAX_USER_IMAGE_B64_CHARS = 2_800_000
# 单条消息图片总量约束（审计 P1-1）：5×2.8M ≈ 10.5MB 原始字节/请求过重，
# 且落库后逐轮全量重传直到压缩 —— 总 b64 封顶 8M chars，超限按序丢弃。
MAX_USER_IMAGES_TOTAL_B64_CHARS = 8_000_000

# 溢出剥图备注按消息来源分流（审计 P2-3）：工具截图 agent 可重截、有文件
# 路径；用户上传图两者皆无 —— 文案不得误导 agent 去 re-screenshot。
_USER_IMAGE_STRIPPED_NOTE = (
    "[用户消息附带的图片因上下文预算未注入 — "
    "如需像素请让用户重发]"
)
_TOOL_IMAGE_STRIPPED_NOTE = (
    "[image stripped from older context — "
    "re-screenshot if you still need pixels]"
)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def guess_media_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("image/"):
        return mime
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(suffix, "image/png")


def resolve_screenshot_under_project(
    project_root: str | None, raw: str | None
) -> Path | None:
    """Resolve a screenshot under the project root (incl. agent worktrees).

    Unlike :func:`resolve_screenshot_path` (agent-workspace sandbox), this
    allows ``<project>/.hiveweave/worktrees/<sid>/...`` so a reviewer can
    inspect an assignee screenshot stored on an attestation. ``..`` that
    escapes the project root is rejected.
    """
    return resolve_screenshot_path(project_root, raw)


def resolve_screenshot_path(workspace: str | None, raw: str | None) -> Path | None:
    """Resolve a screenshot path and sandbox it under ``workspace``.

    Absolute paths and ``..`` escapes outside the workspace are rejected
    (same contract as ``create_doc_review``).
    """
    return _resolve_screenshot(workspace, raw, project_root=None)


def resolve_screenshot_path_multi_tree(
    workspace: str | None,
    raw: str | None,
    project_root: str | None,
) -> tuple[Path | None, str]:
    """#5 读侧多树查找：先本树（workspace，含项目根），再跨树候选。

    判据来源（我们自己的模型，非 DSH）：
    - ``services/git_worktree/service_create.py:99-105`` ——
      ``.hiveweave/{shared,reports,drafts,handoffs}`` 四目录反选入库、
      **跨 worktree 可见可合并**；
    - 写侧因此有**单一权威落点**（截图/契约一律落 MAIN 的
      ``.hiveweave/reports/``）；读侧只查一棵树就失败，正是
      ``look_at_image`` 读 MAIN 截图**必失败**的成因（fixplan §10.2）。

    跨树候选序见 :func:`_multi_tree_bases`：**MAIN → 本树 → 兄弟 worktree**。

    返回 ``(path | None, note)``：``note`` 说明**在哪棵树命中**或**查过哪些树**
    ——多树语境下这是归因的必要条件（fixplan §10.5）。不做"确实不存在"断言。
    """
    resolved = _resolve_screenshot(workspace, raw, project_root=project_root)
    if resolved is not None and resolved.is_file():
        return resolved, ""
    if not raw or not str(raw).strip():
        return None, ""
    # 本树未命中 → 跨树候选（MAIN → 兄弟树）。相对路径才有跨树语义：
    # 绝对路径、或含 `..` 逃逸的相对路径，一律不跨树猜（隔离不减）。
    cand = Path(str(raw).strip().strip("\"'"))
    if cand.is_absolute():
        return None, ""
    rel = str(cand).replace("\\", "/").lstrip("/")
    if ".." in Path(rel).parts:
        return None, ""
    ws_key = _norm_key(workspace)
    tried: list[str] = []
    for base in _multi_tree_bases(workspace, project_root):
        try:
            full = (Path(base) / rel).resolve()
        except (OSError, ValueError):
            continue
        tag = tree_tag(str(full))
        tried.append(tag)
        if full.is_file():
            if _key_within(_norm_key(str(full)), ws_key):
                return full, ""      # 本树命中：无跨树归因需求
            # 归因句子按**子目录**分派（`util/tree_scope.hit_note_for`）：本函数
            # 接受任意相对路径，原先无论命中哪个子目录都印 reports 的
            # "written to MAIN by design"（09-16 二轮审计指出：shared 命中时这句
            # 是反向认知，与本批的"shared 无权威落点"冲突）。
            return full, f" [read from {tag}{hit_note_for(shared_subdir_of(rel) or '')}]"
    if not tried:
        return None, ""
    return None, (
        f" [searched {len(tried)} tree(s): {', '.join(tried)}"
        " — not found in any of them; this is not proof the image"
        " does not exist]"
    )


def _norm_key(p: str | None) -> str:
    """路径比较键（realpath + normcase）。空值返回空串。"""
    if not p or not str(p).strip():
        return ""
    try:
        return os.path.normcase(os.path.realpath(str(p)))
    except (OSError, ValueError):
        return ""


def _key_within(child_key: str, base_key: str) -> bool:
    """``child_key`` 是否落在 ``base_key`` 内（**按路径分量**，非裸前缀）。

    裸 ``str.startswith`` 会把 ``C:\\proj-wt`` 误判成 ``C:\\proj`` 的子路径
    —— 多树场景下两个 worktree 目录名常常是长同名前缀（``A044``/``A0440``），
    必须走 ``commonpath``。
    """
    if not child_key or not base_key:
        return False
    if child_key == base_key:
        return True
    try:
        return os.path.commonpath([child_key, base_key]) == base_key
    except ValueError:
        return False


def _multi_tree_bases(
    workspace: str | None, project_root: str | None
) -> list[str]:
    """候选树根（去重、保序）：**MAIN → 本树 → 兄弟 worktree**。

    判据出处同 :func:`resolve_screenshot_path_multi_tree`：共享产物的权威
    落点是 MAIN（``service_create.py:99-105``），所以 MAIN 排第一；
    兄弟树是 `§10.2`「MAIN → 请求者 → assignee」在同一项目命名空间
    （``dispatch_pin.py:7,34``）下的上界。

    09-16（②）：顺序改由 ``util/tree_scope.ordered_tree_roots`` **唯一权威**
    给出（按子目录合并策略参数化，reports 与 shared 不同），本函数只做
    `(workspace, project_root)` → `(root, workspace)` 的形参转接。
    ⚠ 旧实现与 ``tools/file.py::_reports_read_scope`` **在"请求者树不是排序
    第一个兄弟"时给出不同顺序**（旧实现把兄弟树全排在请求者树之前）——
    本函数签名与返回类型不变，故调用方无需改动。
    """
    return ordered_tree_roots(project_root, workspace, local_first=False)


def _resolve_screenshot(
    workspace: str | None, raw: str | None, *, project_root: str | None
) -> Path | None:
    """``resolve_screenshot_path`` 的实现体。

    ``project_root`` 为 None 时只允许 workspace 内（原契约）；
    给出时在**同一函数内**允许多一层项目根（shared 产物的权威落点）——
    这样"共享设计"与"隔离实现"用同一段判定，不会两处漂移。
    """
    if not raw or not str(raw).strip():
        return None
    if not workspace or not str(workspace).strip():
        return None
    try:
        root = Path(workspace).resolve()
    except OSError:
        return None
    bases = [root]
    if project_root and str(project_root).strip():
        try:
            proj = Path(project_root).resolve()
        except OSError:
            proj = None
        if proj is not None and proj != root:
            bases.append(proj)
    candidate = Path(str(raw).strip().strip("\"'"))
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    for base in bases:
        try:
            resolved.relative_to(base)
            return resolved
        except ValueError:
            continue
    log.warning(
        "vision.path_escape",
        path=str(resolved),
        workspace=str(root),
        project_root=str(project_root or ""),
    )
    return None


def load_image_for_llm(
    path: Path | str,
    *,
    max_bytes: int = MAX_SCREENSHOT_BYTES,
) -> dict[str, str] | None:
    """Load an image file into ``{media_type, data}`` (base64) for providers.

    Returns None when missing, non-image, or over size cap (caller should
    still keep the text path in the tool result).
    """
    p = Path(path)
    if not p.is_file():
        log.info("vision.image_missing", path=str(p))
        return None
    if p.suffix.lower() not in _IMAGE_SUFFIXES:
        log.info("vision.not_image_suffix", path=str(p), suffix=p.suffix)
        return None
    try:
        size = p.stat().st_size
    except OSError as e:
        log.warning("vision.stat_failed", path=str(p), error=str(e))
        return None
    if size <= 0:
        return None
    if size > max_bytes:
        log.warning(
            "vision.image_too_large",
            path=str(p),
            bytes=size,
            max_bytes=max_bytes,
        )
        return None
    try:
        raw = p.read_bytes()
    except OSError as e:
        log.warning("vision.read_failed", path=str(p), error=str(e))
        return None
    return {
        "media_type": guess_media_type(p),
        "data": base64.b64encode(raw).decode("ascii"),
        "path": str(p),
    }


def parse_user_images(data_urls: Any) -> list[dict[str, str]]:
    """Parse user-uploaded image payloads into internal ``{media_type, data}`` dicts.

    Frontend sends data URLs (``data:image/png;base64,XXXX``) over WS chat
    push and REST ``POST /api/chat``. chat_messages already stores the
    originals (UI display); this list is what the LLM actually sees.

    Fail-open: invalid entries (non-string / bad prefix / non-base64 /
    bad charset / non-image mime / empty / over-size) are skipped with a
    log, never raise. Caps: at most :data:`MAX_USER_IMAGES` images, each
    base64 payload at most :data:`MAX_USER_IMAGE_B64_CHARS` chars, and the
    whole message's base64 total at most
    :data:`MAX_USER_IMAGES_TOTAL_B64_CHARS` (over-budget images dropped
    in order).
    """
    if not isinstance(data_urls, list):
        return []
    out: list[dict[str, str]] = []
    total_b64 = 0
    for idx, raw in enumerate(data_urls):
        if len(out) >= MAX_USER_IMAGES:
            log.warning(
                "vision.user_images_capped",
                max_images=MAX_USER_IMAGES,
                dropped=len(data_urls) - MAX_USER_IMAGES,
            )
            break
        if not isinstance(raw, str):
            log.info("vision.user_image_skip_non_string")
            continue
        s = raw.strip()
        if not s.startswith("data:"):
            log.info("vision.user_image_bad_prefix", preview=s[:32])
            continue
        header, _, b64 = s.partition(",")
        if "base64" not in header:
            log.info("vision.user_image_not_base64", preview=header[:64])
            continue
        media_type = header[len("data:"):].split(";", 1)[0].strip() or "image/png"
        if not media_type.startswith("image/"):
            log.info("vision.user_image_not_image", media_type=media_type)
            continue
        b64 = b64.strip()
        if not b64:
            log.info("vision.user_image_empty_data")
            continue
        # base64 字符集校验（审计 P2-1）：脏字符（@@、! 等）原样进请求体会
        # 被严格网关整包 400，一条坏图炸掉整条消息。
        if not re.fullmatch(r"[A-Za-z0-9+/=\s]+", b64):
            log.info(
                "vision.user_image_bad_charset",
                media_type=media_type,
                preview=b64[:32],
            )
            continue
        if len(b64) > MAX_USER_IMAGE_B64_CHARS:
            log.warning(
                "vision.user_image_too_large",
                chars=len(b64),
                max_chars=MAX_USER_IMAGE_B64_CHARS,
            )
            continue
        # 聚合上限（审计 P1-1）：总 b64 封顶，越界按序丢弃（不回填后续小图）。
        if total_b64 + len(b64) > MAX_USER_IMAGES_TOTAL_B64_CHARS:
            log.warning(
                "vision.image_aggregate_capped",
                accepted=len(out),
                dropped_remaining=len(data_urls) - idx,
                total_b64_chars=total_b64,
                cap_chars=MAX_USER_IMAGES_TOTAL_B64_CHARS,
            )
            break
        total_b64 += len(b64)
        out.append({"media_type": media_type, "data": b64})
    return out


def strip_images_from_messages(
    messages: list[dict[str, Any]],
    *,
    keep_last: int = KEEP_LAST_IMAGES,
) -> list[dict[str, Any]]:
    """Drop ``images`` from all but the newest ``keep_last`` image messages.

    Compaction-only: rewriting older message bodies (pixels + the
    ``[image stripped…]`` note) invalidates DeepSeek prefix cache from that
    token. The tool loop must call this at overflow, not every round.
    Does not mutate the input list in place.
    """
    if keep_last < 0:
        keep_last = 0
    indexed = [
        i for i, m in enumerate(messages)
        if isinstance(m, dict) and m.get("images")
    ]
    if len(indexed) <= keep_last:
        return messages
    drop = set(indexed[: max(0, len(indexed) - keep_last)])
    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        if i in drop and isinstance(m, dict) and "images" in m:
            cleaned = {k: v for k, v in m.items() if k != "images"}
            note = cleaned.get("content") or ""
            # 按来源分流备注（审计 P2-3）：用户上传图无文件路径、agent 无法
            # 重截 —— 「re-screenshot / 路径仍在」只对工具截图成立。
            marker = (
                _USER_IMAGE_STRIPPED_NOTE
                if m.get("role") == "user"
                else _TOOL_IMAGE_STRIPPED_NOTE
            )
            if (
                _USER_IMAGE_STRIPPED_NOTE not in note
                and _TOOL_IMAGE_STRIPPED_NOTE not in note
            ):
                cleaned["content"] = f"{note}\n{marker}".strip()
            out.append(cleaned)
        else:
            out.append(m)
    return out


def messages_without_images(
    messages: list[dict[str, Any]],
    *,
    keep_user: bool = False,
) -> list[dict[str, Any]]:
    """Strip image payloads (for conversation-store persistence).

    ``keep_user=True`` preserves ``images`` on user turns — user-uploaded
    images must stay visible in later turns' history (provider renders them
    per request; text-only models strip at request build). In-flight
    tool-loop screenshots stay strip-always: pixels are only needed in the
    current loop; the next turn can re-screenshot.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if (
            isinstance(m, dict)
            and "images" in m
            and not (keep_user and m.get("role") == "user")
        ):
            cleaned = {k: v for k, v in m.items() if k != "images"}
            out.append(cleaned)
        else:
            out.append(m)
    return out


# ── 批次 D：请求级图片预算确定性 offload ─────────────────────────
#: 预算（字节）：全部历史图片 base64 解码后总大小超过此值 → 最老优先淘汰。
#: 20 MiB 对齐 DSH maxInlineRequestImageBytes 默认（网关请求体积帽）。
IMAGE_BUDGET_BYTES = 20 * 1024 * 1024

_OFFLOAD_PLACEHOLDER = "[image offloaded: exceeded request image budget]"


def offload_old_images(
    messages: list[dict[str, Any]],
    budget_bytes: int = IMAGE_BUDGET_BYTES,
) -> list[dict[str, Any]]:
    """确定性图片预算 offload：超预算时按最老优先淘汰历史图片。

    只修改返回的副本（不改输入），淘汰图片的 ``images`` 列表替换为
    占位文本以保持对话结构。最近的消息图片优先保留。确定性：
    相同输入永远产生相同输出（无随机/时序依赖）。
    """
    import copy as _copy

    result = _copy.deepcopy(messages)

    # 收集全部 (msg_idx, img_idx, data_len) 按消息序
    entries: list[tuple[int, int, int]] = []
    total = 0
    for mi, msg in enumerate(result):
        imgs = msg.get("images")
        if not isinstance(msg, dict) or not isinstance(imgs, list):
            continue
        for ii, img in enumerate(imgs):
            data = ""
            if isinstance(img, dict):
                data = img.get("data") or ""
            elif isinstance(img, str):
                data = img
            if data:
                entries.append((mi, ii, len(data)))
                total += len(data)

    if total <= budget_bytes:
        return result

    # 最老优先淘汰：替换 data 为占位文本
    for mi, ii, _sz in entries:
        if total <= budget_bytes:
            break
        msg = result[mi]
        imgs = msg.get("images")
        if not isinstance(imgs, list) or ii >= len(imgs):
            continue
        img = imgs[ii]
        if isinstance(img, dict):
            removed_len = len(img.get("data") or "")
            img["data"] = _OFFLOAD_PLACEHOLDER
        elif isinstance(img, str):
            removed_len = len(img)
            imgs[ii] = _OFFLOAD_PLACEHOLDER
        else:
            continue
        total -= removed_len

    return result

def openai_image_parts(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI Chat Completions image_url parts from internal image dicts."""
    parts: list[dict[str, Any]] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        data = img.get("data") or ""
        if not data:
            continue
        media = img.get("media_type") or "image/png"
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media};base64,{data}"},
        })
    return parts


def anthropic_image_blocks(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic content blocks for images."""
    blocks: list[dict[str, Any]] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        data = img.get("data") or ""
        if not data:
            continue
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("media_type") or "image/png",
                "data": data,
            },
        })
    return blocks


def gemini_image_parts(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gemini generativeLanguage inlineData parts."""
    parts: list[dict[str, Any]] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        data = img.get("data") or ""
        if not data:
            continue
        parts.append({
            "inlineData": {
                "mimeType": img.get("media_type") or "image/png",
                "data": data,
            },
        })
    return parts


def _text_from_message(msg: dict[str, Any]) -> str:
    """Prefer visible content; fall back to reasoning/thinking fields."""
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        bits: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                bits.append(part["text"])
        joined = "".join(bits)
        if joined.strip():
            return joined
    for key in ("reasoning_content", "reasoning", "thinking"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def extract_nonstream_text(data: dict[str, Any]) -> str:
    """Pull assistant text from a non-streaming chat completion body.

    Supports OpenAI ``choices``, Anthropic ``content`` blocks, Gemini
    ``candidates``, and the openai-responses non-stream shape
    (``object=response`` + ``output[]``) — same providers as
    ``provider_factory``.
    When ``message.content`` is empty (common with thinking models), falls
    back to ``reasoning_content`` / ``thinking``.
    """
    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            text = _text_from_message(msg)
            if text:
                return text

    content = data.get("content")
    if isinstance(content, list):
        bits = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                bits.append(block["text"])
        if bits:
            return "".join(bits)
    if isinstance(content, str) and content.strip():
        return content

    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates:
        c0 = candidates[0] if isinstance(candidates[0], dict) else None
        parts = (c0 or {}).get("content", {}).get("parts") if c0 else None
        if isinstance(parts, list):
            bits = [
                p["text"]
                for p in parts
                if isinstance(p, dict) and isinstance(p.get("text"), str)
            ]
            return "".join(bits)

    # openai-responses 非流式形态（TEST_DSH_62：muse-spark 实际能看图且
    # 上游回答完整，但网关回的是 object=response 的 Responses 协议体而非
    # chat choices，旧解析读成空 → "Vision model returned empty content"）。
    # 字段路径对齐 llm/openai_responses.py:_chunks_from_complete_response
    # （SSE 路径的同类解析）：output[] 的 message 条目 → content[] 里
    # type=="output_text" 的 .text；reasoning / function_call 等条目不含
    # 可见文本，跳过。status=completed 但无文本则落空返回 ""，交给调用方
    # 既有报错路径。
    output = data.get("output")
    if data.get("object") == "response" or isinstance(output, list):
        bits = []
        for item in output or []:
            if not isinstance(item, dict):
                continue
            for part in item.get("content") or []:
                if (
                    isinstance(part, dict)
                    # "text" 与 SSE 路径同口径兜底（部分网关回简写形态）
                    and part.get("type") in ("output_text", "text")
                    and isinstance(part.get("text"), str)
                ):
                    bits.append(part["text"])
        if bits:
            return "".join(bits)

    return ""


async def analyze_image(
    *,
    image: dict[str, str],
    prompt: str,
    model_config: dict[str, Any],
    timeout_s: float = 120.0,
) -> str:
    """One-shot non-streaming multimodal call. Stateless — no history.

    ``image`` is ``{media_type, data}`` from :func:`load_image_for_llm`.
    Returns the full assistant text (never streams to the caller).
    Thinking/reasoning mode is forced off so answers land in ``content``.
    """
    import httpx

    from hiveweave.llm.provider import provider_factory
    from hiveweave.llm.retry import (
        RetryHandler,
        RetryableError,
        is_retryable_status,
    )

    # Vision one-shot wants visible content, not a thinking-only body.
    # （supports_images 不在此强设：provider_factory.create 只认
    # is_image_supported() 自动探测，不读 model_config["supports_images"]，
    # 写了也是死代码，已摘除。）
    cfg = dict(model_config)
    cfg["supports_thinking"] = False
    cfg["default_reasoning_effort"] = None

    provider = provider_factory.create(cfg)
    body = provider.build_body(
        messages=[
            {
                "role": "user",
                "content": prompt.strip(),
                "images": [image],
            },
        ],
        stream=False,
        temperature=0.2,
        tools=None,
    )
    headers = provider.build_headers()
    headers["Accept"] = "application/json"

    async def _once() -> str:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0, read=timeout_s, write=10.0, pool=10.0
            ),
        ) as client:
            resp = await client.post(
                provider.build_url(),
                json=body,
                headers=headers,
            )
            # 与流式路径同口径：429/5xx 瞬态错误交给 RetryHandler 指数退避重试
            # （含 Retry-After）。之前 raise_for_status 直接抛，视觉门禁一遇到
            # 限流就废掉 → 团队只能 waive visual/module_visual。
            if is_retryable_status(resp.status_code):
                raise RetryableError(
                    f"vision HTTP {resp.status_code}: {resp.text[:500]}",
                    status=resp.status_code,
                    headers=dict(resp.headers),
                )
            resp.raise_for_status()
            data = resp.json()
        text = extract_nonstream_text(data).strip()
        if not text:
            raise RuntimeError("Vision model returned empty content")
        return text

    return await RetryHandler(
        on_retry=lambda attempt, delay_ms, exc: log.info(
            "vision_http_retry",
            attempt=attempt,
            delay_ms=delay_ms,
            error=str(exc)[:200],
        )
    ).with_retry(_once)

