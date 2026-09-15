# scripts/

项目辅助脚本目录。

## gen-stats.py

扫描代码库生成项目统计 JSON（`docs/stats.json`），作为文档数字的**单一事实源**。

### 用途

文档与代码常常脱节——README 里写「74 个工具」，实际可能已增减。本脚本用正则扫描真实代码，把 API 路由数、工具数、游戏日时长、代码行数等指标固化成 `docs/stats.json`，让文档数字可以机器校验、避免漂移。

### 依赖

仅 Python 标准库（`pathlib` / `re` / `json` / `sys`），不依赖任何第三方包，无需 `uv sync` 即可运行。

### 用法

```bash
# 生成 / 刷新 docs/stats.json 并打印摘要
python scripts/gen-stats.py

# CI 校验模式：与已有 docs/stats.json 比对，漂移则 exit 1
python scripts/gen-stats.py --check
```

### 采集的指标

| 字段 | 来源 | 说明 |
|------|------|------|
| `api_routes` | `apps/hiveweave-py/src/hiveweave/api/*.py` | 匹配 `@(router\|app).(get\|post\|put\|delete\|patch\|websocket)(` 装饰器 |
| `api_modules` | 同上 | 至少含一条路由的 API 模块数 |
| `tools` | `apps/hiveweave-py/src/hiveweave/tools/*.py` | 匹配 `@tool(` 装饰器 |
| `real_seconds_per_game_day` | `src/hiveweave/**/*.py` 全量扫描 | `REAL_SECONDS_PER_GAME_DAY = N` 常量 |
| `game_day_description` | 由上派生 | 人类可读描述（如「1 real hour = 1 game day」） |
| `source_loc.backend_python` | `apps/hiveweave-py/src/hiveweave/` | 后端 Python 代码行数 |
| `source_loc.frontend_ts` | `apps/web/src/` | 前端 TS/TSX 代码行数 |
| `source_loc.tests` | `apps/hiveweave-py/tests/` | 测试代码行数 |
| `test_to_source_ratio` | 由上派生 | 测试 / 后端源码 比值 |

### CI 校验机制

CI workflow（`.github/workflows/ci.yml`）的 `backend-lint-test` job 在跑完 mypy / pytest 后追加一步：

```yaml
- name: Verify docs/stats.json is up to date
  run: python scripts/gen-stats.py --check
```

若代码变更后 `docs/stats.json` 没同步更新，CI 会失败并提示：

```
STATS DRIFT DETECTED — run scripts/gen-stats.py to refresh docs/stats.json
```

### 维护流程

1. 改动影响 API 路由数 / 工具数 / 游戏日时长 / 大量 LOC 时，本地跑 `python scripts/gen-stats.py` 刷新 `docs/stats.json` 并一并提交。
2. 文档里需要引用这些数字时，应直接读 `docs/stats.json` 或同步其中数值，不要凭印象手写——这是项目里所有「X 个路由 / Y 个工具」类数字的权威来源。

### 局限

- 行数为「文件物理行数」，包含空行与注释；用于趋势对照而非精确度量。
- `@tool` / 路由装饰器靠正则匹配，遇到非常规写法（如装饰器跨多行且首行不闭合）可能漏计，按当前代码风格可覆盖。

## scan_judgement_field.py

扫描**「判定字段被消费时静默丢失」**的耦合点 —— 换判据字段（文本判据 → 状态列，如
`kind`）时的固定探针。

### 为什么需要它

把某个判定从**文本判据**（标题子串）改成**状态判据**（DB 列）之后，真正的缺陷不是一处
而是一类：凡是把「行的某个子集」交给判定的环节，只要那一步没带上该列，判定就**恒为
「不匹配」**，依赖它的门**静默失效**（不报错、不打日志）。

2026-09-14 实测（#11 把 VERIFY 判定从标题换成 `kind`）：同一形态一次撞见 **4 处**真缺陷，
最严重的一处让「VERIFY 的 reviewer 必须钉在 creator」规则**永不生效**（等于开了自审后门）。

### 三类载体

| 类 | 形态 | 说明 |
|---|---|---|
| **A** | 窄 SELECT 缺列 × 同函数喂判定 | 函数里有 `FROM tasks` 的 SQL 常量、字段表不含目标列，且同函数出现判定调用 |
| **B** | 中间视图缺键 | 本函数用 dict 字面量赋的局部变量直接传给判定，且该字面量无目标键。**最隐蔽**——`draft = {...}` 看着只是搬运字段 |
| **C** | 测试面 fixture 缺键 | dict 字面量 `title` 以标记开头却无目标键 ⇒ 断言走「非匹配」分支（**假绿**） |

### 用法

```bash
python scripts/scan_judgement_field.py                    # 默认 --field kind --marker VERIFY
python scripts/scan_judgement_field.py --field kind --marker VERIFY
python scripts/scan_judgement_field.py --kinds A B        # 只跑指定类别
```

### 判据性质与边界

- 判的是**数据流缺列**（状态），**不是文案匹配** —— 与项目全局纪律一致。
- **退出码恒为 0：这是盘点工具，不是 gate。** A/C 类含已知假阳性（窄行只用于读
  `status`/`assignee_id`；测试**显式 stub 了判定**，或故意造「无该列」的负向对照）
  ⇒ **逐条人核**。做成 gate 的唯一后果是训练人「红了就 append」。
- 同类纪律见 `ratchet_positive_controls.py`。

