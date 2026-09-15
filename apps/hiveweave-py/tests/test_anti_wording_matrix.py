"""§七 反措辞测试矩阵（fixplan-16items-2026-09-14）。

计划 §七 的联合验收：**同一语义 × 多种措辞/语言变体 ⇒ 判定结果完全一致**。
它不替代各条自己的验收测试；它锁的是「换措辞/换语言即绕过」这一族病
（文本判据）在批次 1 各条的**新判据**上不再成立。

矩阵行 ↔ 本文件用例组（计划 §七 表）：

| 条目 | 用例组 |
|---|---|
| #8 | ``TestMatrix8DeliveryState``（徽章只反映状态位，与正文措辞无关） |
| #8 侧门 | ``test_matrix8_side_door_*``（收件人身份判定收口 + 私有别名表源码守卫） |
| #11 | ``TestMatrix11VerifyKind``（VERIFY 判定只读 kind，标题任意改） |
| #12 | ``TestMatrix12SeverityFailsafe``（severity：只有显式 ``SEVERITY:low`` 放行，外语/全角/无标记一律 fail-safe 拦） |
| #13 | ``TestMatrix13UpstreamClassify``（上游错误分类 status 优先；文本 fallback 是第二层且有留痕） |
| #14 | ``TestMatrix14AcceptanceIds``（验收覆盖只认「声明 id + 平台凭证/waiver」，文本相似度无关） |
| #15 | ``TestMatrix15FactBits``（失败归因布尔位优先；位缺失 ⇒ unknown 不猜，外语措辞驱动不了归因） |
| #16 | ``test_matrix16_no_bypass_recipes_in_prompts``（(b) 档「绕过配方」在 prompts/ 零残留） |
| P-1 | ``test_matrix_p1_ratchet_sees_new_table``（棘轮尺子看见新增文本表；存量当前零增长） |

**边界（每个守卫不守什么）**：#12 的真闸门此刻仍在 shadow（``1b89a13``），
本组锁的是 **fail-safe 判定语义本身**（解析 + ``high+unparsed`` 合成式），
「切真拦」时语义不变则本组持续绿。#15 锁第一/二层判据函数；「位缺失时
上层是否真落 unknown」由 #15 主验收（``738f850``）的调用方测试守。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.tasks.acceptance import (
    uncovered_acceptance_items,
)
from hiveweave.services.tasks.verify import is_verify_task
from hiveweave.services.wake_policy import is_user_sender
from hiveweave.tools.fact_positions import (
    classify_error_text,
    fact_from_bits,
)
from hiveweave.tools.misc_tools import (
    MarkDeliveryCompleteParams,
    MessageUserParams,
    mark_delivery_complete_tool,
    message_user_tool,
)
from hiveweave.tools.orchestration_tools import is_user_recipient

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "hiveweave"

PROJECT_ID = "test-anti-wording-matrix"
CEO_ID = "matrix-ceo-uuid"

# §七 #8 行：同一「完工」语义的 5 种表达（中/英/法/西 + 旧词表时代的中性绕过形态）。
_DONE_PHRASINGS = [
    "项目已全部完成，可以交付使用",
    "All done, ready for delivery.",
    "Tout est terminé, prêt à livrer.",
    "Todo está completo, listo para entregar.",
    "记录之六：本阶段工作纪要如下（不做完工判断）。",
]


# ── 共用夹具（#8 组需要真实项目 DB；其余组是纯函数） ─────────────────


@pytest.fixture
async def ceo_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.clear()
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"workspace_path": workspace_path}
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _identity_patch(role: str = "ceo"):
    # ⚠ patch misc_tools 里的名字（from-import 是导入期绑定，见
    # test_ceo_exit_assertion.py::_identity_patch 的注释）。
    return [
        patch(
            "hiveweave.tools.misc_tools.get_project_id",
            new=AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.services.org.OrgService.get_agent",
            new=AsyncMock(
                return_value={"id": CEO_ID, "role": role, "permission_type": "coordinator"}
            ),
        ),
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value=role,
        ),
    ]


async def _send_ceo(message: str, ws: str):
    save_mock = AsyncMock()
    with ExitStack() as stack:
        for cm in _identity_patch():
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "hiveweave.services.chat_message.ChatMessageService.save_message",
                new=save_mock,
            )
        )
        stack.enter_context(
            patch(
                "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
                new=AsyncMock(),
            )
        )
        result = await message_user_tool(
            MessageUserParams(message=message), CEO_ID, ws
        )
    return result, save_mock


# ── #8：徽章只反映状态位 ────────────────────────────────────────────


class TestMatrix8DeliveryState:
    """同一定义「完成」的 5 种措辞 ⇒ metadata 完全一致（判定零文本参与）。"""

    @pytest.mark.asyncio
    async def test_badge_invariant_at_unmarked_state(self, ceo_env):
        results, metas = [], []
        for text in _DONE_PHRASINGS:
            result, save = await _send_ceo(text, ceo_env["workspace_path"])
            results.append(result)
            metas.append(save.call_args.args[0]["metadata"])
        assert all(r.success is True for r in results)
        assert all(m["delivery_state"] == "unmarked" for m in metas)
        blobs = {json.dumps(m, ensure_ascii=False, sort_keys=True) for m in metas}
        assert len(blobs) == 1, f"措辞改变了 metadata：{blobs}"

    @pytest.mark.asyncio
    async def test_badge_invariant_at_complete_state(self, ceo_env):
        with ExitStack() as stack:
            for cm in _identity_patch():
                stack.enter_context(cm)
            marked = await mark_delivery_complete_tool(
                MarkDeliveryCompleteParams(), CEO_ID, ceo_env["workspace_path"]
            )
        assert marked.success is True, marked.error

        metas = []
        for text in _DONE_PHRASINGS:
            result, save = await _send_ceo(text, ceo_env["workspace_path"])
            assert result.success is True
            metas.append(save.call_args.args[0]["metadata"])
        assert all(m["delivery_state"] == "complete" for m in metas)
        blobs = {json.dumps(m, ensure_ascii=False, sort_keys=True) for m in metas}
        assert len(blobs) == 1, f"措辞改变了 metadata：{blobs}"

    @pytest.mark.asyncio
    async def test_badge_invariant_at_dirty_ledger(self, ceo_env):
        """脏账本 × 五语料象限：账本不干净时，任何措辞的完工声明同样
        **不拦**且徽章完全一致（unmarked + 同一份阻塞项列表）——堵住
        「仅脏账本时按措辞拦」的回归形态。"""
        import time as _time

        conn = await project_db.ensure_project_db(ceo_env["workspace_path"])
        now = int(_time.time() * 1000)
        await conn.execute(
            "INSERT INTO tasks (id, project_id, title, creator_id, assignee_id,"
            " status, evidence, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "matrix-dirty-1", PROJECT_ID, "t", CEO_ID, CEO_ID, "running",
                json.dumps({"verdict": "FAIL", "blocking_issues": ["x"]}),
                now, now,
            ],
        )
        await conn.commit()

        metas = []
        for text in _DONE_PHRASINGS:
            result, save = await _send_ceo(text, ceo_env["workspace_path"])
            assert result.success is True
            meta = save.call_args.args[0]["metadata"]
            assert meta["delivery_state"] == "unmarked"
            metas.append(meta)
        blobs = {json.dumps(m, ensure_ascii=False, sort_keys=True) for m in metas}
        assert len(blobs) == 1, f"措辞改变了 metadata：{blobs}"


def test_matrix8_side_door_recipient_identity_is_structured():
    """#8 侧门：收件人身份判定收口到唯一权威集合（``wake_policy._USER_IDS``）。

    权威集合内的别名（含中文 UI 标记）都算人类用户；集合外的称呼词
    （``boss`` / ``老板`` 等私有别名）**不再**被当成人类 —— 判据是身份，
    不是称呼措辞。
    """
    for alias in ("user", "human", "operator", "用户", "USER", " Human "):
        assert is_user_recipient(alias), alias
        assert is_user_sender(alias), alias
    for not_user in ("boss", "老板", "开发负责人", "ceo", "user-bot"):
        assert not is_user_recipient(not_user), not_user


def test_matrix8_side_door_no_private_alias_table_in_source():
    """#8 侧门源码守卫：文件里不得再出现「称呼词集合字面量」（旧
    ``user_aliases = {"user","用户","boss","老板"}`` 的复活即红）。

    只查 AST 的 Tuple/Set/List 字面量成员 —— docstring/注释里**复述这段
    历史**（现文件就是这么写的）不算违规，判据表复活才算。"""
    import ast as _ast

    path = _SRC_ROOT / "tools" / "orchestration_tools.py"
    tree = _ast.parse(path.read_text(encoding="utf-8"))
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.Tuple, _ast.Set, _ast.List)):
            continue
        members = {
            elt.value for elt in node.elts if isinstance(elt, _ast.Constant)
            and isinstance(elt.value, str)
        }
        if {"user", "用户"} <= members:
            pytest.fail(
                f"{path.name}:{node.lineno} 出现人类身份别名集合字面量 "
                f"{members} —— 身份判定必须走 wake_policy 的唯一权威集合"
            )


# ── #11：VERIFY 判定只读 kind ───────────────────────────────────────


class TestMatrix11VerifyKind:
    """同一 kind 的任务换任何标题 ⇒ 判定不变；任何标题也造不出 VERIFY。"""

    @pytest.mark.parametrize(
        "title",
        ["VERIFY: api 收口", "验收：api 收口", "QA：api", "vérification : api", None, ""],
    )
    def test_verify_kind_survives_any_title(self, title):
        assert is_verify_task({"kind": "verify", "title": title}) is True

    @pytest.mark.parametrize(
        "title",
        ["VERIFY: 伪装", "验收：伪装", "[VERIFY] 伪装", "verification déguisée"],
    )
    def test_verify_prefix_cannot_grant_kind(self, title):
        assert is_verify_task({"title": title}) is False
        assert is_verify_task({"kind": None, "title": title}) is False

    def test_no_runtime_title_regex_in_verify_module(self):
        """源码守卫：运行时标题判据不得回到 verify.py（迁移模块里除外）。"""
        src = (_SRC_ROOT / "services" / "tasks" / "verify.py").read_text(encoding="utf-8")
        assert "_VERIFY_TITLE_RE = re.compile" not in src
        assert "def is_verify_title" not in src


# ── #12：severity fail-safe ─────────────────────────────────────────


class TestMatrix12SeverityFailsafe:
    """§七 #12 行：只有显式 ``SEVERITY:low`` 放行；全角/中文/法/西/德文与
    无标记一律落 ``unparsed`` ⇒ fail-safe（缺失即按 high 拦）。

    ⚠ 真闸门此刻在 shadow（``1b89a13``）：本组锁**判定语义**（解析 +
    ``high + unparsed`` 合成式）；切「真拦」只是让这套语义开始拦门，
    语义不变则本组持续绿（合成式由下面的源码守卫钉住）。
    """

    #: (issue 文本, 期望 severity 或 None)。外语前缀**都不命中** ``SEVERITY:``
    #: ⇒ 在 fail-safe 下一律被拦 —— 这组用例本身就是「为什么必须 fail-safe」
    #: 的证据：老 fail-open 会让它们**静默放行**。
    VARIANTS = [
        ("SEVERITY:low", "low"),
        ("severity：medium", "medium"),
        ("[high] 内存泄漏", "high"),          # 旧括号形态（shadow 兼容期）
        ("【high】内存泄漏", None),           # 全角 —— 旧判据漏掉的那批
        ("严重程度：高", None),
        ("高", None),
        ("élevé", None),
        ("SÉVERITÉ:faible", None),            # 重音符 ⇒ 不命中 ASCII 前缀
        ("GRAVEDAD:baja", None),
        ("SCHWEREGRAD:niedrig", None),
        ("", None),
    ]

    def test_only_explicit_low_is_not_high(self):
        from hiveweave.services.code_audit import parse_issue_severity

        for issue, expected in self.VARIANTS:
            got = parse_issue_severity(issue)
            assert got == expected, f"{issue!r}: {got!r} != {expected!r}"

    def test_failsafe_blocks_everything_but_explicit_non_high(self):
        """fail-safe 合成式：``high + unparsed > 0`` ⇒ 拦。所有外语变体都拦。"""
        from hiveweave.services.code_audit import count_issue_severities

        foreign = ["【high】", "高", "élevé", "SÉVERITÉ:faible", "GRAVEDAD:baja"]
        counts = count_issue_severities(foreign)
        assert counts["unparsed"] == len(foreign)
        assert counts["high"] + counts["unparsed"] > 0  # ⇒ 拦

        explicit_low = count_issue_severities(["SEVERITY:low"])
        assert explicit_low["high"] + explicit_low["unparsed"] == 0  # ⇒ 不拦

    def test_conflict_rule_first_prefix_wins_and_is_logged(self):
        """``SEVERITY:low … 【high】`` ⇒ 按 low（只认首个前缀），且冲突被上报。"""
        from hiveweave.services.code_audit import (
            count_issue_severities,
            parse_issue_severity,
            severity_conflict,
        )

        issue = "SEVERITY:low 顺带修了【high】的历史遗留"
        assert parse_issue_severity(issue) == "low"
        assert severity_conflict(issue) is True
        counts = count_issue_severities([issue])
        assert counts["conflicts"] == 1
        assert counts["low"] == 1 and counts["unparsed"] == 0

    def test_failsafe_composition_source_guard(self):
        """源码守卫：fail-safe 合成式不得退回 ``unparsed`` 不计入的形态。"""
        src = (_SRC_ROOT / "services" / "code_audit.py").read_text(encoding="utf-8")
        assert '_counts["high"] + _counts["unparsed"]' in src


# ── #13：上游错误分类 status 优先 ───────────────────────────────────


class TestMatrix13UpstreamClassify:
    """§七 #13 行：同 status 不同措辞 ⇒ 分类一致；无 status 时文本 fallback
    能兜住（阳性对照）；两层都不认识 ⇒ fail-loud 留样本。"""

    def test_same_status_different_wording_same_class(self):
        from hiveweave.llm.retry import (
            PermanentError,
            RetryableError,
            classify_http_error,
        )

        for status, expect in ((429, RetryableError), (400, PermanentError)):
            for body in (
                "standard english error body",
                "Corps d'erreur en français : le serveur a échoué",
                "Cuerpo de error en español",
                "",
            ):
                got = classify_http_error(status, body)
                assert isinstance(got, expect), f"{status} {body!r} → {type(got).__name__}"

    def test_text_fallback_catches_when_status_missing(self):
        """阳性对照（§七 #13）：关掉 status 判定（status=None）⇒ 文本 fallback
        能兜住已知英文瞬态措辞 —— 两层都在。"""
        from hiveweave.llm.retry import RetryableError, classify_http_error

        got = classify_http_error(None, "internal server error, please retry")
        assert isinstance(got, RetryableError)

    def test_unknown_wording_is_fail_loud_not_silent(self):
        """两层都不认识（无 status + 法文陌生文案）⇒ 分类保守 + 样本留痕，
        且样本归属到调用 agent（不给会挂错人）。"""
        from hiveweave.llm.retry import PermanentError, classify_http_error
        from hiveweave.llm.unknown_error_samples import (
            clear_unknown_samples,
            recent_unknown_samples,
        )

        clear_unknown_samples()
        got = classify_http_error(
            None,
            "Échec mystérieux du fournisseur : xyzzy",
            provider="probe",
            agent_id="matrix-agent-13",
        )
        assert isinstance(got, PermanentError)
        samples = recent_unknown_samples("matrix-agent-13")
        assert len(samples) == 1
        assert samples[0]["source"] == "classify_http_error"
        assert samples[0]["status"] is None
        assert "xyzzy" in samples[0]["body_preview"]
        # 归属过滤：别人的缓冲里没有
        assert recent_unknown_samples("someone-else") == []


# ── #14：验收覆盖只认 id + 凭证/waiver ──────────────────────────────


class TestMatrix14AcceptanceIds:
    """§七 #14 行：覆盖判定 = 「声明了条目 id 且锚在平台核验物上」；
    抄原文不算、换措辞不影响、``N/A—任意理由`` 不放行（要走平台 waiver）。"""

    CRITERIA = [{"id": "A1", "text": "管理员登录后可见仪表盘"}]

    def test_copying_original_text_is_not_coverage(self):
        evidence = {"summary": "管理员登录后可见仪表盘 —— 已实现并自测通过"}
        gaps = uncovered_acceptance_items(self.CRITERIA, evidence, verified_ids=set())
        assert any("A1" in g for g in gaps)

    def test_declared_id_with_verified_attestation_is_coverage(self):
        """换措辞（条目原文改写）+ 声明 id + 凭证已核验 ⇒ 覆盖，与措辞无关。"""
        for text in ("管理员登录后可见仪表盘", "user sees dashboard after login"):
            criteria = [{"id": "A1", "text": text}]
            evidence = {
                "acceptance_coverage": {"A1": {"attestation_ids": ["att-9"]}}
            }
            gaps = uncovered_acceptance_items(
                criteria, evidence, verified_ids={"att-9"}
            )
            assert gaps == [], (text, gaps)

    def test_declared_id_without_verification_is_fail_closed(self):
        evidence = {"acceptance_coverage": {"A1": {"attestation_ids": ["att-x"]}}}
        gaps = uncovered_acceptance_items(self.CRITERIA, evidence, verified_ids=set())
        assert any("A1" in g for g in gaps)  # 自述凭证未核验 ⇒ 不算

    def test_na_reason_without_platform_waiver_does_not_pass(self):
        """``not_applicable_reason`` 写什么字都不放行 —— 只有平台 waiver 行
        （``waived=True``）能把 N/A 变成覆盖；理由措辞/语言无关。"""
        na = lambda reason: {
            "acceptance_coverage": {"A1": {"not_applicable_reason": reason}}
        }
        for reason in ("N/A—随便什么", "no tests needed", "Pas de tests"):
            gaps = uncovered_acceptance_items(
                self.CRITERIA, na(reason), verified_ids=set(), waived=False
            )
            assert any("A1" in g for g in gaps), reason
        # 平台已豁免 ⇒ 理由非空即覆盖（理由只供人读，不参与判定）
        for reason in ("仓库无测试基建", "aucune infrastructure de test"):
            gaps = uncovered_acceptance_items(
                self.CRITERIA, na(reason), verified_ids=set(), waived=True
            )
            assert gaps == [], reason
        # 豁免了但理由为空 ⇒ 仍不算（非空理由是声明成立的最低要求）
        gaps = uncovered_acceptance_items(
            self.CRITERIA, na("  "), verified_ids=set(), waived=True
        )
        assert any("A1" in g for g in gaps)

    def test_unknown_item_id_is_reported_not_silently_ignored(self):
        evidence = {"acceptance_coverage": {"ZZ": {"attestation_ids": ["att-9"]}}}
        gaps = uncovered_acceptance_items(self.CRITERIA, evidence, verified_ids={"att-9"})
        assert any("ZZ" in g for g in gaps)


# ── #15：失败归因布尔位优先 ─────────────────────────────────────────


class TestMatrix15FactBits:
    """§七 #15 行：归因读状态位，错误文案措辞/语言不影响；位缺失 ⇒ None
    （unknown，不猜）。文本签名表是第二层 fallback（已知英文措辞仍兜得住）。"""

    def test_bits_drive_attribution_regardless_of_wording(self):
        for err in (
            "spawn failed: process could not start",
            "Échec du lancement : le processus n'a pas démarré",
            "错误：进程未能启动",
        ):
            got = fact_from_bits({"runner_failed": True, "error": err})
            assert got == "runner_failed", err

    def test_command_bit_maps_to_command_failed(self):
        assert (
            fact_from_bits({"command_failed": True, "error": "whatever"})
            == "command_failed"
        )

    def test_missing_bits_are_unknown_not_guessed(self):
        """位缺失（含显式 False）⇒ None（不猜）；外语措辞也驱动不了归因。"""
        for source in (
            {"error": "Échec du lancement : le processus n'a pas démarré"},
            {"error": "spawn failed"},
            {"runner_failed": False, "error": "spawn failed"},
            {},
        ):
            assert fact_from_bits(source) is None, source

    def test_text_signature_layer_is_fallback_positive_control(self):
        """阳性对照：位缺失时，已知英文签名仍能兜底（第二层在）。"""
        assert classify_error_text("spawn failed to start") == "runner_failed"
        assert classify_error_text("Port 4000 is reserved") == "bad_args"

    def test_foreign_wording_cannot_drive_text_layer_either(self):
        """外语措辞在第二层同样不命中 ⇒ 不会被文案骗出归因（落 unknown）。"""
        assert classify_error_text("Échec du lancement du processus") is None
        assert classify_error_text("端口被保留无法使用") is None


# ── #16：(b) 档「绕过配方」零残留 ───────────────────────────────────


def test_matrix16_no_bypass_recipes_in_prompts():
    """§三 #16 收敛后的验收口径：**逐条分档，(b) 档零残留**。

    - (a) 接口契约（``test_run:``/``contractWaived`` 等合法取值）与
      (c) 入站协议词汇（``[TURN EXIT BLOCKED]`` 等）**按设计保留**——
      删了 agent 就无法提交/无法认出唤醒信号，净亏；
    - (b) 绕过配方（告诉模型门禁在读什么文本）必须为零。本用例列的即
      §三 #16 表里 (b) 档的全部成员；新发现 (b) 档条目时**加进这张表**。
    """
    b_class_recipes = [
        "只拦 high",          # 「code_audit 只拦 high 级问题」
        "只拦high",
        "ISSUES high=0",      # 内部凭证标记名
        "medium/low 会放行",  # 「纯 medium/low 会放行」的诱导（不能只写
                              # "medium/low"——context.py 合法枚举说明会误报）
    ]
    prompts_dir = _SRC_ROOT / "prompts"
    scanned = 0
    # 扫描面 = 全部文本形态的提示词文件（.py 模块 + .md/.txt 模板）——
    # 只扫 .py 会让未来的非代码模板静默逃出扫描面
    template_paths = [
        p for p in prompts_dir.rglob("*")
        if p.is_file() and p.suffix in (".py", ".md", ".txt", ".j2", ".jinja2")
    ]
    for path in template_paths:
        scanned += 1
        text = path.read_text(encoding="utf-8")
        for recipe in b_class_recipes:
            assert recipe not in text, f"{path.name} 残留绕过配方：{recipe!r}"
    assert scanned >= 4, f"prompts 扫描面异常（只扫到 {scanned} 个文件）"
    # 阳性对照：扫描真的在读文件内容 —— (a) 档合法取值应能扫到
    identity = (prompts_dir / "identity.py").read_text(encoding="utf-8")
    assert "test_run:" in identity


# ── P-1：棘轮「新增被看见」 ─────────────────────────────────────────


def _load_ratchet():
    spec = importlib.util.spec_from_file_location(
        "ratchet_module_for_matrix",
        Path(__file__).with_name("test_text_judge_ratchet.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_matrix_p1_ratchet_sees_new_table():
    """§七 P-1 行：新增一张后缀型文本表 ⇒ 尺子必须报出（阳性对照）；
    存量当前零增长（阴性）；非后缀名按设计不在本尺扫描面（已知缺口，
    docstring 写明，不算回归）。"""
    ratchet = _load_ratchet()

    def tier_of_snippet(snippet: str) -> str | None:
        tree = ast.parse(snippet)
        target_info = ratchet._module_assign_targets(tree.body[0])
        assert target_info is not None, snippet
        names, value = target_info
        return ratchet._tier_of(names[0], value)

    assert tier_of_snippet("_FOO_NEEDLES = ('rm -rf ',)") == "table"
    assert tier_of_snippet("_BAR_PATTERNS = ['a', 'b']") == "table"
    # 已知缺口（不是回归）：非后缀名 / 局部变量不在扫描面 ——棘轮管「新增
    # 后缀型表被看见」，不管蓄意换名（见 ratchet docstring 的「可绕」清单）
    assert tier_of_snippet("_FOO_THINGS = ('rm -rf ',)") is None

    growth = ratchet._growth("table")
    assert not growth, f"存量出现新增文本表：{growth}"
    by_tier = ratchet.baseline().get("_by_tier") or {}
    assert by_tier.get("table"), "棘轮基线 table 档为空 ⇒ 尺子已失效"
