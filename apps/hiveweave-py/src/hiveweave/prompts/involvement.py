"""用户参与度三级（high / medium / low）— 契约 13.

移植自 Elixir streamer.ex:
  build_involvement_block / format_involvement_block / normalize_involvement_level

三级参与度（让渡的是决策权，不是诚实义务）：
  - high:   技术决策 / 产品决策 / 方向变更 全问用户
  - medium: 技术自主 + 产品/方向问用户（charter 写入默认）
  - low:    全自主，方向变更仅通知

不变部分：无论哪个级别，AI 都不能伪造结果、隐藏风险、跳过验证。

本模块为纯字符串构建，不访问 DB。
"""

from __future__ import annotations

# streamer 兜底默认值。charter 写入 DB 时默认 "宏观决策+技术选型"（medium），
# 但 streamer 在 charter 完全缺失 / legacy 自由文本时回落为 high。
DEFAULT_LEVEL: str = "high"


_LEVEL_BEHAVIORS: dict[str, str] = {
    "high": (
        "- 技术决策：必须问用户（via question 工具）\n"
        "- 产品/业务决策：必须问用户\n"
        "- 重大方向变更：必须问用户\n"
        "- 适用场景：用户有技术能力且想掌控方向"
    ),
    "medium": (
        "- 技术决策：AI 自主执行\n"
        "- 产品/业务决策：必须问用户\n"
        "- 重大方向变更：必须问用户\n"
        "- 适用场景：用户懂产品不懂技术，让渡技术决策权"
    ),
    "low": (
        "- 技术决策：AI 自主执行\n"
        "- 产品/业务决策：AI 自主执行\n"
        "- 重大方向变更：仅通知用户\n"
        "- 适用场景：用户完全信任 AI 或只想看结果"
    ),
}


_INVARIANT: str = (
    "**不变的部分**：无论哪个级别，AI 都不能伪造结果、不能隐藏风险、"
    "不能跳过验证。让渡的是决策权，不是诚实义务。"
)


def normalize_involvement_level(raw: str | None) -> str:
    """规整 raw 值为 high / medium / low。

    接受 "high" / "medium" / "low"（大小写不敏感）。
    legacy 自由文本（如 "宏观决策+技术选型"）回落为 "high"（streamer 兜底）。
    None / 空串 / 非字符串 → "high"。
    """
    if not isinstance(raw, str):
        return DEFAULT_LEVEL
    key = raw.strip().lower()
    if key in ("high", "medium", "low"):
        return key
    # legacy 自由文本默认 high
    return DEFAULT_LEVEL


def build_involvement_block(level: str) -> str:
    """按级别格式化 User Involvement 段。

    level 应为 normalize_involvement_level 的输出（high / medium / low）。
    未知级别回落为 high。

    返回的段每轮动态注入 context prompt，让 agent 始终知道当前自治级别。
    """
    norm = level if level in _LEVEL_BEHAVIORS else DEFAULT_LEVEL
    behavior = _LEVEL_BEHAVIORS[norm]
    return (
        f"## User Involvement（当前级别：{norm}）\n"
        f"{behavior}\n\n"
        f"{_INVARIANT}"
    )
