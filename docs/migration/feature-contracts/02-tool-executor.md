# 功能契约 02：工具执行器

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约，不引用实现代码。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 02 |
| 模块名称 | 工具执行器 |
| Elixir 源码 | `apps/hiveweave/lib/hiveweave/tool_executor.ex`（3500+ 行，73 个 dispatch 分支） |
| TS 参考源码 | `packages/core/src/tool-executor.ts` + `packages/core/src/tools/` |
| OpenCode 参考源码 | `D:\PC_AI\Project\opencode\packages\opencode\src\tool\` + `packages\core\src\session\runner\tool.ts` |
| 状态 | 草稿 |

## 功能概述

按 agent 的权限类型（coordinator/executor）和角色（CEO/HR/qa/test_engineer/auditor 等）过滤可用工具集，对 LLM 返回的 tool_call 执行权限检查后分发到具体实现。每个工具返回人类可读的字符串结果供 LLM 消费。包含自毁命令拦截、路径沙箱、大输出截断与临时文件存储等安全机制。

## 接口契约

### 输入（Consumes）

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| permission_type | agent 对象 | `"coordinator"` / `"executor"` | 决定工具集基线 |
| role | agent 对象 | `"ceo"` / `"hr"` / `"qa"` / `"test_engineer"` / `"code_reviewer"` / ... | 细分工具集 |
| tool_name | LLM tool_call | string（可能带 `hiveweave__` 前缀） | 需剥离前缀后 dispatch |
| tool_input | LLM tool_call | JSON object | 工具参数 |
| workspace_path | 项目配置 | filesystem path | 沙箱根目录 |
| agent | OrgService | agent 对象 | 用于权限评估、worktree 定位、记忆写入 |

### 输出（Produces）

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| 工具列表 | Streamer（传给 LLM） | `[{type:"function", function:{name, description, parameters}}]` | OpenAI function-calling 格式 |
| 执行结果 | ConversationStore（传回 LLM） | string | 人类可读文本，错误也以字符串返回 `"Error: ..."` |
| 工具输出文件 | 文件系统 `.hiveweave/tool_outputs/` | 文件 | 大输出存盘，7 天后清理 |

### 副作用

| 副作用 | 触发条件 | 目标 | 说明 |
|---|---|---|---|
| 文件读写 | read_file/write_file/edit_file/bash/apply_patch 等 | 文件系统 | 必须在 workspace_path 或 worktree 内 |
| 子进程执行 | bash/run_command | OS 进程 | 120s 超时，stderr 合并到 stdout |
| DB 写入 | write_memory/write_work_log/hire_agent/dispatch_task 等 | per-project DB | 各工具的持久化操作 |
| 权限请求 | `:ask` 权限 | ApprovalService | 异步请求用户审批 |
| 广播事件 | send_message/dispatch_task/approve_work 等 | PubSub | 通知其他 agent 或前端 |

## 核心流程

```
1. get_tools(permission_type, role):
   - coordinator + "ceo" → management + git_worktree + charter_full + binding + admin + readonly_file + core + extra
   - coordinator + "hr" → hire/transfer/dismiss + binding + admin + charter_readonly + readonly_file + core + extra
   - coordinator + 其他 → management + git_worktree + admin + readonly_file + core + extra + self_skill
   - executor + "qa" → full_file + qa_review + core + self_skill
   - executor + "test_engineer" → bash + readonly_file + core + self_skill（无 write）
   - executor + auditor 角色 → bash + readonly_file + qa_review + core + self_skill（无 write）
   - executor + 其他 → full_file + executor_specific + core + self_skill

2. execute(agent, tool_name, input, workspace_path):
   a. 剥离 "hiveweave__" 前缀
   b. 权限评估：Permission.evaluate(agent, name, input) → :allow / :deny / :ask
   c. :deny → 返回 "Permission denied"
   d. :ask → 检查 saved_rules 是否匹配 → 匹配则执行；否则请求审批（120s 超时）
   e. :allow → 执行
   f. dispatch(name, input, workspace_path, agent) → 分发到具体工具实现
   g. maybe_save_large_output(result) → 截断大输出
   h. 异常捕获 → 返回 "Error: ..."（不抛异常给上层）
```

## 工具集矩阵

### 按角色分配

| 工具类别 | CEO | HR | Coordinator(其他) | Executor(通用) | QA | Test Engineer | Auditor |
|---|---|---|---|---|---|---|---|
| **core_tools**（send_message, read_roster, check_agent_status, write_memory, fetch_url, get_project_time, get_real_time, set_alarm, question, todowrite, websearch, read_goals, mcp_list_tools, mcp_call, run_command） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **readonly_file_tools**（read_file, list_files, grep, glob, search_files） | ✅ | ✅ | ✅ | — | — | ✅ | ✅ |
| **full_file_tools**（bash, read_file, list_files, grep, glob, apply_patch, write_file, edit_file, delete_file, move_file, create_directory, delete_directory, search_files） | — | — | — | ✅ | ✅ | — | — |
| **management_tools**（read_work_logs, review_code, approve_work, reject_work, list_subordinates, view_org_chart） | ✅ | — | ✅ | — | — | — | — |
| **git_worktree_tools**（create, checkpoint, merge, rollback, remove, list, status） | ✅ | — | ✅ | — | — | — | — |
| **binding_tools**（bind_skill, unbind_skill, list_available_skills, get_skill_detail, read_skill, bind_mcp, unbind_mcp, list_available_mcp） | ✅ | ✅ | — | — | — | — | — |
| **self_skill_tools**（bind_skill, list_available_skills） | — | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| **hire_tools**（hire_agent, list_agent_templates, transfer_agent, dismiss_agent, update_roster） | — | ✅ | — | — | — | — | — |
| **admin_tools**（mcp_configure, list_models, set_default_model） | ✅ | ✅ | ✅ | — | — | — | — |
| **charter_full**（save_charter, read_charter） | ✅ | — | — | — | — | — | — |
| **charter_readonly**（read_charter） | — | ✅ | — | — | — | — | — |
| **qa_review_tools**（run_code_review, run_security_audit, run_tests, run_perf_audit, run_full_review） | — | — | — | — | ✅ | — | ✅ |
| **executor_specific**（write_work_log, read_project_memory） | ✅(部分) | ✅(部分) | ✅(部分) | ✅ | ✅ | ✅ | ✅ |
| **extra**（read_project_memory, update_goals, list_all_agents, trigger_integration） | 部分 | list_all_agents | update_goals, trigger_integration | — | — | — | — |

### 工具完整清单（73 个 dispatch 分支）

**文件操作类**：bash, run_command, read_file, list_files, grep, glob, search_files, apply_patch, write_file, edit_file, delete_file, move_file, create_directory, delete_directory

**通信类**：send_message, dispatch_task, message_superior, report_completion, question

**组织管理类**：hire_agent, list_agent_templates, transfer_agent, dismiss_agent, update_roster, read_roster, list_subordinates, view_org_chart, list_all_agents, check_agent_status

**工作流类**：read_work_logs, write_work_log, approve_work, reject_work, review_code, trigger_integration

**记忆类**：write_memory, read_project_memory

**目标类**：read_goals, update_goals

**时间类**：get_project_time, get_real_time, set_alarm

**技能/MCP 类**：bind_skill, unbind_skill, list_available_skills, get_skill_detail, read_skill, bind_mcp, unbind_mcp, list_available_mcp, mcp_list_tools, mcp_call, mcp_configure

**模型管理类**：list_models, set_default_model

**Git worktree 类**：git_worktree_create, git_worktree_checkpoint, git_worktree_merge, git_worktree_rollback, git_worktree_remove, git_worktree_list, git_worktree_status

**QA 类**：run_code_review, run_security_audit, run_tests, run_perf_audit, run_full_review

**Charter 类**：save_charter, read_charter

**其他**：websearch, fetch_url, todowrite

## 安全机制

### 自毁命令拦截（bash 专用）

| 模式 | 处理 |
|---|---|
| `rm -rf /` | 阻止："system-level destructive command" |
| `format [a-z]:` | 阻止 |
| `diskpart` | 阻止 |
| `shutdown` / `reboot` / `poweroff` / `halt` | 阻止 |

> `run_command` 工具**不执行**自毁检查——它是 bash 的底层逃生舱，由 coordinator 使用。

### 路径沙箱

| 规则 | 说明 |
|---|---|
| workdir 必须在 workspace_path 内 | `Path.expand(cwd)` 必须以 `Path.expand(workspace_path)` 为前缀 |
| 文件操作必须在 effective_workspace 内 | read_file/write_file 等同样校验 |
| worktree agent 的 effective_workspace 是 `.hiveweave/worktrees/<shortId>/` | 非 workspace 根目录 |

### 大输出截断

| 条件 | 动作 |
|---|---|
| 输出 > 2000 行 **或** > 50KB | 保存全文到 `.hiveweave/tool_outputs/<agent>_<tool>_<timestamp>.txt`，保留 7 天 |
| 截断后返回 | 前 20 行 + `... (N lines omitted, full output at <path>)` + 后 5 行 |

## 错误处理

| 错误场景 | 处理方式 | 说明 |
|---|---|---|
| 工具名未知 | 返回 `"Unknown tool: <name>"` | dispatch 兜底分支 |
| 参数缺失 | 返回 `"Error: <param> is required"` | 每个工具自行校验 |
| 权限拒绝 | 返回 `"Permission denied: <name> is blocked"` | 不执行 |
| 审批超时 | 返回 `"Permission request timed out (120s)"` | 用户不在 |
| 工具执行异常 | 捕获并返回 `"Error: <exception>"` | 不抛异常给 Streamer |
| bash 超时 | 返回 `"Error: Command timed out after 120 seconds"` | Task.yield + brutal_kill |
| 路径沙箱违规 | 返回 `"Error: Sandbox violation"` | 不执行 |
| 自毁命令 | 返回 `"Error: Command blocked: <reason>"` | 不执行 |

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| 单轮工具数上限 | `5` | 工具执行 |
| 工具执行超时 | `120_000` ms | 工具执行 |
| 大输出行数阈值 | `2000` 行 | 工具执行 |
| 大输出字节阈值 | `50_000` bytes | 工具执行 |
| 临时文件保留天数 | `7` 天 | 工具执行 |
| 截断预览头行数 | `20` | 工具执行 |
| 截断预览尾行数 | `5` | 工具执行 |
| read_file 默认 limit | `2000` | 工具执行 |
| 审批超时 | `120_000` ms | 工具执行 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| T1 | TS `trigger_integration` 是 placeholder | Python 侧实现真正的集成测试触发，或明确标记为未实现 |
| E1 | MCP 简化为 HTTP-only | 使用官方 `mcp` Python SDK，支持 stdio + HTTP |
| — | Elixir 73 个 dispatch 分支比 TS 68 个多 | 以 Elixir 为准（active backend），补齐 TS 缺失的工具 |

## Python 实现建议

- **架构模式**：
  - 工具注册表模式：`TOOL_REGISTRY: dict[str, Callable]`，每个工具是一个 async 函数
  - 权限矩阵用配置表（dict/Pydantic model），不用继承
  - `get_tools()` 返回 OpenAI function-calling 格式的 JSON schema 列表
  - `execute()` 是 async，因为 bash/HTTP 工具需要 async

- **bash 工具**：
  - `asyncio.create_subprocess_exec` 或 `asyncio.create_subprocess_shell`
  - 用 `asyncio.wait_for` 实现 120s 超时
  - Windows 用 `cmd /c`，Linux/Mac 用 `bash -c`
  - 环境变量注入 `HIVEWEAVE_BASH=1` + `HIVEWEAVE_WORKSPACE=<cwd>`

- **路径沙箱**：
  - `pathlib.Path.resolve()` 检查是否在 workspace 内
  - worktree agent 的 effective_workspace 是 `.hiveweave/worktrees/<shortId>/`

- **权限检查**：
  - 三级：allow / deny / ask
  - ask 时先查 saved_rules（glob 匹配），再走 ApprovalService 异步请求
  - 参考 OpenCode 的 permission 模式：`packages/opencode/src/tool/permission.ts`

- **大输出处理**：
  - 参考 OpenCode 的 `packages/opencode/src/tool/truncate.ts`（MAX_LINES=2000, MAX_BYTES=50KB, 7 天保留）
  - 存到 `.hiveweave/tool_outputs/`，返回 head+tail 预览

## 验收标准

- [ ] `get_tools()` 按 permission_type + role 返回正确的工具集
- [ ] CEO 有 management + git_worktree + binding + admin + charter_full + readonly_file + core
- [ ] HR 有 hire_tools + binding + admin + charter_readonly + readonly_file + core（无 management, 无 git_worktree）
- [ ] Executor 有 full_file + executor_specific + core + self_skill（无 management, 无 git_worktree, 无 binding, 无 admin）
- [ ] QA 有 full_file + qa_review + core + self_skill
- [ ] Test Engineer 有 bash + readonly_file + core + self_skill（无 write_file, 无 apply_patch）
- [ ] Auditor 有 bash + readonly_file + qa_review + core + self_skill（无 write_file, 无 apply_patch）
- [ ] `execute()` 先剥离 `hiveweave__` 前缀，再权限检查，再 dispatch
- [ ] 权限 deny 返回 "Permission denied" 字符串（不抛异常）
- [ ] 权限 ask 先查 saved_rules，匹配则执行，否则请求审批
- [ ] 审批超时返回友好提示
- [ ] 所有工具异常被捕获，返回 "Error: ..." 字符串
- [ ] bash 执行前检查自毁命令模式
- [ ] bash 执行的 workdir 必须在 workspace 内
- [ ] bash 超时 120s 后强制终止
- [ ] 工具输出超过 2000 行或 50KB 时存盘并返回截断预览
- [ ] 临时文件 7 天后自动清理
- [ ] read_file 支持 offset + limit 参数
- [ ] `run_command` 不执行自毁检查（与 bash 区分）
- [ ] worktree agent 的 effective_workspace 是其 worktree 目录

## 并行对比测试方案

| 测试场景 | Elixir 行为 | Python 预期行为 | 验证方法 |
|---|---|---|---|
| CEO 工具集 | 返回 management+git_worktree+binding+admin+charter_full+readonly_file+core | 相同 | 对比工具列表 JSON |
| HR 工具集 | 返回 hire+binding+admin+charter_readonly+readonly_file+core | 相同 | 同上 |
| Executor 工具集 | 返回 full_file+executor_specific+core+self_skill | 相同 | 同上 |
| 权限拒绝 | 返回 "Permission denied" | 相同 | 配置 deny 规则，对比返回字符串 |
| bash 正常执行 | 执行命令返回 stdout | 相同 | 执行 `echo hello`，对比输出 |
| bash 自毁命令 | 阻止并返回 "Command blocked" | 相同 | 执行 `rm -rf /`，对比阻止消息 |
| bash 超时 | 120s 后返回超时错误 | 相同 | 执行 `ping -t localhost`，对比超时行为 |
| bash 路径沙箱 | workdir 在 workspace 外返回 "Sandbox violation" | 相同 | 指定 workspace 外的 workdir |
| 大输出截断 | > 2000 行时存盘返回预览 | 相同 | 执行输出 3000 行的命令，对比预览格式 |
| read_file offset+limit | 从 offset 行开始读 limit 行 | 相同 | 读大文件，对比返回内容 |
| 未知工具 | 返回 "Unknown tool" | 相同 | 调用不存在的工具名 |

---

> **填写规则**：
> 1. 用 spec 语言，不用代码语言。说"做什么"，不说"怎么做"。
> 2. 所有常量值引用 `constants.md`，不在此处重复定义。
> 3. 所有已知问题引用 `known-issues.md`，不在此处重复描述。
> 4. 每个契约必须经过用户确认后才能标记为"已确认"。
> 5. "Python 实现建议"是建议不是约束，实现者可以根据情况调整，但必须满足验收标准。
