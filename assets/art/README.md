# assets/art —— 美术资产

> 项目美术素材的存放处。这里的东西**不参与构建** —— 不会被 vite 打包、不会进 EXE。

## 目录约定

```
assets/art/<领域>/
├── source/     # 源素材：AI 出图原稿、参考图、分方向 / 分层源文件
└── working/    # 工作文件：修图中间产物（LaMa patch、备份、试验稿）—— 不进 git
```

## 与运行时的分工（**这是本目录存在的理由，请遵守**）

| 位置 | 放什么 | 是否进构建 |
|---|---|---|
| **`assets/art/**`（这里）** | 源素材、工作文件、参考图 | ❌ **永不** |
| `apps/web/public/office-assets/` | **只有运行时真正加载的素材** | ✅ 会被 vite **全量**复制进 `dist/` |

⚠ **为什么必须分开**：`public/` 下的**每一个文件**都会进 dist，进而进 EXE。
2026-09-17 因此发生过 **7.93 MB 中间产物误打包**（`robocopy /MIR` 不读 `.gitignore`）。
当时修法是构建脚本加 `/XF` 过滤 —— **那是止血**；**`assets/art/` 才是根治**（源头就不在 public）。

**2026-09-22 完成搬迁**：`public/office-assets/` 从 **16 MB → 约 3 MB**，只剩 6 件运行时素材。

## office/

### `source/`（15 件，**进 git**）

| 文件 | 说明 |
|---|---|
| `agent-purple-walk-{back,front,left,right}.png` | 角色行走的**分方向源**（MiniMax H3 抽帧），供合成用；运行时只加载 `agent-purple-anim-sheet.png` |
| `agent-purple-typing-sheet.png` | 打字表 **v1 备份**（现行是 anim-sheet v2） |
| `agent-purple-stand-idle.png` | 站姿表（锚点同为 0.875；Godot 侧已接，Web 未接） |
| `office-desk-set.png` | 桌套件**源**（运行时用切好的 `office-desk-back/front.png`） |
| `office-reference-board.png` | 参考板 |
| `floor-wood` / `rug` / `sofa` / `poster` / `meeting-table` / `vending-machine` / `wall-clock`.jpg | 08-22 生成的高清配件素材，**当前 0 引用**（底图已把这些内容画进去了）—— 保留作源 |

### `working/`（10 件，**不进 git**）

LaMa 修图链的中间产物：`*.pre-{chair,iso,kairo}.bak.png` / `*.patched.png` / `*.with-desks.bak.png`
—— 均可从 `source/` + 脚本再生，故用 `.gitignore` 排除（体积大：三者合计约 8 MB）。

## 相关文档

- 资产清单 / 生成模板 / 校验标准：`docs/前端设计规格.md` **§11**
- 生成工具（3daistudio MCP · ComfyUI · apimart）：同文档 **§19** 与 `skills/image-generation/SKILL.md`
- 场景标定与修复流程：`skills/office-scene-calibration/SKILL.md`
- UI 概念稿：`docs/design-refs/`
