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

## ⚠ 2026-09-16 补全：锚必须**同时钉住工作树**（#21 成因链第 3 环）

上面三档只钉了 gitdir 与 common dir ⇒ **git 把 cwd 当成工作树根**。实测复现
（58 现场形态）：在 `<proj>/.vite`（任意普通目录）里 `--git-dir=<proj>/.git
rev-parse --show-toplevel` 输出 `.vite`、`ls-files -d` 报出**全部** tracked 文件；
于是 `git add -A` 把整棵树记成"删除"，而 `commit` 因 gitdir 锚定到 `<proj>/.git`
**直接写进 `main`** ⇒ 主干清空（TEST_DSH_58/59 实测 3 次）。

⇒ 锚改为同时钉 `--work-tree=` / `GIT_WORK_TREE`（见 `GitAnchor.args/env`）。
这是**补全**（仓库 + 公共目录 + 工作树），**不是**回退本模块的任何一档。

## 边界（不要读成「git 侧全封了」）

- `<gitdir>/config.worktree` 仍会被 git 读（平台自己开了
  `extensions.worktreeConfig`）⇒ 属另一条（fixqueue #2 残余 R3）。**审计 2026-09-15
  实测**：该载体不仅能挂 `filter.<n>.clean`，还能挂 `merge.<n>.driver` —— 且
  `conflict_predict` 那条 `git merge-tree` 走的**不是** `_git`（无锚），是本族的
  另一个落点（已在本批接上锚）。修法提示（实测）：`git config extensions.worktreeConfig
  false`（**repo config**）后该载体 `IGNORED`、载荷 `BLOCKED`；而命令行
  `-c extensions.worktreeConfig=false` **无效**（实测仍执行）。
- **同路径替换（R4）由「身份指纹」这一层挡**（2026-09-15 落地）：信任锚钉的是**路径**，
  路径可预测 ⇒ 光钉路径挡不住「删掉该路径上的 gitdir、再在同路径造一个自己的仓」。
  故另记 `(st_dev, st_ino)`（NTFS 上 = 卷序列号 + 文件索引；**同路径重建必变**，
  平台重写 `config` **不变** —— 都实测过）到 `get_data_root()/git-anchor/`
  （**在项目 ACL 授权面之外** ⇒ agent 读不到也写不到），每次跑 git 前比对；不符 ⇒ 拒绝。
  **创建点主动记录**（`service_create` 的 `git init` / `worktree add` 之后），TOFU 只兜
  「本机制上线前就存在的项目」。
  ⚠ **运维逃生门**：若 `.git` 是**用户**（而非 agent）删掉重建的，平台会持续拒绝 ——
  删掉 `<data_root>/git-anchor/<hash>.id` 让平台下次重新采用即可；而「用户删掉 `.git`
  让平台重建」这条恢复路本身是通的（`ensure_git_repo` 的 `git init` 会覆盖记录），
  只有「`.git` 仍在（agent 造的）」才会持续拒绝。
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
    work_tree: str

    @property
    def args(self) -> list[str]:
        """⚠ **必须连工作树一起钉**（#21 成因链第 3 环，2026-09-16 补全）。

        只给 `--git-dir` 时，git 会把 **cwd** 当成工作树根 —— 实测复现（58 现场形态）：
        在 `TEST_DSH_58/.vite`（任意普通目录）里
        `--git-dir=<root>/.git rev-parse --show-toplevel` → 输出 `.vite`，
        同目录 `ls-files -d` → **10 个**（正好＝主干 tracked 文件数）。
        于是任何一次 `git add -A` 都会把整棵树记成"删除"，而 `commit` 因为
        gitdir 锚定到 `<main>/.git` 而**直接写进 `main`** ⇒ 主干被清空
        （TEST_DSH_58/59 实测 3 次）。

        这不是新增约束、更不是回退锚：锚本就该同时钉住
        「**仓库 + 公共目录 + 工作树**」三者 —— 这是**补全**。
        """
        return [f"--git-dir={self.git_dir}", f"--work-tree={self.work_tree}"]

    @property
    def env(self) -> dict[str, str]:
        """钉住 gitdir / 公共目录 / 工作树（三者缺一，另两处就被 cwd 或文件说了算）。"""
        return {
            "GIT_DIR": self.git_dir,
            "GIT_COMMON_DIR": self.common_dir,
            "GIT_WORK_TREE": self.work_tree,
        }


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
        # 主树：工作树 = holder（含 `.git` 目录的那层 = 项目根）。注意 holder 是
        # `_nearest_dot_git` **上溯**到的，所以 cwd 在主树子目录时这里仍然正确
        # —— 而"不给 --work-tree"时 git 会拿 cwd 当工作树根（那是错的）。
        return GitAnchor("main", holder, git_dir, git_dir, holder)

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
    # worktree：工作树 = holder（含 `.git` **指针文件**的那层 = worktree 根）。
    return GitAnchor("worktree", project, git_dir,
                     os.path.join(project, ".git"), holder)


def anchor_for_git(cwd: str, project_root: str | None = None
                   ) -> tuple[GitAnchor | None, str | None]:
    """给 `_git` 用：返回 (锚点, 拒绝原因)。拒因非空 ⇒ **不要跑 git**。

    三档（**不要**把「派生不出」与「可证篡改」混成一档）：

    1. 派生出锚点 ⇒ 钉住（`--git-dir` + `GIT_COMMON_DIR`）；
    2. **可证篡改**（平台 worktree 的 `.git` 变成目录、期望 gitdir 消失、调用方给了
       project_root 而 cwd 既非项目根也不是指针 worktree、**身份指纹不符**）⇒
       `AnchorRefusal` ⇒ **拒绝执行**（loud）。静默回落「让 git 自己找」正是被改写
       指针利用的那条路；
    3. 只是**派生不出**（兄弟目录布局等）⇒ 退回不钉锚（= 修复前行为）+ 一条
       warning —— 拒绝会把合法布局一起打死（实测：`test_checkpoint_dirty_contract`
       的夹具就是这种布局）。
    """
    try:
        anchor = resolve_anchor(cwd, project_root)
    except AnchorRefusal as exc:
        log.warning("git_anchor_refused", cwd=str(cwd), reason=str(exc))
        return None, f"git 拒绝执行（信任锚校验失败）：{exc}"
    except AnchorUnderivable as exc:
        log.warning("git_anchor_underivable", cwd=str(cwd), reason=str(exc))
        return None, None
    if anchor is not None:
        mismatch = verify_gitdir_identity(anchor.git_dir)
        if mismatch is not None:
            log.warning("git_anchor_identity_mismatch", cwd=str(cwd),
                        git_dir=anchor.git_dir, reason=mismatch)
            return None, f"git 拒绝执行（gitdir 身份指纹不符）：{mismatch}"
    return anchor, None


# ── gitdir 身份指纹（fixqueue #2 残余 R4：**同路径替换**）────────────────
# 信任锚钉的是**路径**（`--git-dir`/`GIT_COMMON_DIR`），挡不住「把那个路径上的目录
# 删掉、再在同路径造一个自己的仓」—— 实测（审计 2026-09-15）：项目根边界形态
# `rmdir /s /q <proj>\.git` 后 `git init`，平台 `_git` 就读到了 agent 的 config。
# ⇒ 再钉一层**身份**：记下 gitdir 的 (st_dev, st_ino)（NTFS 上=卷序列号+文件索引），
# 每次跑 git 前 stat 比对；不符 ⇒ 拒绝。指纹落在**平台数据根**（`get_data_root()`，
# 与 `empty-git-hooks` 同处，在项目 ACL 授权面之外 ⇒ agent 写不到、也伪造不了）。
#
# 边界（写清楚，别读成万无一失）：**首次见到某 gitdir 时是 TOFU**（记录当下身份）。
# 平台新建项目/新建 worktree 会在**创建点**主动记录（早于任何 agent 命令），TOFU
# 只覆盖「本机制上线前就已存在的项目」。
_IDENTITY_CACHE: dict[str, tuple[int, int] | None] = {}


def _identity_dir() -> str:
    from pathlib import Path

    from hiveweave.config import get_data_root

    d = Path(get_data_root()) / "git-anchor"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _identity_file(git_dir: str) -> str:
    import hashlib

    key = hashlib.sha256(os.path.realpath(git_dir).encode("utf-8")).hexdigest()
    return os.path.join(_identity_dir(), f"{key[:32]}.id")


def record_gitdir_identity(git_dir: str) -> tuple[int, int] | None:
    """记录 gitdir 当前身份（创建点调用；幂等）。返回 (dev, ino) 或 None。"""
    try:
        st = os.stat(git_dir)
    except OSError:
        return None
    ident = (int(st.st_dev), int(st.st_ino))
    try:
        with open(_identity_file(git_dir), "w", encoding="utf-8") as fh:
            fh.write(f"{ident[0]}:{ident[1]}\n")
    except OSError as exc:  # 记录不下就**不**缓存 ⇒ 下次重试（不静默当作已记）
        log.warning("git_anchor_identity_record_failed", git_dir=git_dir,
                    error=str(exc)[:120])
        return None
    _IDENTITY_CACHE[os.path.realpath(git_dir)] = ident
    return ident


def read_gitdir_identity(git_dir: str) -> tuple[int, int] | None:
    """读**已记录**的 gitdir 身份（诊断/测试用；没有记录 ⇒ None）。"""
    try:
        with open(_identity_file(git_dir), encoding="utf-8") as fh:
            dev_s, _, ino_s = (fh.read().strip() or ":").partition(":")
        return (int(dev_s), int(ino_s))
    except (OSError, ValueError):
        return None


def verify_gitdir_identity(git_dir: str) -> str | None:
    """校验 gitdir 身份。`None` = 通过（或首次 TOFU 记录成功）；否则返回拒因。"""
    key = os.path.realpath(git_dir)
    try:
        st = os.stat(git_dir)
    except OSError:
        return None  # 路径不在：交给上游的「不存在」分支处理（不在这里断言）
    live = (int(st.st_dev), int(st.st_ino))
    expected = _IDENTITY_CACHE.get(key)
    if expected is None:
        try:
            with open(_identity_file(git_dir), encoding="utf-8") as fh:
                dev_s, _, ino_s = (fh.read().strip() or ":").partition(":")
            expected = (int(dev_s), int(ino_s))
        except (OSError, ValueError):
            record_gitdir_identity(git_dir)  # TOFU：首次见到 ⇒ 记当下身份
            log.info("git_anchor_identity_adopted", git_dir=git_dir)
            return None
    if expected == live:
        _IDENTITY_CACHE[key] = expected
        return None
    return (f"{git_dir} 的身份与平台记录不符（记录 {expected[0]}:{expected[1]} / "
            f"现实 {live[0]}:{live[1]}）—— 该 gitdir 已被删除并重建（同路径替换）")
