"""批29 N5（2026-10-09）：ORG_ESCALATION 失败签名剥 attestation 包装取内层。

病灶（s24/d90 实测）：5 个不同 agent 撞「同一堵墙」（签名
``[ATTESTATION] … Command exited with code 1``），实为 5 个工程师各自
**不同**的真实单测失败（TAP not ok 各不相同）——test_run 凭证横幅被算进
签名头部，签名窗口被「横幅 + 统一 exit 文案」占满，异质失败同桶，误导
协调者做「封路」级决策。

修法：对 attestation 包装的失败，剥掉 ``[ATTESTATION] attestation_id=…
kind=…`` 包装前缀后对剩余正文（真实命令输出 / TAP 差异）做归一化签名。
兼容：已有 memories 行不改；只影响新签名生成。
"""

from __future__ import annotations

from hiveweave.services.failure_signature import (
    signature_of,
    strip_attestation_wrapper,
)

_INNER_A = (
    "Command exited with code 1\n"
    "[stdout tail]\n"
    "not ok 1 - test_alpha_adds_row_to_ledger\n"
    "  expected: 2 rows\n"
    "  received: 1 rows"
)
_INNER_B = (
    "Command exited with code 1\n"
    "[stdout tail]\n"
    "not ok 7 - test_beta_purges_stale_cache\n"
    "  AssertionError: cache not empty after purge"
)


def _wrapped(inner: str, aid: str = "3f2a1b8c-9d4e-4f5a-8b7c-1d2e3f4a5b6c") -> str:
    """按 tools/bash._combine_attestation_output 的真实形态包装失败 error。"""
    banner = f"[ATTESTATION] attestation_id={aid} kind=test_run"
    footer = f"\n\n[attestation_id={aid} kind=test_run exit=1]"
    return f"{banner}\n{inner}{footer}"


# ── strip_attestation_wrapper ─────────────────────────────────────────


class TestStripWrapper:
    def test_strips_banner_first_line(self) -> None:
        stripped = strip_attestation_wrapper(_wrapped(_INNER_A))
        assert stripped.startswith("Command exited with code 1")
        assert "[ATTESTATION]" not in stripped
        assert "not ok 1" in stripped, "内层正文（TAP 差异）必须保留"

    def test_no_banner_unchanged(self) -> None:
        plain = "Command exited with code 1\n[stdout tail]\nboom happened here"
        assert strip_attestation_wrapper(plain) == plain

    def test_mid_text_banner_not_stripped(self) -> None:
        """锚定字符串开头：正文中段出现 [ATTESTATION] 字样不是包装前缀，不剥。"""
        text = "intro line\n[ATTESTATION] attestation_id=x kind=y tail"
        assert strip_attestation_wrapper(text) == text

    def test_banner_only_strips_to_empty(self) -> None:
        assert (
            strip_attestation_wrapper(
                "[ATTESTATION] attestation_id=x kind=test_run"
            )
            == ""
        )


# ── signature_of 指纹取内层 ───────────────────────────────────────────


class TestSignatureInnerFingerprint:
    def test_different_inner_failures_get_different_signatures(self) -> None:
        """① 两条不同内层失败的 attestation 包装命令 ⇒ 签名不同（不再同桶）。"""
        sig_a = signature_of(_wrapped(_INNER_A))
        sig_b = signature_of(_wrapped(_INNER_B))
        assert sig_a is not None and sig_b is not None
        assert sig_a != sig_b
        assert not sig_a.startswith("[ATTESTATION]"), "横幅不得再进签名"

    def test_same_inner_different_wrapper_ids_aggregate(self) -> None:
        """② 两条同内层（不同 run 的凭证 id 各异）⇒ 签名相同（聚合保持）。"""
        sig_a1 = signature_of(
            _wrapped(_INNER_A, aid="11111111-2222-4333-8444-555555555555")
        )
        sig_a2 = signature_of(
            _wrapped(_INNER_A, aid="99999999-8888-4777-8666-777777777777")
        )
        assert sig_a1 is not None
        assert sig_a1 == sig_a2

    def test_banner_only_error_not_broadcast(self) -> None:
        """整条 error 只有横幅（无内层正文）⇒ 无有效信息，不广播。"""
        assert (
            signature_of("[ATTESTATION] attestation_id=x kind=test_run")
            is None
        )

    def test_unwrapped_behavior_unchanged(self) -> None:
        """③ 无包装命令行为不变：签名仍由错误原文归一而来。"""
        plain = (
            "pwsh: Command not found: frobnicate the widget "
            "spans multiple words here yes indeed"
        )
        sig = signature_of(plain)
        assert sig is not None
        assert "frobnicate" in sig
        assert strip_attestation_wrapper(plain) == plain
