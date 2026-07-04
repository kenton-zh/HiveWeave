# HiveWeave ExUnit + Playwright 优先验证范围
> 目标：用最小可行测试集打通“后端可测 + 前端可测”链路，形成质量门禁基线。

## 1. 后端 ExUnit 优先范围

### 1.1 必须跑通（P0）
| 测试文件 | 覆盖模块 | 验证目标 |
|----------|----------|----------|
| `test/hiveweave/services/permission_test.exs` | `HiveWeave.Services.Permmission` | 权限 allow/ask/deny 三态 |
| `test/hiveweave/services/settings_test.exs` | `HiveWeave.Services.Settings` | KV 读写 |
| `test/hiveweave_web/health_controller_test.exs` | `HiveWeaveWeb.HealthController` | `/api/health` 200 |

### 1.2 优先补充（P1）
| 测试文件 | 覆盖模块 | 验证目标 |
|----------|----------|----------|
| `test/hiveweave/services/org_test.exs` | `HiveWeave.Services.Org` | 组织树构建 |
| `test/hiveweave/services/model_test.exs` | `HiveWeave.Services.Model` | LLM 模型 CRUD |
| `test/hiveweave/services/template_test.exs` | `HiveWeave.Services.Template` | 模板 CRUD |

### 1.3 后续扩展（P2）
- `tool_executor_p1_test.exs` / `p2_test.exs` — 工具执行权限门禁
- `conversation_store_test.exs` — 对话历史压缩
- `token_utils_test.exs` — Token 估算

## 2. 前端 Playwright 优先范围

### 2.1 必须跑通（P0）
| 测试场景 | 验证目标 | 依赖 |
|----------|----------|------|
| 页面加载 | 首页渲染、标题、组织树节点 | 后端 `/api/health` + `/api/org` |
| API 探活 | `/api/health` 返回 200 | 后端 4000 端口 |
| 组织树显示 | Agent 名称渲染 | 后端 `/api/org` |

### 2.2 优先补充（P1）
| 测试场景 | 验证目标 | 依赖 |
|----------|----------|------|
| 选择 Agent + 右面板切换 | Chat/Goals/Agent/Logs 标签可切换 | 后端 `/api/chat/history/:id` 等 |
| 模型设置弹窗 | 打开/关闭 | 前端组件 |
| 暂停/恢复按钮 | 状态切换 | 后端 `/api/chat/pause` |

### 2.3 后续扩展（P2）
- 新建 Agent 流程
- Office 视图渲染
- 聊天发送与流式响应

## 3. 执行顺序
1. 修复后端编译错误（Plug  retired 版本、Phoenix 模块加载）
2. 跑通 P0 ExUnit（health + permission + settings）
3. 配置 Playwright + MSW，跑通 P0 E2E
4. 补充 P1 测试，形成质量门禁
5. 添加 CI workflow，阻塞合并

## 4. 质量门禁标准
- ExUnit 通过率 = 100%（P0）
- Playwright P0 通过率 = 100%
- 新增代码必须伴随测试（Boil the Lake）
