# HiveWeave 接口契约草案
> Tech Lead 制下用于前后端联调与测试的基线文档。当前版本基于前端 `apps/web/src/api.ts` 与后端 `apps/hiveweave/lib/hiveweave_web/router.ex` 对齐整理。

## 1. 基础约定
- **Base URL**: `http://localhost:4000/api`
- **WebSocket**: `ws://localhost:4000/socket`
- **认证**: Header `x-api-key: <key>`；未携带时后端按匿名/默认策略处理
- **Content-Type**: `application/json`
- **响应格式**: 统一 JSON；错误时返回 `%{error: String.t()}` 或 HTTP 4xx/5xx

## 2. REST API 清单

### 2.1 Settings
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/settings` | SettingsController#index | 列出所有设置 |
| GET | `/settings/:key` | SettingsController#show | 读取单个设置 |
| POST/PUT | `/settings` | SettingsController#upsert | 写入设置 |

### 2.2 Projects
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/projects` | ProjectsController#index | 项目列表 |
| POST | `/projects` | ProjectsController#create | 创建项目 |
| GET | `/projects/:id` | ProjectsController#show | 项目详情 |
| PATCH/PUT | `/projects/:id` | ProjectsController#update | 更新项目 |
| PUT | `/projects/:id/workspace` | ProjectsController#update_workspace | 更新工作区路径 |
| DELETE | `/projects/:id` | ProjectsController#delete | 删除项目 |
| GET | `/projects/:id/game-time` | ProjectsController#game_time | 项目游戏时间 |
| GET | `/projects/:id/goals` | ProjectsController#goals | 项目 OKR |
| PUT | `/projects/:id/goals` | ProjectsController#update_goals | 更新 OKR |

### 2.3 Org / Agents
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/org` | OrgController#tree | 组织树 |
| GET | `/org/agents` | OrgController#list_agents | Agent 列表 |
| GET | `/org/agents/:id` | OrgController#show_agent | Agent 详情 |
| GET | `/org/agents/:id/children` | OrgController#children | 直属下属 |
| POST | `/org/agents` | OrgController#create_agent | 创建 Agent |
| PATCH/PUT | `/org/agents/:id` | OrgController#update_agent | 更新 Agent |
| DELETE | `/org/agents/:id` | OrgController#delete_agent | 删除 Agent |
| GET | `/org/modules` | OrgController#list_modules | 模块列表 |

### 2.4 Chat
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| POST | `/chat` | ChatController#send | 发送消息 |
| GET | `/chat/history/:agentId` | ChatController#history | 聊天历史 |
| GET | `/chat/messages/:agentId` | ExtraController#chat_messages | 消息列表 |
| GET | `/chat/unread/:agentId` | ChatController#unread | 未读计数 |
| POST | `/chat/mark-read` | ChatController#mark_read | 标记已读 |
| GET | `/chat/inbox/:agentId` | ChatController#inbox | 收件箱 |
| POST | `/chat/inbox` | ChatController#send_inbox | 发送收件箱消息 |
| POST | `/chat/pause` | ChatController#pause | 暂停 Agent |
| POST | `/chat/resume` | ChatController#resume | 恢复 Agent |
| GET | `/chat/paused` | ChatController#paused | 已暂停列表 |
| POST | `/chat/reset-processing/:agentId` | ChatController#reset_processing | 重置处理状态 |
| GET | `/chat/resolved-model/:agentId` | ChatController#resolved_model | 已解析模型 |
| GET | `/chat/todos/:agentId` | ExtraController#chat_todos | Todo 列表 |
| POST | `/chat/todos/:agentId` | ExtraController#chat_todos_write | 写入 Todo |
| GET | `/chat/questions` | ExtraController#chat_questions_index | 问题列表 |
| POST | `/chat/questions/:id/answer` | ExtraController#chat_questions_answer | 回答问题 |

### 2.5 Permissions / Approvals
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/permissions/rules/:agent_id` | PermissionsController#get_rules | 权限规则 |
| PATCH/PUT | `/permissions/rules/:agent_id` | PermissionsController#update_rules | 更新规则 |
| GET | `/permissions/pending/:agent_id` | PermissionsController#get_pending | 待审批 |
| GET | `/permissions/pending/project/:project_id` | PermissionsController#get_project_pending | 项目待审批 |
| POST | `/permissions/respond` | PermissionsController#respond | 审批响应 |

### 2.6 LLM Models
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/llm-models` | ExtraController#llm_models_index | 模型列表 |
| POST | `/llm-models` | ExtraController#llm_models_create | 创建模型 |
| GET | `/llm-models/:id` | ExtraController#llm_model_show | 模型详情 |
| PATCH/PUT | `/llm-models/:id` | ExtraController#llm_model_update | 更新模型 |
| DELETE | `/llm-models/:id` | ExtraController#llm_model_delete | 删除模型 |
| POST | `/llm-models/:id/test` | ExtraController#llm_model_test | 测试连通性 |

### 2.7 Agent Templates
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/agent-templates` | ExtraController#templates_index | 模板列表 |
| GET | `/agent-templates/divisions` | ExtraController#template_divisions | 部门列表 |
| GET | `/agent-templates/:id` | ExtraController#template_show | 模板详情 |

### 2.8 Communications
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/communications` | ExtraController#communications_index | 沟通记录 |
| POST | `/communications` | ExtraController#communications_create | 创建沟通 |

### 2.9 User Pings
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/user-pings` | ExtraController#user_pings_index | 用户 ping 列表 |
| POST | `/user-pings/:id/read` | ExtraController#user_ping_read | 标记已读 |

### 2.10 Project Alarms
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/projects/:project_id/alarms` | ExtraController#project_alarms_index | 闹钟列表 |
| POST | `/projects/:project_id/alarms` | ExtraController#project_alarms_create | 创建闹钟 |
| DELETE | `/projects/:project_id/alarms/:id` | ExtraController#project_alarm_cancel | 取消闹钟 |

### 2.11 Work Logs
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/logs/:agentId` | ExtraController#work_logs_index | 工作日志 |
| GET | `/logs/:agentId/subordinates` | ExtraController#work_logs_subordinates | 下属日志 |

### 2.12 Debug / Monitoring
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/debug/agents/:agentId/traces` | ExtraController#debug_traces | 调试追踪 |

### 2.13 Filesystem
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/fs/browse` | ExtraController#fs_browse | 浏览文件系统 |

### 2.14 Health
| Method | Path | Controller#Action | 说明 |
|--------|------|-------------------|------|
| GET | `/health` | HealthController#index | 健康检查 |

## 3. WebSocket Channels
| Channel | 事件 | 说明 |
|---------|------|------|
| `lobby:status` | `status_update` | 全局 Agent 处理状态 |
| `agent:<id>` | `stream_chunk`, `status`, `inbox` | 单 Agent 聊天流 + 状态 + 收件箱 |
| `project:<id>` | `game_time`, `status` | 项目游戏时间 + 状态 |

## 4. 关键类型（前端 TypeScript）
```ts
interface Project {
  id: string;
  name: string;
  workspacePath?: string;
  description?: string;
  orgParadigm?: string;
  language?: string;
  createdAt?: number;
}

interface KeyResult {
  text: string;
  status: "todo" | "doing" | "done";
  owner?: string;
}

interface GoalsData {
  objective: string;
  focus: string;
  keyResults: KeyResult[];
  userInvolvement?: string;
}

interface Agent {
  id: string;
  projectId: string;
  name: string;
  role: string;
  shortId?: string;
  parentId?: string;
  status?: string;
  permissionType?: string;
}
```

## 5. 待确认项
- [ ] 后端是否统一返回 `%{data: ...}` 包裹，还是直接裸对象？
- [ ] `/api/health` 是否带认证？
- [ ] WebSocket 连接参数中 `api_key` 是否必填？
- [ ] 分页/过滤参数规范（当前未发现，但列表接口后续可能需要）
