"""#18 复现装置：受限令牌下**孙进程 spawn** 的三态边界 + `node --test` 产品结论。

## 为什么单独一个文件
`#18` 的原探针是**一次性脚本、已删**（fixqueue 自陈），导致"想复核"时没有可复跑的尺子。
本文件把它落成**常驻测试**（对齐 DSH 的做法：他们把这条边界 **pin 成测试**，
见 `packages/sandbox/sandbox-windows-acl/tests/runner.spec.ts:296-309`）。

## DSH 的结论（2026-09-12，`b35a3b29eb` + 上述测试注释 —— 我们 2026-09-16 读到的）
- `CreatePipe(SD=null)`（**匿名**管道）消费的是**令牌默认 DACL** ⇒ 只要该 DACL 带上
  restricting-SID ACE，匿名管道可用，`inherit`/`ignore` 的 spawn 成功；
- **libuv 的 pipe-stdio 用的是命名管道**，其默认安全描述符来自 **Win32 层用户态默认 SD 模板**
  （KernelBase 构造：owner/SYSTEM/Admins 全权、**Everyone/ANONYMOUS 只读**）—— **不是**令牌
  默认 DACL ⇒ 客户端开写时**没有任何 restricting SID 被授权** ⇒ `ERROR_ACCESS_DENIED`，
  **以 spawn EPERM 呈现**；
- 这是 WRITE_RESTRICTED 令牌 POC 记录的「**no output redirection**」边界 ⇒
  **piped capture 不可能工作**，DSH 把它 pin 成 DENIED（不是"没修"，是"判为不可修"）。

## 本文件断言什么
1. **三态**（我们的探针形态与 DSH 逐点可比）：`ignore` 必须 OK；`pipe` 按**固有限制**
   pin 成 DENIED（**若它某天变 OK ⇒ 边界变了，必须回来更新 #18**）；
2. **产品层**：`node --test` 在沙箱内到底能不能用（它内部对每个测试文件起子进程并
   **用管道收 stdout** ⇒ 按上面的机制应当**不可用**）。这条给的是"#18 到底能不能达成
   'JS 标准测试入口可用'"的**结论性证据**，不是猜测。

⚠ 仅 Windows + 需要 `node`；二者缺一即整模块 skip。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from hiveweave.config import settings
from hiveweave.services.acl_sandbox.service import spawn_confined

from tests.test_acl_sandbox_win32 import _ensure_subject_ace  # noqa: F401

pytestmark = [
    pytest.mark.win32,
    pytest.mark.skipif(
        not sys.platform.startswith("win"), reason="需要 Windows 受限令牌"
    ),
    pytest.mark.skipif(
        shutil.which("node") is None, reason="探针需要 node（JS 孙进程场景）"
    ),
]

#: 三态探针 —— **与 DSH 的同名测试逐点同形**（便于跨仓对照结论）。
_THREE_MODE_JS = """\
const { spawnSync } = require('child_process')
const t = (name, opts) => {
  const s = spawnSync(process.execPath, ['-e', '0'], { encoding: 'utf8', ...opts })
  console.log(name + ':' + (s.status === 0 ? 'OK' : 'DENIED'))
}
t('inherit', { stdio: 'inherit' })
t('ignore', { stdio: 'ignore' })
t('pipe', { stdio: 'pipe' })
"""

#: `node --test` 的最小用例（它内部会 **spawn 子进程 + 管道收 TAP**）。
_NODE_TEST_JS = """\
const { test } = require('node:test')
const assert = require('node:assert')
test('tiny', () => { assert.strictEqual(1, 1) })
"""


@pytest.fixture(scope="session", autouse=True)
def _reap_sandbox_workers():
    """会话级回收（F4，审计实测）：沙箱的 drain/watcher 是**非守护线程**，不回收会在
    pytest 退出时报 `non-daemon thread(s) still alive ... 'acl-drain_0'`，在把本文件
    与别的模块同跑的编排里有挂住进程的风险。与同族三文件一致：
    `test_acl_sandbox_win32.py` / `test_acl_sandbox_basetemp.py` / `test_acl_sandbox_extra_writable.py`。
    """
    yield
    from hiveweave.services.acl_sandbox.service import shutdown_runner
    from hiveweave.services.acl_sandbox.spawn import stop_watcher

    stop_watcher()
    shutdown_runner()


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    """带真实主体 ACE 的 workspace（受限令牌经它要能过 pass-1）。"""
    d = tmp_path / "ws"
    d.mkdir(parents=True)
    _ensure_subject_ace(d)
    return d


async def _run(ws: Path, command: str, *, timeout_s: float = 90):
    return await spawn_confined(
        command=command,
        workdir=str(ws),
        workspace_path=str(ws),
        agent_id="A001",
        project_id="p18-probe",
        entry="bash",
        timeout_s=timeout_s,
    )


def _modes(stdout: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in (stdout or "").splitlines():
        k, _, v = ln.strip().partition(":")
        if k in ("inherit", "ignore", "pipe"):
            out[k] = v.strip()
    return out


@pytest.mark.asyncio
async def test_grandchild_spawn_three_modes_in_confined_token(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 三态边界（状态判据）：`ignore` 必须可用；`pipe` 是**固有边界**，pin 成 DENIED。

    为什么必须 pin 住"不能用"这一半：否则日后有人看到 `pipe` 失败会以为"没修好"，
    再去找一个不存在的修法（DSH 已经给出机制：命名管道的默认 SD 来自 KernelBase
    用户态模板，**与令牌默认 DACL 无关** ⇒ 注入 DACL 治不了它）。
    若哪天这里变成 OK ⇒ **边界变了**，回来更新 #18 的定性与验收。
    """
    # Q5（审计）：用 monkeypatch 而不是裸赋值 —— 别让全局在用例间泄漏。
    monkeypatch.setattr(settings, "acl_sandbox", True)
    (ws / "probe.js").write_text(_THREE_MODE_JS, encoding="utf-8", newline="\n")

    r = await _run(ws, "node probe.js")
    assert r is not None, "受限 spawn 本身没起来（那是另一类故障）"
    got = _modes(r.get("stdout") or "")
    assert got, f"探针没输出三态结果：{r}"

    assert got.get("ignore") == "OK", (
        "不继承句柄的 spawn 必须可用（这一半是能修的：走令牌默认 DACL）"
        f"：{got}"
    )
    assert got.get("pipe") == "DENIED", (
        "piped capture 不再是 DENIED ⇒ **WRITE_RESTRICTED 的固有限制变了**："
        f"回来更新 #18（把'另一半不可修'的定性改掉）：{got}"
    )


@pytest.mark.asyncio
async def test_node_test_runner_requires_isolation_off_in_confined_token(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 产品级结论（**2026-09-16 实测，改变了 #18 的定性**）：

    - 默认 `node --test` 在受限沙箱内**不可用**：它以 `spawn EPERM` 收场，堆栈落在
      `ChildProcess.spawn` → `FileTest.run`（因为它对每个测试文件**起子进程 + 用管道收 TAP**，
      正好命中三态里那半"命名管道不给 restricting SID 授权"）；
    - **但把隔离关掉就能跑通**：`--experimental-test-isolation=none`（node 22 的写法，
      23+ 改名为 `--test-isolation=none`）让测试文件**跑在同一进程**里 ⇒ 不起子进程、不用管道
      ⇒ **沙箱内可用**。实测 v22.22.2：默认 `fail 1` → 关隔离 `# pass 1 / # fail 0`。

    ⇒ `#18` 的处置因此**不再是"沙箱要改"，而是"给 JS 测试入口指一条可用姿势 + 撞墙时
    识别成沙箱边界"**（沙箱侧真的改不动：命名管道的默认 SD 来自 KernelBase 用户态模板，
    与令牌默认 DACL 无关 —— DSH 的机制解释与我们三态实测一致）。

    ⚠ 本条同时是**回归探针**：哪个开关能用是 **node 版本相关**的，所以断言写成
    "**至少一个等价开关可用**"并报告是哪个；若某天全都不可用 ⇒ 转红，回来处理。
    """
    # Q5（审计）：用 monkeypatch 而不是裸赋值 —— 别让全局在用例间泄漏。
    monkeypatch.setattr(settings, "acl_sandbox", True)
    (ws / "tiny.test.js").write_text(_NODE_TEST_JS, encoding="utf-8", newline="\n")

    # 先把"默认形态不可用"钉住（否则下面那条"关隔离可用"可能只是碰巧）
    base = await _run(ws, "node --test tiny.test.js")
    assert base is not None, "受限 spawn 本身没起来"
    base_text = f"{base.get('stdout') or ''}\n{base.get('stderr') or ''}"
    # Q8（审计）：两条都要满足 —— 否则"退出 0 但没跑出 # pass 1"这种含糊态会空过
    assert base.get("exit_code") != 0, (
        "默认 `node --test` 在沙箱内**退出码是 0** ⇒ 边界变了，回来更新 #18"
        f"（把'另一半不可修'的定性改掉）：\n{base_text[:400]}"
    )
    assert "EPERM" in base_text or "operation not permitted" in base_text, (
        "默认形态不是以 EPERM 收场 ⇒ 换机制了？回来更新 #18 与三态那条："
        f"\n{base_text[:400]}"
    )

    # 再找**能用**的隔离开关
    passed: list[str] = []
    for flag in ("--experimental-test-isolation=none", "--test-isolation=none"):
        r = await _run(ws, f"node --test {flag} tiny.test.js")
        text = f"{(r or {}).get('stdout') or ''}\n{(r or {}).get('stderr') or ''}"
        if (r or {}).get("exit_code") == 0 and "# pass 1" in text:
            passed.append(flag)
    assert passed, (
        "没有任何隔离开关能让 `node --test` 在受限沙箱内通过 —— 那 #18 的'有出路'结论失效，"
        "需要重新定性与给替代方案（例如改由未受限路径跑 JS 测试）"
    )
