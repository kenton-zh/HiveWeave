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
