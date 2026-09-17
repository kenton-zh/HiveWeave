"""F6 守卫 spec：scripts/verify_commit_license.py 的正/负 fixture 与验收。

设计稿 §5（DSH ``verify-config-source-ownership.spec.ts`` 形态 +
GitHub 正/负 fixture 形态）：

- **合成第 3 个漏网点**（不能只跑真实仓库，§11.11 对照 4）：临时树里放
  一个「用错谓词」的假模块 ⇒ 守卫**必须报出**且文案完整；
- **用对谓词的假模块** ⇒ 守卫**沉默**（``can_idle`` 是唯一许可源）；
- **负 fixture**：``return None``（trigger.py R4 例外形态）与
  ``return True``（health_supervisor「许可不唤醒」形态）**不算**违规
  —— 守卫判据 (c) 的排除面必须可执行地钉死（§11.8：既证「该红的红」，
  也证「该绿的不绿」）；
- **真实仓库验收**（§11.10 #6）：修后 ``src/hiveweave`` 命中集必须为空
  （修前的两个真实漏网点已切闭式；多于此 = 假阳性，须收窄判据）。
  修前形态的阳性对照以 ``git show HEAD:…`` 快照在
  ``test_real_repo_pre_fix_snapshots_are_caught`` 钉住（金样本）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GUARD = _REPO_ROOT / "scripts" / "verify_commit_license.py"
_SRC_ROOT = _REPO_ROOT / "apps" / "hiveweave-py" / "src" / "hiveweave"


def _load_guard():
    spec = importlib.util.spec_from_file_location("verify_commit_license", _GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 阳性 fixture：合成第 3 个漏网点（(c)-1 直接形态——poll.py 修前同构）
BAD_DIRECT = '''
from hiveweave.services.task import TaskService


async def snapshot(agent_id: str, project_id: str) -> str:
    obligations = await TaskService().get_actionable_obligations(
        project_id, agent_id
    )
    if not obligations:
        return "No tasks left — you are free to finish this turn."
    return "still busy"
'''

# 阳性 fixture：(c)-2 尾部拼接形态——hint 修前同构（多源 AND + 尾 return）
BAD_TAIL_CONCAT = '''
from hiveweave.services.task import TaskService


async def hint(agent_id: str, project_id: str) -> str:
    asks: list = []
    obligations = await TaskService().get_actionable_obligations(
        project_id, agent_id
    )
    if not asks and not obligations:
        body = "nothing left " + "just commit"
        return f"EXIT: {body}"
    body = "items pending"
    return f"EXIT: {body}"
'''

# 阳性 fixture：(c)-2 **真形态**（审计 MEDIUM-1）：body 内拼接赋值给局部
# 变量、函数体最后一条语句 return 该变量（Name，非 JoinedStr）。
BAD_TAIL_NAME = '''
from hiveweave.services.task import TaskService


async def finish_hint(agent_id: str, project_id: str) -> str:
    obligations = await TaskService().get_actionable_obligations(
        project_id, agent_id
    )
    if not obligations:
        msg = "no obligations -- " + "safe to finish"
        return msg
    msg = "items pending"
    return msg
'''

# 负 fixture：正当消费点形态（守卫必须沉默）
GOOD_CAN_IDLE = '''
from hiveweave.services.tasks.obligations import ObligationLedger


async def maybe_wake(agent_id: str, project_id: str) -> str:
    svc = ObligationLedger()
    if await svc.can_idle(project_id, agent_id):
        return "no open work — safe to stand down"
    return "still hold open work"
'''

# 负 fixture：trigger.py 的 R4 例外形态（return None 不算违规输出）
NEUTRAL_RETURN_NONE = '''
from hiveweave.services.task import TaskService


async def duty_check(agent_id: str, project_id: str):
    obligations = await TaskService().get_actionable_obligations(
        project_id, agent_id
    )
    if not obligations:
        return None
    return obligations
'''

# 负 fixture：health_supervisor「许可不唤醒」形态（return True/布尔）
NEUTRAL_RETURN_BOOL = '''
from hiveweave.services.task import TaskService


async def supervisor_ok(agent_id: str, project_id: str) -> bool:
    obligations = await TaskService().get_actionable_obligations(
        project_id, agent_id
    )
    if not obligations:
        return True
    return False
'''

# 负 fixture：orelse（"有活"分支）分支不参与判定；body 侧纯常量赋值
# （非拼接）+ 尾部 return 不是 (c)-2 的形态
NEUTRAL_ORELSE_STRING = '''
from hiveweave.services.task import TaskService


async def hint(agent_id: str, project_id: str) -> str:
    obligations = await TaskService().get_actionable_obligations(
        project_id, agent_id
    )
    if not obligations:
        items = "none actionable"
    else:
        items = "you have items — act on them"
    return f"EXIT: {items}"
'''


def _make_tree(tmp: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp


def test_guard_reports_synthetic_third_leak_site():
    """★ §11.11 对照 4：合成第 3 个漏网点必须被报出（阳性对照）。

    改坏动作 = 在任何模块里引入 BAD_DIRECT / BAD_TAIL_CONCAT 形态 ⇒
    真实仓库运行时本用例的「真实仓库干净」断言会转红；本用例另在
    临时树里独立验证守卫报出 + 文案点名 can_idle。
    """
    guard = _load_guard()
    with tempfile.TemporaryDirectory() as td:
        root = _make_tree(
            Path(td),
            {"pkg/bad_direct.py": BAD_DIRECT, "pkg/bad_tail.py": BAD_TAIL_CONCAT},
        )
        violations = guard.collect_commit_license_violations(root)
        assert len(violations) == 2, violations
        joined = "\n".join(violations)
        assert "bad_direct.py" in joined and "can_idle" in joined, joined
        assert "bad_tail.py" in joined, joined


def test_guard_catches_true_tail_name_form():
    """审计 MEDIUM-1：(c)-2 的**真形态**必须有独立正例 —— body 内拼接
    赋值给局部变量 + 函数尾 ``return <该变量>``。

    改坏动作：把 (c)-2 的 ``_tail_concat_names`` + 尾 return 检测删掉 ⇒
    本测试转红（此前 BAD_TAIL_CONCAT 实际经 (c)-1 命中，(c)-2 零覆盖）。
    """
    guard = _load_guard()
    with tempfile.TemporaryDirectory() as td:
        root = _make_tree(Path(td), {"pkg/tail_name.py": BAD_TAIL_NAME})
        violations = guard.collect_commit_license_violations(root)
        assert violations, "(c)-2 真形态未被报出"
        assert "tail_name.py" in "\n".join(violations)


def test_guard_silent_on_can_idle_and_legit_forms():
    """负 fixture 全绿：can_idle 形态 / return None / return True / orelse。"""
    guard = _load_guard()
    with tempfile.TemporaryDirectory() as td:
        root = _make_tree(
            Path(td),
            {
                "pkg/good.py": GOOD_CAN_IDLE,
                "pkg/ret_none.py": NEUTRAL_RETURN_NONE,
                "pkg/ret_bool.py": NEUTRAL_RETURN_BOOL,
                "pkg/orelse.py": NEUTRAL_ORELSE_STRING,
            },
        )
        assert guard.collect_commit_license_violations(root) == []


def test_guard_cli_exit_codes():
    """CLI 契约：违规 exit 1（附文案），干净 exit 0。"""
    with tempfile.TemporaryDirectory() as td_bad, tempfile.TemporaryDirectory() as td_good:
        _make_tree(Path(td_bad), {"pkg/bad.py": BAD_DIRECT})
        _make_tree(Path(td_good), {"pkg/good.py": GOOD_CAN_IDLE})
        r_bad = subprocess.run(
            [sys.executable, str(_GUARD), td_bad],
            capture_output=True, text=True, timeout=120,
        )
        assert r_bad.returncode == 1
        assert "can_idle" in r_bad.stdout
        r_good = subprocess.run(
            [sys.executable, str(_GUARD), td_good],
            capture_output=True, text=True, timeout=120,
        )
        assert r_good.returncode == 0


def test_real_repo_is_clean_after_f6_fix():
    """★ §11.10 #6（修后口径）：真实 src/hiveweave 命中集必须为空。

    修前的两个真实漏网点（``poll.py`` 的 safe-to-commit_turn 与
    ``turn_exit.build_exit_contract_hint`` 的「无未完成义务」）已在 F6
    本体切到闭式源；此后再出现任何命中 = 新增违规（或判据过宽须收窄）。
    """
    guard = _load_guard()
    violations = guard.collect_commit_license_violations(_SRC_ROOT)
    assert violations == [], (
        "commit-license 守卫在真实仓库命中（白名单谓词产生收尾许可）：\n"
        + "\n".join(violations)
    )


def test_real_repo_pre_fix_snapshots_are_caught():
    """修前金样本必须被守卫报出（阳性对照的存档形态）。

    金样本 = 修前两个真实漏网点的**最小同构片段**（非整文件）：poll 的
    ``safe to commit_turn(waiting)`` 与 hint 的「无未完成义务…仅需提交
    commit_turn 收尾」。若守卫判据被改窄到抓不到它们 ⇒ 本用例转红。
    """
    guard = _load_guard()
    pre_poll = '''
from hiveweave.services.task import TaskService


async def _build_obligations_snapshot(agent_id: str, project_id: str) -> str:
    obligations = await TaskService().get_actionable_obligations(
        project_id, agent_id
    )
    if not obligations:
        return "\\nCurrent obligations: none — safe to commit_turn(waiting)."
    return "obligations exist"
'''
    pre_hint = '''
from hiveweave.services.task import TaskService


async def build_exit_contract_hint(agent_id: str, project_id: str) -> str:
    asks: list = []
    obligations = await TaskService().get_actionable_obligations(
        project_id, agent_id
    )
    if not asks and not obligations:
        return (
            "【本轮出口条件】无未回复 ask / 未完成义务 / 未提交 worktree："
            "仅需提交 commit_turn 收尾。"
        )
    return "items"
'''
    with tempfile.TemporaryDirectory() as td:
        root = _make_tree(
            Path(td), {"p/poll.py": pre_poll, "p/hint.py": pre_hint}
        )
        violations = guard.collect_commit_license_violations(root)
        assert len(violations) == 2, violations
        assert all("can_idle" in v for v in violations)
