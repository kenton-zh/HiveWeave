# 提示词压缩计划

## 目标

通过 caveman 压缩风格，降低 Agent 上下文中的 token 占用，同时保留所有技术实质内容。

预期效果：read_skill 返回内容缩减 ~75%，Agent 间通信缩减 ~75%，SKILL.md 总量从 257KB 降到 ~64KB。

## 背景决策

以下 3 点已确认：
1. SKILL.md 和 references **预压缩**（静态文件压缩一次，零运行时成本）
2. Agent → User 回复 **不用 caveman**（用户看到正常完整回复）
3. memory 文件 **不压缩**（保留 Agent 原始表达）

---

## 压缩规则（来源：caveman-compress SKILL.md）

### 必须删除

- **冠词**：a, an, the
- **填充词**：just, really, basically, actually, simply, essentially, generally
- **客套话**：sure, certainly, of course, happy to, I'd recommend
- **对冲词**：it might be worth, you could consider, it would be good to
- **冗长短语**：in order to → to, make sure to → ensure, the reason is because → because
- **连接废话**：however, furthermore, additionally, in addition

### 必须原样保留（绝不修改）

- 代码块（``` 围栏和缩进块）
- 行内代码（`backtick content`）
- URL 和链接（完整 URL、markdown 链接）
- 文件路径（`/src/components/...`, `./config.yaml`）
- 命令（`npm install`, `git commit`, `docker build`）
- 技术术语（库名、API 名、协议、算法名）
- 专有名词（项目名、人名、公司名）
- 日期、版本号、数值
- 环境变量（`$HOME`, `NODE_ENV`）

### 必须保留结构

- 所有 markdown 标题（保留标题原文，压缩标题下方的正文）
- 列表层级（保持嵌套级别）
- 有序列表编号
- 表格结构（压缩单元格文本，保留表格结构）
- YAML frontmatter / 元数据头

### 压缩手法

- 用短同义词：big 不用 extensive，fix 不用 implement a solution for，use 不用 utilize
- 允许片段：Run tests before commit 不用 You should always run tests before committing
- 删掉 you should, make sure to, remember to — 直接陈述动作
- 合并表达相同意思的重复条目
- 多个同类示例只保留一个

### 压缩示例

**原文：**
> You should always start by writing a specification before any code is written. This specification should include acceptance criteria, user stories, and constraints. The specification must answer what we are building, why, and for whom.

**压缩后：**
> Write spec before code. Include: acceptance criteria, user stories, constraints. Spec must answer: what building, why, for whom, success criteria.

---

## 任务分解

### 任务 1：预压缩 24 个 SKILL.md 文件

**输入目录**：`d:\PC_AI\Project\agent-skills\skills\*/SKILL.md`

**输出**：每个 SKILL.md 旁边生成 `SKILL.compressed.md`

**24 个技能清单及原始大小**：

| # | 技能 slug | 原始大小 | 预期压缩后 |
|---|---|---|---|
| 1 | api-and-interface-design | ~12KB | ~3KB |
| 2 | browser-testing-with-devtools | ~9KB | ~2.3KB |
| 3 | ci-cd-and-automation | ~8KB | ~2KB |
| 4 | code-review-and-quality | ~18.8KB | ~4.7KB |
| 5 | code-simplification | ~7KB | ~1.8KB |
| 6 | context-engineering | ~8KB | ~2KB |
| 7 | debugging-and-error-recovery | ~10.5KB | ~2.6KB |
| 8 | deprecation-and-migration | ~10KB | ~2.5KB |
| 9 | doc-writing-guide | ~12KB | ~3KB |
| 10 | documentation-and-adrs | ~8KB | ~2KB |
| 11 | doubt-driven-development | ~7KB | ~1.8KB |
| 12 | frontend-ui-engineering | ~15KB | ~3.8KB |
| 13 | game-design-theory | ~12KB | ~3KB |
| 14 | html-deck | ~10KB | ~2.5KB |
| 15 | html-report | ~10KB | ~2.5KB |
| 16 | idea-refine | ~7KB | ~1.8KB |
| 17 | incremental-implementation | ~9.2KB | ~2.3KB |
| 18 | interview-me | ~6KB | ~1.5KB |
| 19 | observability-and-instrumentation | ~10KB | ~2.5KB |
| 20 | performance-optimization | ~10KB | ~2.5KB |
| 21 | planning-and-task-breakdown | ~7.4KB | ~1.9KB |
| 22 | security-and-hardening | ~10KB | ~2.5KB |
| 23 | shipping-and-launch | ~10.2KB | ~2.6KB |
| 24 | spec-driven-development | ~8.4KB | ~2.1KB |
| 25 | source-driven-development | ~8KB | ~2KB |
| 26 | tabletop-rpg-design | ~12KB | ~3KB |
| 27 | test-driven-development | ~15.1KB | ~3.8KB |
| 28 | using-agent-skills | ~7KB | ~1.8KB |

（注：实际技能数可能多于 24，以目录中实际存在的为准。上表大小为估算值，执行时以实际文件大小为准。）

**执行步骤**：

1. 遍历 `d:\PC_AI\Project\agent-skills\skills\*/SKILL.md`
2. 对每个文件，按上述压缩规则手工或用 LLM 压缩
3. 保留 YAML frontmatter（`---` 之间的内容）原样不动
4. 保留所有代码块原样不动
5. 保留所有文件路径、命令、URL、技术术语原样不动
6. 压缩正文散文部分
7. 输出为同目录下的 `SKILL.compressed.md`
8. **不覆盖原始 SKILL.md**——保留原版作为参照

**压缩质量检查**（每个文件压缩后执行）：
- frontmatter 完整保留？
- 代码块数量与原文一致？
- 文件路径 / 命令 / URL 未被修改？
- 核心步骤和验证门禁全部保留？
- 没有发明原文不存在的新内容？

---

### 任务 2：预压缩 7 个 reference 文件

**输入目录**：`d:\PC_AI\Project\agent-skills\references\*.md`

**输出**：每个文件旁边生成 `<filename>.compressed.md`

**7 个 reference 文件**：

| # | 文件名 | 用途 |
|---|---|---|
| 1 | definition-of-done.md | 完成标准 |
| 2 | security-checklist.md | 安全检查清单 |
| 3 | testing-patterns.md | 测试模式 |
| 4 | performance-checklist.md | 性能检查清单 |
| 5 | accessibility-checklist.md | 无障碍检查清单 |
| 6 | observability-checklist.md | 可观测性检查清单 |
| 7 | （以目录中实际存在的为准） | |

**执行步骤**：与任务 1 相同的压缩规则和检查标准。

---

### 任务 3：修改 skill_registry.ex 读取压缩版

**文件**：`apps/hiveweave/lib/hiveweave/skill_registry.ex`

**改动**：`read_skill/1` 函数读取技能全文时，优先读取 `SKILL.compressed.md`，不存在则回退到 `SKILL.md`。

**伪代码**：

```elixir
def read_skill(slug) do
  skill_dir = Path.join(@skills_dir, slug)
  compressed = Path.join(skill_dir, "SKILL.compressed.md")
  original = Path.join(skill_dir, "SKILL.md")
  
  cond do
    File.exists?(compressed) -> File.read!(compressed)
    File.exists?(original) -> File.read!(original)
    true -> {:error, "Skill not found: #{slug}"}
  end
end
```

**同样适用于 references**：如果项目创建时复制 references 到 `.agent-skills/references/`，优先复制 `.compressed.md` 版本。

---

### 任务 4：在 identity prompt 中注入 caveman 通信规则

**文件**：`apps/hiveweave/lib/hiveweave/llm/streamer.ex`

**改动位置**：`build_coordinator_prompt/1` 和 `build_executor_prompt/1`

#### 4a. 所有角色通用的 caveman 通信规则

追加到所有角色的 identity prompt 末尾（~300 chars）：

```
## Communication Style — Caveman for Agent-to-Agent
When reporting to superiors (report_completion, send_message to agent):
terse, drop articles/filler, fragments OK, technical terms exact.
When reporting to user (send_message to user): normal, complete, friendly.
```

**注意**：这段内容进入 `sys_identity`（静态，prefix-cached）。变更后触发一次 cache miss，之后永久稳定。

#### 4b. 审查员专属的 caveman-review 汇报格式

追加到审查员的 executor prompt（~200 chars）：

```
## Review Report Format
One line per finding: path:line: severity: problem. fix.
Severity: 🔴bug / 🟡risk / 🔵nit / ❓q
End with: totals: N🔴 N🟡 N🔵 N❓
Example: src/auth/login.ts:L45: 🔴bug: password compare not constant-time. Use crypto.timingSafeEqual.
```

#### 4c. 不需要修改的部分

- `build_context_prompt`（sys_context）— 不涉及通信风格
- `build_active_skills_section` — 技能索引不受影响
- tool schema — 工具定义不变
- conversation store — compaction 机制不变

---

### 任务 5：预压缩 persona 文件

**输入目录**：`d:\PC_AI\Project\agent-skills\agents\*.md`

**4 个 persona 文件**：

| # | 文件名 | 原始大小 | 预期压缩后 |
|---|---|---|---|
| 1 | code-reviewer.md | ~3.9KB | ~1KB |
| 2 | security-auditor.md | ~5.1KB | ~1.3KB |
| 3 | test-engineer.md | ~3.4KB | ~0.9KB |
| 4 | web-performance-auditor.md | ~12.4KB | ~3.1KB |

**输出**：每个文件旁边生成 `<filename>.compressed.md`

**用途**：审查工具（run_code_review 等）的 `build_review_system_prompt/1` 读取压缩版 persona 注入工具内部 LLM 调用。

**改动**：`tool_executor.ex` 中 `build_review_system_prompt/1` 优先读取 `.compressed.md`。

---

## 验证方案

### 验证 1：压缩率检查

对每个压缩文件，验证压缩率在 60%-80% 之间：

```
原始大小: 8400 bytes
压缩后:   2100 bytes
压缩率:   75% ✓ (在 60-80% 范围内)
```

如果压缩率 < 60%，说明压缩不充分；如果 > 80%，说明可能过度压缩丢失了内容。

### 验证 2：内容完整性检查

对每个压缩文件，检查以下内容是否保留：

- [ ] YAML frontmatter 完整（`name` 和 `description` 字段未变）
- [ ] 代码块数量与原文一致
- [ ] 所有文件路径未被修改（grep 对比）
- [ ] 所有命令未被修改（grep 对比）
- [ ] 所有 URL 未被修改（grep 对比）
- [ ] 核心步骤（Step/Phase/阶段）数量一致
- [ ] 验证门禁（Verify/Check/验证）条目数量一致

### 验证 3：功能验证

压缩完成后，启动后端验证：

1. 调用 `read_skill("spec-driven-development")` 返回压缩版内容
2. 调用 `read_skill("code-review-and-quality")` 返回压缩版内容
3. Agent 间通信（report_completion）输出为 caveman 风格
4. Agent → User 回复仍为正常风格
5. 审查员汇报格式为 caveman-review 一行一发现格式

### 验证 4：上下文预算对比

在测试项目中触发一轮完整的 CEO → 技术负责人 → Developer → 审查员 流程，对比：

| 指标 | 压缩前 | 压缩后 |
|---|---|---|
| read_skill 单次返回大小 | ~8-19KB | ~2-5KB |
| Developer report_completion 大小 | ~500B | ~125B |
| 审查员审查报告大小 | ~1.5KB | ~200B |
| CEO 第 8 轮 history 总量 | ~68KB | ~17KB |

---

## 文件清单

### 需要新建的文件

```
agent-skills/
  skills/
    api-and-interface-design/SKILL.compressed.md
    browser-testing-with-devtools/SKILL.compressed.md
    ci-cd-and-automation/SKILL.compressed.md
    code-review-and-quality/SKILL.compressed.md
    code-simplification/SKILL.compressed.md
    context-engineering/SKILL.compressed.md
    debugging-and-error-recovery/SKILL.compressed.md
    deprecation-and-migration/SKILL.compressed.md
    doc-writing-guide/SKILL.compressed.md
    documentation-and-adrs/SKILL.compressed.md
    doubt-driven-development/SKILL.compressed.md
    frontend-ui-engineering/SKILL.compressed.md
    game-design-theory/SKILL.compressed.md
    html-deck/SKILL.compressed.md
    html-report/SKILL.compressed.md
    idea-refine/SKILL.compressed.md
    incremental-implementation/SKILL.compressed.md
    interview-me/SKILL.compressed.md
    observability-and-instrumentation/SKILL.compressed.md
    performance-optimization/SKILL.compressed.md
    planning-and-task-breakdown/SKILL.compressed.md
    security-and-hardening/SKILL.compressed.md
    shipping-and-launch/SKILL.compressed.md
    spec-driven-development/SKILL.compressed.md
    source-driven-development/SKILL.compressed.md
    tabletop-rpg-design/SKILL.compressed.md
    test-driven-development/SKILL.compressed.md
    using-agent-skills/SKILL.compressed.md
  references/
    definition-of-done.compressed.md
    security-checklist.compressed.md
    testing-patterns.compressed.md
    performance-checklist.compressed.md
    accessibility-checklist.compressed.md
    observability-checklist.compressed.md
  agents/
    code-reviewer.compressed.md
    security-auditor.compressed.md
    test-engineer.compressed.md
    web-performance-auditor.compressed.md
```

### 需要修改的代码文件

| 文件 | 改动内容 |
|---|---|
| `apps/hiveweave/lib/hiveweave/skill_registry.ex` | `read_skill/1` 优先读 `.compressed.md` |
| `apps/hiveweave/lib/hiveweave/llm/streamer.ex` | identity prompt 注入 caveman 通信规则 + 审查员 review 格式 |
| `apps/hiveweave/lib/hiveweave/tool_executor.ex` | `build_review_system_prompt/1` 优先读 `.compressed.md` persona |

---

## 执行顺序

1. **任务 1**：预压缩 24+ 个 SKILL.md（最耗时，可并行）
2. **任务 2**：预压缩 7 个 reference 文件
3. **任务 5**：预压缩 4 个 persona 文件
4. **任务 3**：修改 skill_registry.ex（依赖任务 1 完成）
5. **任务 4**：修改 streamer.ex 注入 caveman 规则（不依赖前序任务，可并行）
6. **验证**：全部完成后执行验证方案

任务 1、2、5 可以并行执行（都是文件压缩，互不依赖）。任务 3 依赖任务 1。任务 4 独立。

---

## 注意事项

1. **不覆盖原始文件**：所有压缩版以 `.compressed.md` 后缀新建，原始 `.md` 保留作为参照和回退
2. **frontmatter 绝对不动**：YAML frontmatter 中的 `name` 和 `description` 字段是 SkillRegistry 索引的来源，修改会导致技能无法被搜索到
3. **代码块绝对不动**：SKILL.md 中的代码示例、命令、配置片段必须原样保留
4. **中文内容**：部分 SKILL.md 可能包含中文，caveman 压缩规则同样适用于中文——删填充词（"的话"、"其实"、"基本上"），保留技术术语
5. **文言文模式不用**：caveman 支持 wenyan 模式，但本计划不使用——Agent 间通信用英文 caveman full 级别即可
