"""Per-agent git 身份 —— 用 `-c user.name/-c user.email` 注入，不再用 worktree-local config。

## 为什么换（fixqueue #2 残余 R3，2026-09-15 实测）

原实现：`git config extensions.worktreeConfig true` + `git config --worktree user.name …`。
代价是 git 从此会去读两个 **agent 可写面内**的文件：

  - `<project>/.git/config.worktree`
  - `<project>/.git/worktrees/<id>/config.worktree`

而 worktree gitdir **必须**对 agent 可写（`index`/`index.lock`/`COMMIT_EDITMSG` 在那里），
所以「封住文件」这条路留不住（删/建走 pass-1，ACL 挡不住）；更要命的是
`filter.<n>.clean` / `merge.<n>.driver` 是**动态键名**，`GIT_CONFIG_*` 静态清单覆盖不到
⇒ 实测：agent 在 `<gitdir>/config.worktree` 写 `merge.<n>.driver` 后，平台的
`git merge-tree` **执行了它**（审计 2026-09-15 ①-1 实跑）。

## 现在的形态

身份走**命令行注入**（与本仓 maintenance 提交既有的 `-c user.name=…` 同一形态），
repo 级 `extensions.worktreeConfig` 一律置 **false**（自愈存量项目）⇒ 上面两个载体
整体失效、不依赖 ACL。

⚠ **关扩展必须写 repo config**：命令行 `-c extensions.worktreeConfig=false` **实测无效**
（git 在读 local config 时就决定了要不要包含 worktree config；实测仍会执行载荷）。
"""

from __future__ import annotations


import structlog

log = structlog.get_logger(__name__)

# 平台兜底身份：无花名/脏数据时用，也是平台自有提交（initial/maintenance/merge）的身份。
PLATFORM_NAME = "HiveWeave Agent"
PLATFORM_EMAIL = "hiveweave@agent.local"
PLATFORM_IDENTITY_ARGS = ["-c", f"user.name={PLATFORM_NAME}",
                          "-c", f"user.email={PLATFORM_EMAIL}"]


async def agent_identity_args(short_id: str | None) -> list[str]:
    """某 agent 的 `-c` 身份参数（拿不到花名时退 ``<平台名> <short_id>``，仍可区分）。

    只读 DB（`OrgService.resolve_agent`），失败/脏数据一律回退 —— 身份注入**不得**
    影响 checkpoint/merge 主流程（与旧实现的 fail-quiet 同哲学）。
    """
    sid = (short_id or "").strip()
    if not sid:
        return list(PLATFORM_IDENTITY_ARGS)
    name: str | None = None
    try:
        from hiveweave.services.org import OrgService

        agent = await OrgService().resolve_agent(sid)
        raw = (agent or {}).get("name")
        name = raw if isinstance(raw, str) and raw.strip() else None
    except Exception:  # 身份只是展示属性，绝不能拖垮主流程
        name = None
    git_name = name or f"{PLATFORM_NAME} {sid}"
    return ["-c", f"user.name={git_name}",
            "-c", f"user.email={sid}@agents.hiveweave.local"]


async def retire_worktree_config(project_root: str) -> bool:
    """把 repo 级 `extensions.worktreeConfig` 置 false（幂等自愈），返回是否成功。

    **存量项目也要治**：老版本在 worktree 创建时把它打开过，光改新代码不会关掉它
    ⇒ 这个函数被两个地方调用：① worktree 创建流程；② 受限命令的 standing-grants
    阶段（每进程每项目一次，见 `acl_sandbox.service`）—— 后者保证「只要项目跑起来
    就被收口」，不必依赖新建 worktree。

    ⚠ 必须写 **repo config**：命令行 `-c extensions.worktreeConfig=false` 实测**无效**
    （git 在读 local config 时就决定要不要包含 worktree config）。
    """
    try:
        # #19（AST 网扫出来的**第二处清单外落点**）：原先是
        # `asyncio.to_thread(hidden_run, ["git", …])` —— 助手**作为值传递**，
        # 早期的网（只认 `Call.func`）看不见它。这里改走**接信任锚**的 `_git`：
        # 本函数的调用方（`acl_sandbox.service` 的 standing-grants 阶段、
        # `service_create` 的 worktree 创建）把它当 **R3 收口的 fail-closed 前提**
        # ⇒ 它读的 gitdir 必须是平台派生的那一个，否则"R3 已退休"的结论可被
        # 同路径替换（R4）误导。
        # ⚠ **先读后写**：`.git/config` 在「锁死档」下连平台主体都没有 DELETE ⇒
        # 一旦已经是 false 就**不要**再写（否则每次重启都会因 lock+rename 失败刷 warning）。
        from hiveweave.services.git_worktree.git_cmd import _git

        _ok_p, probe_out = await _git(
            ["config", "--get", "extensions.worktreeConfig"],
            project_root, project_root=project_root,
        )
        if (probe_out or "").strip().lower() == "false":
            return True
        _ok_w, _out_w = await _git(
            ["config", "extensions.worktreeConfig", "false"],
            project_root, project_root=project_root,
        )
        proc_ok = _ok_w
    except Exception as exc:  # 仓库/目录不可用等 —— 不阻断调用方
        log.warning("git_identity.retire_worktree_config_error",
                    project_root=project_root, error=str(exc)[:200])
        return False
    if not proc_ok:
        log.warning("git_identity.retire_worktree_config_failed",
                    project_root=project_root,
                    out=(_out_w or "")[:200])
        return False
    return True

