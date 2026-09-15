"""平台侧 git 信任锚 —— gitdir / common dir 由**平台自己算出并钉住**。

## 为什么需要它（全部一手实测，2026-09-15）

平台的 git 跑在**不受限**进程里（`git_worktree/*` 的 `_git`），而 agent 能写自己
worktree 的 `.git` 指针、也能写 git 自己会去读的 gitdir 内文件 ⇒ agent 可以让平台
的 git 去读**它写好的 config**，于是 `filter.<n>.clean` 这类动态键名照样执行
（`GIT_CONFIG_*` 静态清单覆盖不到）。三条实测路径：

1. **指针重定向**：agent 删掉 `<wt>/.git`（删这一侧走 pass-1 的 user ACE，ACL 挡不住）
   → 建**同名目录** + 自写 `config/HEAD/objects/refs` ⇒ 平台在该 worktree 的
   `git add -A` 执行其载荷（审计 A1 三段实跑）。
2. **只钉 `--git-dir` 不够**：agent 把 `<gitdir>` 删成**独立仓**（删 `commondir`、
   自写 `config`）后，`git --git-dir=<gitdir> config --get probe.standalone` 实测读到
   `FROM_AGENT_GITDIR`。
3. **钉 `--git-dir` + `GIT_COMMON_DIR` 才封死**：同一现场实测**读不到**；且
   `add`/`commit`/`config`/`log` 全部照常（钉住的路径都由平台派生）。

⇒ 本模块把「git 去哪找 gitdir / common dir」从**读 agent 可写文件**改成
**平台派生**：不读 `<wt>/.git` 的内容、不读 `commondir`，**无 TOCTOU**。

## 边界（不要读成「git 侧全封了」）

- `<gitdir>/config.worktree` 仍会被 git 读（平台自己开了
  `extensions.worktreeConfig`）⇒ 属另一条（fixqueue #2 残余 R3）。**审计 2026-09-15
  实测**：该载体不仅能挂 `filter.<n>.clean`，还能挂 `merge.<n>.driver` —— 且
  `conflict_predict` 那条 `git merge-tree` 走的**不是** `_git`（无锚），是本族的
  另一个落点（已在本批接上锚）。修法提示（实测）：`git config extensions.worktreeConfig
  false`（**repo config**）后该载体 `IGNORED`、载荷 `BLOCKED`；而命令行
  `-c extensions.worktreeConfig=false` **无效**（实测仍执行）。
- **同路径替换挡不住**（残余 R4）：本模块把 gitdir/common 钉在**平台派生路径**上，
  但那条路径**可预测** ⇒ agent 删掉 `<proj>/.git` 再 `git init` 造一个**同样路径**的
  新仓，平台读到的就是它的 config。**审计 2026-09-15 实测**：① 项目根边界形态下
  `rmdir /s /q <proj>\\.git` → 重建 → 平台 `_git` 读到 `FROM_AGENT_COMMON`（`ok=True`）；
  ② worktree cwd 形态**删除同样成功**，只是重建被本模块拒（fail-closed = DoS）。
  ⇒ 需「身份指纹」层（项目创建时把 `<proj>/.git` 的 `(st_dev, st_ino)` 记进
  `project_meta`，每次 `_git` 校验）；本模块**不**声称挡住它。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import structlog

from .constants import WORKTREE_DIR

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GitAnchor:
    """平台派生的 git 目录锚点（全部为 realpath）。"""

    kind: str          # "main"（主树，含 cwd 在主树子目录的形态）| "worktree"
    project_root: str
    git_dir: str
    common_dir: str

    @property
    def args(self) -> list[str]:
        return [f"--git-dir={self.git_dir}"]

    @property
    def env(self) -> dict[str, str]:
        """钉住 gitdir 与 common dir —— 后者是「删 commondir 自建独立仓」的解。"""
        return {"GIT_DIR": self.git_dir, "GIT_COMMON_DIR": self.common_dir}


class AnchorRefusal(Exception):
    """**可证的**篡改形态 ⇒ 必须拒绝跑 git（fail-closed）。

    只用于「平台布局下不该出现」的形态，例如平台 worktree 的 `.git` 变成了目录。
    """


class AnchorUnderivable(Exception):
    """派生不出项目根，但**没有**篡改证据（如 worktree 与主仓是兄弟目录）。

    这种布局平台自建 worktree 不会产生，但**合法**（实测 `test_checkpoint_dirty_contract`
    的夹具就是），也有人会拿一个 linked worktree 目录当工作区。⇒ **不拒绝**，
    退回「不钉锚」（= 修复前行为）并 loud 记一笔；要覆盖这种布局，调用方传
    `project_root`（多数调用点手里就有 `workspace_path`）。
    """


def _nearest_dot_git(path: str) -> tuple[str, str] | None:
    """向上找最近的 `.git`（文件或目录）→ (它所在的目录, `.git` 路径)。"""
    cur = os.path.realpath(path)
    while True:
        dot = os.path.join(cur, ".git")
        if os.path.exists(dot):
            return cur, dot
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _project_root_above(path: str) -> str | None:
    """从 `path` 的**父目录**向上找第一个「`.git` 是目录」的祖先（= 项目根）。

    只认目录形态：worktree 自己的 `.git` 是**文件**，不能当项目根。
    """
    cur = os.path.dirname(os.path.realpath(path))
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _is_platform_worktree_root(path: str) -> bool:
    """`path` 是否落在平台的 worktree 落点（`<proj>/.hiveweave/worktrees/<name>`）。

    这是**平台自有布局**判据（常量 `WORKTREE_DIR`），不是文案判据：`.hiveweave`
    对受限令牌是 PROTECTED（无能力 ACE），agent 造不出这个路径 ⇒ 可信。
    用途见下面 `resolve_anchor`：平台 worktree 的 `.git` **必须是指针文件**，
    若某次发现它是**目录**，说明现场被换成了「自建仓」（审计 A1 实测的跳板形态）。
    """
    parts = os.path.realpath(path).replace("\\", "/").rstrip("/").split("/")
    want = WORKTREE_DIR.strip("/").split("/")     # [".hiveweave", "worktrees"]
    return len(parts) > len(want) and parts[-len(want) - 1:-1] == want


def _platform_worktree_root_of(path: str) -> str | None:
    """`path`（或其祖先）是否是平台 worktree 根 ⇒ 返回那个 worktree 根。

    ⚠ 必须查**祖先**，不能只查本层（审计 2026-09-15 ①-2）：
    agent 可以在自己的 worktree 里建**嵌套仓**（`<wt>/x/.git` 目录）；若只看本层，
    平台会把 `<wt>/x` 当成「主树」并去读它自建的 config。
    """
    cur = os.path.realpath(path)
    while True:
        if _is_platform_worktree_root(cur):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def resolve_anchor(cwd: str, project_root: str | None = None) -> GitAnchor | None:
    """派生锚点。`None` = 这里不是 git 仓（如 `git init` 调用前）——不是错误。

    **先上溯定「这是哪棵树」**，再按树形派生 gitdir（审计 2026-09-15 ①-2：
    早先版本只看 `cwd` 自身，于是 `cwd` 是 worktree **子目录**时会被误拒；
    而不给 `project_root` 时又会把 worktree 内的**嵌套仓**误当主树）：

      - 上溯命中 `.git` **文件** ⇒ worktree（`project_root` 有则用它派生，
        否则继续上溯找「`.git` 为目录」的祖先）；
      - 上溯命中 `.git` **目录** ⇒ 主树，**但** holder 若位于平台 worktree 布局之下
        （`<…>/.hiveweave/worktrees/<name>/…`）⇒ 拒绝（自建/嵌套仓）。

    只做**结构**判断（`exists`/`isdir`/`basename`/平台布局），**不读任何文件内容**。
    可疑形态 ⇒ `AnchorRefusal`（fail-closed）；仅派生不出 ⇒ `AnchorUnderivable`。
    """
    real_cwd = os.path.realpath(cwd)
    project = os.path.realpath(project_root) if project_root else None

    found = _nearest_dot_git(real_cwd)
    if found is None:
        return None                      # 没有仓：交给 git 自己（init 前等）
    holder, dot = found

    if os.path.isdir(dot):
        # 主树形态 —— 但 holder 在平台 worktree 布局之下 = 被替换/自建，拒绝
        wt_root = _platform_worktree_root_of(holder)
        if wt_root is not None:
            raise AnchorRefusal(
                f"{holder} 位于平台 worktree（{wt_root}）之下，但它的 .git 是**目录**"
                f"（平台 worktree 只应有指向平台 gitdir 的指针文件）⇒ 现场已被替换，"
                f"拒绝跑 git")
        git_dir = dot
        if project is not None and os.path.normcase(holder) != os.path.normcase(project):
            # 给了项目根却不是它 —— 说明 cwd 落在别的仓里（嵌套/兄弟仓），
            # 这时按「主树」跑等于放弃钉锚；明确拒绝而不是静默放行。
            raise AnchorRefusal(
                f"cwd 上溯到的仓主是 {holder}，与给定 project_root {project} 不一致"
                f" ⇒ 无法安全派生信任锚，拒绝跑 git")
        if not os.path.isdir(git_dir):    # pragma: no cover - 结构上不可能
            return None
        return GitAnchor("main", holder, git_dir, git_dir)

    # `.git` 是文件 ⇒ worktree gitdir 指针形态
    if project is None:
        project = _project_root_above(holder)
    if project is None:
        raise AnchorUnderivable(
            f"worktree {holder} 的 .git 是 gitdir 指针，但向上找不到项目根"
            f"（.git 为目录的祖先）⇒ 本布局无法派生信任锚；"
            f"要覆盖请调用方传 project_root")
    git_dir = os.path.join(project, ".git", "worktrees", os.path.basename(holder))
    if not os.path.isdir(git_dir):
        raise AnchorRefusal(
            f"worktree {holder} 期望的 gitdir 不存在：{git_dir}"
            f"（可能已被删除/替换）⇒ 拒绝跑 git")
    return GitAnchor("worktree", project, git_dir, os.path.join(project, ".git"))


def anchor_for_git(cwd: str, project_root: str | None = None
                   ) -> tuple[GitAnchor | None, str | None]:
    """给 `_git` 用：返回 (锚点, 拒绝原因)。拒因非空 ⇒ **不要跑 git**。

    三档（**不要**把「派生不出」与「可证篡改」混成一档）：

    1. 派生出锚点 ⇒ 钉住（`--git-dir` + `GIT_COMMON_DIR`）；
    2. **可证篡改**（平台 worktree 的 `.git` 变成目录、期望 gitdir 消失、调用方给了
       project_root 而 cwd 既非项目根也不是指针 worktree）⇒ `AnchorRefusal` ⇒
       **拒绝执行**（loud）。静默回落「让 git 自己找」正是被改写指针利用的那条路；
    3. 只是**派生不出**（兄弟目录布局等）⇒ 退回不钉锚（= 修复前行为）+ 一条
       warning —— 拒绝会把合法布局一起打死（实测：`test_checkpoint_dirty_contract`
       的夹具就是这种布局）。
    """
    try:
        return resolve_anchor(cwd, project_root), None
    except AnchorRefusal as exc:
        log.warning("git_anchor_refused", cwd=str(cwd), reason=str(exc))
        return None, f"git 拒绝执行（信任锚校验失败）：{exc}"
    except AnchorUnderivable as exc:
        log.warning("git_anchor_underivable", cwd=str(cwd), reason=str(exc))
        return None, None
