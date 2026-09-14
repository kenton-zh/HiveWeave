"""日志/事件文本脱敏的**唯一实现**。

原实现是 ``services/offturn.py`` 里的私有 ``_SECRET_RE`` / ``_SK_RE`` /
``_redact``。fixplan #13 的「未识别上游错误样本」也要写原始文案进库，
需要同一套脱敏 ⇒ 收口到这里，**避免出现第 N 份密钥正则**（本仓此前已散落
在 ``tools/security.py`` / ``tools/executor.py`` 多处，再加一份只会扩大
「哪份才是漏的」的排查面）。

⚠ 只搬不改语义：两条正则与 ``_SECRET_RE.sub(r"\1***")`` 的替换串逐字保留，
``services/offturn.py`` 改为从这里导入。
"""

from __future__ import annotations

import re

#: ``key=… / token: … / Authorization: Bearer …`` —— 保留键名，吃掉值。
_SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|token|secret|authorization|bearer)\s*[=:]\s*(?:bearer\s+)?)\S+"
)

#: 裸 ``sk-…`` 形态（OpenAI 风格密钥）。与上面互补：它可能出现在任何位置，
#: 前面没有键名。
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")


def redact_secrets(text: str) -> str:
    """把密钥/令牌值替换为 ``***``；键名与结构保留（便于排查）。

    用于**写库/写日志之前**：任何来自上游响应体、异常消息、工具回执的文本
    在落盘前都应过一遍。**只脱敏，不做其它清洗** —— 不要在这里顺手改大小写
    或截断，那会让"看到的"和"实际的"不一致。
    """
    t = _SECRET_RE.sub(r"\1***", text or "")
    return _SK_RE.sub("sk-***", t)
