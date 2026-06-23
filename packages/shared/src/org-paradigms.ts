/**
 * Organizational Paradigm Library
 *
 * Defines reusable team structure patterns that HR agents can match
 * to project requirements. Paradigms are reference baselines, not
 * rigid templates — HR may trim, combine, or fine-tune as needed.
 */

export interface OrgParadigmStructure {
  /** Suggested hierarchy depth (1 = flat, 2-3 = moderate, 4+ = deep) */
  layers: number;
  /** Whether the paradigm includes a coordinator/management layer */
  hasCoordinator: boolean;
  /** Typical team size range (human-readable) */
  typicalSize: string;
}

export interface OrgParadigm {
  /** Stable identifier for code references, e.g. "flat_squad" */
  id: string;
  /** Display name in Chinese, e.g. "扁平小组" */
  name: string;
  /** English name for code references */
  englishName: string;
  /** One-line description of the paradigm */
  description: string;
  /** Structural characteristics */
  structure: OrgParadigmStructure;
  /** Typical roles in this paradigm */
  roles: string[];
  /** Signals that this paradigm is a good fit */
  fitSignals: string[];
  /** Signals that this paradigm is NOT a good fit */
  antiFitSignals: string[];
  /** Guidance for HR when building a team with this paradigm (LLM-readable) */
  creationGuidance: string;
}

// ---------------------------------------------------------------------------
// Software Development Paradigms
// ---------------------------------------------------------------------------

export const SOFTWARE_PARADIGMS: OrgParadigm[] = [
  {
    id: "solo",
    name: "单兵模式",
    englishName: "Solo",
    description: "一个全能 executor 独立完成明确目标的任务，无协调层，零管理开销。",
    structure: {
      layers: 1,
      hasCoordinator: false,
      typicalSize: "1",
    },
    roles: ["developer"],
    fitSignals: [
      "目标明确且单一",
      "脚本或工具开发",
      "一次性任务",
      "MVP 验证",
      "代码重构或迁移",
      "不需要多人协作",
    ],
    antiFitSignals: [
      "需要多领域专业知识",
      "项目周期长",
      "需要持续维护",
      "涉及多个子系统",
    ],
    creationGuidance: [
      "创建 1 个 executor agent，赋予所有必要的能力。",
      "不设 coordinator，agent 的 parentId 设为 null（root-level）。",
      "role 根据具体任务选择（通常是 developer）。",
      "goal 和 backstory 中明确写出完整交付目标。",
      "这是最轻量的模式，不要过度设计。",
    ].join("\n"),
  },

  {
    id: "flat_squad",
    name: "扁平小组",
    englishName: "Flat Squad",
    description: "2-5 个 executor 平级协作，没有中间管理层，靠自主协调推进。",
    structure: {
      layers: 1,
      hasCoordinator: false,
      typicalSize: "2-5",
    },
    roles: ["developer", "qa", "devops"],
    fitSignals: [
      "小型项目",
      "原型/POC",
      "快速迭代",
      "团队间耦合低",
      "每个人都能独立交付",
      "startup 早期",
    ],
    antiFitSignals: [
      "需要跨团队协调",
      "有严格的质量门禁",
      "超过 5 个独立工作流",
      "需要统一的技术方向把控",
    ],
    creationGuidance: [
      "创建 2-5 个 executor agent，全部设为 root-level（parentId: null）。",
      "不设 coordinator，每个 agent 独立工作，通过 message_peer 互相沟通。",
      "按职能划分角色（如前端开发、后端开发、测试），避免职责重叠。",
      "每个 agent 的 goal 中明确其独立交付物和与其他人的协作边界。",
      "如果需要一个轻量的技术把关人，可以把其中一个 developer 设为 coordinator + executor 混合体（但仍然不创建下属层级）。",
    ].join("\n"),
  },

  {
    id: "tech_lead",
    name: "Tech Lead 制",
    englishName: "Tech Lead",
    description: "一个技术负责人（coordinator）做技术决策并指导 executor 团队，无 PM 层。",
    structure: {
      layers: 2,
      hasCoordinator: true,
      typicalSize: "3-8",
    },
    roles: ["architect", "developer", "qa"],
    fitSignals: [
      "纯技术项目",
      "库/框架/SDK 开发",
      "基础设施",
      "技术方案探索",
      "需要统一的技术方向",
      "团队成员需要技术指导",
    ],
    antiFitSignals: [
      "需要非技术管理（进度、预算、干系人）",
      "多业务线并行",
      "需要产品决策",
      "需要跨部门协调",
    ],
    creationGuidance: [
      "创建 1 个 coordinator（role: architect），作为技术负责人，parentId: null。",
      "在技术负责人下创建 2-6 个 executor（role: developer/qa），parentId 指向技术负责人。",
      "技术负责人的 goal 中强调：技术决策、代码质量把控、架构设计、指导开发者。",
      "技术负责人的 backstory 中写明其技术栈偏好和设计哲学。",
      "executor 的 goal 聚焦于具体的实现任务。",
      "这是 2 层结构：Tech Lead → Executors。不要加 PM 层。",
    ].join("\n"),
  },

  {
    id: "pm_architect",
    name: "PM + 架构师",
    englishName: "PM + Architect",
    description: "项目经理管协调与进度，架构师管技术方向，双线领导开发团队。适合中大型多领域项目。",
    structure: {
      layers: 3,
      hasCoordinator: true,
      typicalSize: "5-15",
    },
    roles: ["manager", "architect", "developer", "qa", "devops"],
    fitSignals: [
      "中大型项目",
      "多领域协作（前端+后端+基础设施）",
      "需要进度管理",
      "需要技术方向把控",
      "有多个工作流需要并行",
      "交付周期较长",
    ],
    antiFitSignals: [
      "小项目（杀鸡用牛刀）",
      "纯技术探索（不需要管理）",
      "团队 < 5 人",
      "快速原型",
    ],
    creationGuidance: [
      "创建 2 个 root-level coordinator：1 个 PM（role: manager）和 1 个架构师（role: architect），parentId 都为 null。",
      "PM 负责：任务分解、进度跟踪、需求确认、用户沟通。",
      "架构师负责：技术选型、代码审查、架构设计、技术指导。",
      "在架构师下创建开发团队（2-8 个 executor，role: developer），parentId 指向架构师。",
      "可选：创建独立的 QA（role: qa, executor），parentId 指向 PM 或架构师。",
      "这是 3 层结构：PM/Architect → Developers → (可选 sub-modules)。",
      "PM 和架构师是平级关系，通过 message_peer 沟通。",
    ].join("\n"),
  },

  {
    id: "pod",
    name: "Pod/小组制",
    englishName: "Pod System",
    description: "大型项目拆分为自治的 Pod（小组），每个 Pod 有自己的 Lead 和开发者，Pod Lead 向上汇报。",
    structure: {
      layers: 3,
      hasCoordinator: true,
      typicalSize: "8-20+",
    },
    roles: ["manager", "architect", "developer", "qa", "devops"],
    fitSignals: [
      "大型项目",
      "多领域需要自治",
      "明确的模块边界",
      "需要并行推进多个工作流",
      "每个领域有独立的交付周期",
      "企业级平台",
    ],
    antiFitSignals: [
      "小项目",
      "单一领域",
      "快速迭代（pod 间协调成本高）",
      "团队 < 8 人",
    ],
    creationGuidance: [
      "创建 1 个 root-level coordinator 作为总负责人（role: manager），parentId: null。",
      "按领域拆分 Pod，每个 Pod 创建一个 coordinator（role: manager 或 architect）作为 Pod Lead，parentId 指向总负责人。",
      "每个 Pod Lead 下创建 2-5 个 executor（role: developer），parentId 指向 Pod Lead。",
      "可选：每个 Pod 配 1 个 QA（role: qa），或创建共享的 QA 团队直接挂在总负责人下。",
      "这是 3 层结构：总负责人 → Pod Leads → Developers。",
      "Pod 之间通过各自的 Lead 互相 message_peer 沟通。",
      "每个 Pod 应该能独立交付，减少跨 Pod 依赖。",
    ].join("\n"),
  },

  {
    id: "pipeline",
    name: "流水线",
    englishName: "Pipeline",
    description: "按阶段顺序推进：设计→开发→测试→部署。每个阶段由专门的 executor 负责，coordinator 管理流转。",
    structure: {
      layers: 2,
      hasCoordinator: true,
      typicalSize: "4-10",
    },
    roles: ["manager", "developer", "qa", "devops"],
    fitSignals: [
      "严格阶段依赖",
      "需要逐步验证",
      "合规要求",
      "瀑布式流程",
      "设计和开发分离",
      "测试是独立阶段",
    ],
    antiFitSignals: [
      "需要快速迭代",
      "阶段之间没有强依赖",
      "小项目（流水线太重）",
      "探索性项目",
    ],
    creationGuidance: [
      "创建 1 个 root-level coordinator（role: manager）作为流水线调度者，parentId: null。",
      "按阶段创建 executor agent，全部 parentId 指向调度者：",
      "  - 阶段 1: 设计/规划（role: architect 或 manager）",
      "  - 阶段 2: 开发实现（role: developer）",
      "  - 阶段 3: 测试验证（role: qa）",
      "  - 阶段 4: 部署运维（role: devops）",
      "调度者的 goal 中强调：按阶段推进、上一阶段完成后才启动下一阶段、质量门禁。",
      "每个 executor 的 goal 中明确其阶段的输入和输出标准。",
      "这是 2 层结构：Pipeline Coordinator → Stage Executors。",
    ].join("\n"),
  },
];

// ---------------------------------------------------------------------------
// Lookup helpers
// ---------------------------------------------------------------------------

/** Get all paradigms (software + future general-purpose) */
export function getAllParadigms(): OrgParadigm[] {
  return [...SOFTWARE_PARADIGMS];
}

/** Find a paradigm by its stable ID */
export function getParadigmById(id: string): OrgParadigm | undefined {
  return SOFTWARE_PARADIGMS.find((p) => p.id === id);
}

/**
 * Generate a concise catalog summary for injection into HR prompts.
 * Each paradigm is summarized to a few lines to keep the prompt compact.
 */
export function getParadigmCatalogSummary(): string {
  return SOFTWARE_PARADIGMS.map(
    (p) =>
      [
        `### ${p.name} (${p.id})`,
        `${p.description}`,
        `规模: ${p.structure.typicalSize} 人 | 层级: ${p.structure.layers} 层 | 协调层: ${p.structure.hasCoordinator ? "有" : "无"}`,
        `适合: ${p.fitSignals.slice(0, 4).join("、")}`,
        `不适合: ${p.antiFitSignals.slice(0, 3).join("、")}`,
      ].join("\n"),
  ).join("\n\n");
}
