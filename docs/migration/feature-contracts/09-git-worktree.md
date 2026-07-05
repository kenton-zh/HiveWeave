# 功能契约 09：Git Worktree

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 09 |
| 模块名称 | Git Worktree |
| Elixir 源码 | `services/git_worktree.ex` |
| TS 参考源码 | `packages/core/src/git-worktree-service.ts` |
| OpenCode 参考源码 | 无 |
| 状态 | 草稿 |

## 功能概述

为每个叶子 Agent 分配隔离的 git worktree（`.hiveweave/worktrees/<shortId>/`，分支 `hw/<shortId>/<task-slug>`），由 Coordinator 全权管理生命周期。支持创建、轻量 checkpoint 提交、fast-forward 合并到 main、回滚、删除。Elixir 和 TS 实现高度一致。

## 接口契约

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `ensure_git_repo` | `(workspace_path)` | `{:ok, bool}` | 确保 git 仓库初始化，master→main 重命名 |
| `create` | `(workspace_path, short_id, task_name, base_branch?)` | `{:ok, %{path, branch}}` | 创建 worktree + 分支 |
| `checkpoint` | `(workspace_path, short_id, message)` | `{:ok, %{hash, count}}` | 轻量提交，无变更则跳过 |
| `merge` | `(workspace_path, short_id, task_name, target_branch?)` | `{:ok, %{merged: true, hash}}` | fast-forward 合并到 main |
| `rollback` | `(workspace_path, short_id, commit_hash?)` | `{:ok, %{hash, message}}` | 回退到指定 checkpoint |
| `remove` | `(workspace_path, short_id, task_name?)` | `{:ok, %{removed: true}}` | 删除 worktree |
| `list` | `(workspace_path)` | `{:ok, [entry]}` | 列出所有 worktree |
| `status` | `(workspace_path, short_id)` | `{:ok, status}` | worktree 状态 |
| `get_worktree_path` | `(workspace_path, short_id)` | `path \| nil` | 获取 worktree 路径 |

## 核心流程

### 创建

```
1. ensure_git_repo（如未初始化）
2. ensure_git_identity（user.email=hiveweave@agent.local, user.name=HiveWeave Agent）
3. slug = slugify(task_name)
4. branch = "hw/<short_id>/<slug>"
5. worktree_path = ".hiveweave/worktrees/<short_id>/"
6. base_branch 三级回退（Enum.find_value 取第一个成功）：
   a. git worktree add <path> -b <branch> origin/<base>
   b. git worktree add <path> -b <branch> <base>
   c. git worktree add <path> -b <branch> master
7. 全部失败 → {:error, "Failed to create worktree"}
```

> **RECONCILE — master/main 不是矛盾**：源码同时存在两处看似冲突的逻辑，实为不同场景：
> - `ensure_git_repo` 对**新初始化**的仓库执行 `git branch -m master main`（重命名为 main，失败则忽略，可能已是 main/trunk）；
> - `create` 的三级回退最后兜底到 `master`，处理**既有遗留仓库**未被重命名、仍以 master 为默认分支的情况。
> 默认 `base_branch` 参数为 `"main"`，回退到 `master` 仅在 origin/main、本地 main 均不存在时生效。两者互补，非矛盾。

#### slugify 规则（源码 `slugify/1`）

```
1. 将 空格/正斜杠/反斜杠 [\s/\\]+ 替换为 "-"
2. 删除除 [a-zA-Z0-9_-] 和 CJK(\u4e00-\u9fff) 外的所有字符
3. 截断至 40 字符
4. 去除首尾连字符 ^-+|-+$
5. 结果为空 → 返回 "task"
```

> **RECONCILE — 并发创建竞态（有效权衡）**：源码 `create` 无锁。理论上两个 coordinator
> 同时为同一 `short_id` 创建 worktree 会竞态。但实际架构中 worktree 工具是 **coordinator-only**，
> 且每个叶子 agent 的 `short_id` 唯一、由 coordinator 串行为下属分配，并发概率极低。修复成本
> （引入文件锁/GenServer 串行化）高于接受成本。Python 迁移可保留无锁实现，或在协调层用
> `asyncio.Lock` 串行化 create 调用作为低成本加固。

### Checkpoint

```
1. git status --porcelain（检查有无变更）
2. 无变更 → 返回当前 HEAD hash + count=0
3. 有变更：
   a. git add -A
   b. git commit -m "checkpoint: <message>"
   c. 查 7 天内 checkpoint 数量
   d. 返回 hash + count
```

### Merge

> **RECONCILE — 修正两处事实错误**：契约原写 `git merge --ff-only` 且"不删除 worktree"。
> 源码 `merge/4` 实际用 `git merge "<branch>" --no-edit`（**非 ff-only**，会产生合并提交），
> 且成功后**立即调用 `remove/3` 删除 worktree + 分支**。已按源码修正。

```
1. git checkout "<target_branch>"（默认 main）
2. git merge "<worktree_branch>" --no-edit
3. 冲突 → git merge --abort + 报错（worktree 保留，见下）
4. 成功 → git rev-parse --short HEAD 取 hash
5. 成功 → 调用 remove(workspace_path, short_id, task_name) 删除 worktree + 分支
6. 返回 {:ok, %{merged: true, hash: hash}}
```

#### merge 冲突时的状态（源码行为）

- 冲突时立即 `git merge --abort`，主仓库回到 `target_branch` 的干净状态
- **worktree 目录与 agent 分支保留不动**（`remove` 仅在成功路径调用），agent 可修 bug 后重试 merge，或调 rollback 回退
- 返回 `{:error, "Merge conflict for <short_id> into <target_branch>. Resolve manually or rollback."}`
- 不会自动解决冲突，不自动 rollback

### Rollback

```
1. 无 commit_hash → 查找最近一个 "checkpoint:" 前缀的 commit（git log --format=%H --grep="checkpoint:" -1）
2. 找不到 → 报错 "No checkpoints found for <short_id>."
3. git reset --hard "<commit_hash>"
4. 返回 hash + message
```

> **RECONCILE — rollback 前不自动 checkpoint（安全建议）**：源码 `rollback/3` 直接
> `git reset --hard`，**不自动 checkpoint** 当前未提交工作。若有未 checkpoint 的改动会被
> 永久丢弃。这是真实的数据丢失风险。Python 迁移建议：rollback 前先调 `checkpoint` 保存当前
> 状态（哪怕作为 "pre-rollback-snapshot"），给用户一次反悔机会。源码不这么做是因为 coordinator
> 调用 rollback 前通常已 checkpoint，但契约应显式记录此安全约束。

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| WORKTREE_DIR | `.hiveweave/worktrees` | — |
| CHECKPOINT_PREFIX | `checkpoint:` | — |
| git 超时 | `30_000` ms | — |
| slug 最大长度 | `40` 字符 | — |
| checkpoint 查询窗口 | `7 天` | — |
| list/status 上限 | `20` 条 | — |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| E2 | 中文 slug 处理是设计决策 | 保留行为，用等效 Unicode 正则 |
| — | Elixir master→main 重命名，TS 无 | 保留重命名（仅新仓初始化时） |
| — | Elixir ensure_git_identity，TS 无 | 保留（确保 git commit 不失败） |
| — | rollback 无 commit 时找最近 checkpoint，找不到则报错 | 保留行为 |
| — | rollback 前不自动 checkpoint，未提交改动会丢失 | **安全建议**：rollback 前先 checkpoint 存档 |
| — | merge 用 `--no-edit`（非 ff-only），会产生合并提交 | 保留行为；契约已修正（原误写 ff-only） |
| — | merge 成功后自动 remove worktree+分支 | 保留行为；契约已修正（原误写"不删除"） |
| — | merge 冲突不自动解决，abort 后 worktree 保留 | 保留行为 |
| — | create 无锁，理论并发竞态 | 有效权衡：coordinator 串行分配，可选 `asyncio.Lock` 加固 |
| — | slugify 删除非 [alnum/_/-/CJK] 字符，空串→"task" | 保留行为，用等效正则 |
| — | Coordinator-only 权限 | 在工具权限矩阵中强制 |

## 验收标准

- [ ] create 在 `.hiveweave/worktrees/<shortId>/` 创建 worktree
- [ ] 分支命名 `hw/<shortId>/<task-slug>`
- [ ] slugify：空格/斜杠转 `-`，删除非 [alnum/_/-/CJK]，截断 40，去首尾 `-`，空串→"task"
- [ ] base_branch 三级回退：origin/<base> → <base> → master
- [ ] ensure_git_repo 确保 git 初始化 + master→main（仅新仓）
- [ ] ensure_git_identity 设置 user.email/name
- [ ] checkpoint 无变更时不创建空 commit
- [ ] checkpoint commit message 带 "checkpoint:" 前缀
- [ ] merge 使用 `--no-edit`（非 ff-only）
- [ ] merge 成功后自动删除 worktree + 分支
- [ ] merge 冲突时 abort、报错，worktree 保留
- [ ] rollback 默认找最近 checkpoint commit
- [ ] rollback 找不到 checkpoint 时报错
- [ ] rollback 前建议先 checkpoint（安全约束）
- [ ] remove 删除 worktree 目录
- [ ] list 返回所有 worktree
- [ ] status 返回 worktree 的 git 状态
- [ ] git 命令 30s 超时
- [ ] 仅 Coordinator 可调用

## Python 实现建议

- 用 `subprocess` 或 `asyncio.create_subprocess_exec` 调 git 命令
- slugify 用正则 `[\u4e00-\u9fff\w]` 保留 CJK，删除其他非法字符，空串→"task"
- Windows 路径用 `pathlib.Path` 处理
- merge 用 `--no-edit`（对齐源码，非 ff-only），成功后自动 remove worktree+分支
- merge 冲突检测：`git merge --no-edit` 返回非 0 即冲突，立即 `git merge --abort`
- rollback 前先调 checkpoint 存档当前状态（安全加固，源码未做）
- create 可选 `asyncio.Lock` 串行化（低成本加固并发竞态）
