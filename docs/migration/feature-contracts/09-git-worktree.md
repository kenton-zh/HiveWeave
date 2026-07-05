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
3. slug = slugify(task_name)（保留 CJK，空格/斜杠转 -，max 40 字符）
4. branch = "hw/<short_id>/<slug>"
5. worktree_path = ".hiveweave/worktrees/<short_id>/"
6. base_branch 三级回退：origin/<base> → <base> → master
7. git worktree add -b <branch> <worktree_path> <base_commit>
```

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

```
1. git checkout <target_branch>（默认 main）
2. git merge --ff-only <worktree_branch>
3. 冲突 → git merge --abort + 报错
4. 成功 → 返回 merge hash
5. 不删除 worktree（由 remove 单独处理）
```

### Rollback

```
1. 无 commit_hash → 查找最近一个 "checkpoint:" 前缀的 commit
2. 找不到 → 报错
3. git reset --hard <commit_hash>
4. 返回 hash + message
```

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
| — | Elixir master→main 重命名，TS 无 | 保留重命名 |
| — | Elixir ensure_git_identity，TS 无 | 保留（确保 git commit 不失败） |
| — | rollback 无 commit 时找最近 checkpoint，找不到则报错 | 保留行为 |
| — | merge 冲突不自动解决 | 保留行为 |
| — | Coordinator-only 权限 | 在工具权限矩阵中强制 |

## 验收标准

- [ ] create 在 `.hiveweave/worktrees/<shortId>/` 创建 worktree
- [ ] 分支命名 `hw/<shortId>/<task-slug>`
- [ ] slug 保留 CJK 字符，空格/斜杠转 `-`，max 40 字符
- [ ] base_branch 三级回退：origin/<base> → <base> → master
- [ ] ensure_git_repo 确保 git 初始化 + master→main
- [ ] ensure_git_identity 设置 user.email/name
- [ ] checkpoint 无变更时不创建空 commit
- [ ] checkpoint commit message 带 "checkpoint:" 前缀
- [ ] merge 使用 fast-forward only
- [ ] merge 冲突时 abort 并报错
- [ ] rollback 默认找最近 checkpoint commit
- [ ] rollback 找不到 checkpoint 时报错
- [ ] remove 删除 worktree 目录
- [ ] list 返回所有 worktree
- [ ] status 返回 worktree 的 git 状态
- [ ] git 命令 30s 超时
- [ ] 仅 Coordinator 可调用

## Python 实现建议

- 用 `subprocess` 或 `asyncio.create_subprocess_exec` 调 git 命令
- slugify 用正则 `[\u4e00-\u9fff\w]` 保留 CJK
- Windows 路径用 `pathlib.Path` 处理
- merge 冲突检测：`git merge --ff-only` 返回非 0 即冲突
