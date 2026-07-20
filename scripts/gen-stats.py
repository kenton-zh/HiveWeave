#!/usr/bin/env python3
"""扫描代码库生成项目统计 JSON，作为文档数字的单一事实源。

用法：python scripts/gen-stats.py [--check]
  --check: 与 docs/stats.json 对比，漂移则 exit 1（CI 校验用）
  默认: 写入 docs/stats.json 并打印人类可读摘要
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "apps/hiveweave-py/src/hiveweave/api"
TOOLS_DIR = ROOT / "apps/hiveweave-py/src/hiveweave/tools"
SRC_DIR = ROOT / "apps/hiveweave-py/src/hiveweave"
TESTS_DIR = ROOT / "apps/hiveweave-py/tests"
WEB_SRC = ROOT / "apps/web/src"

# 路由装饰器正则
ROUTE_RE = re.compile(r'@(?:router|app)\.(get|post|put|delete|patch|websocket)\s*\(')
# @tool 装饰器正则
TOOL_RE = re.compile(r'^@tool\s*\(\s*$|^@tool\s*\(\s*"', re.MULTILINE)
# REAL_SECONDS_PER_GAME_DAY 常量
GAME_DAY_RE = re.compile(r'REAL_SECONDS_PER_GAME_DAY\s*=\s*(\d+)')


def count_routes():
    """统计 API 路由数。"""
    count = 0
    modules = 0
    for f in API_DIR.glob("*.py"):
        if f.name == "__init__.py":
            continue
        text = f.read_text(encoding="utf-8")
        matches = ROUTE_RE.findall(text)
        if matches:
            modules += 1
            count += len(matches)
    return count, modules


def count_tools():
    """统计 @tool 装饰器数量。"""
    count = 0
    for f in TOOLS_DIR.glob("*.py"):
        if f.name.startswith("__"):
            continue
        text = f.read_text(encoding="utf-8")
        count += len(TOOL_RE.findall(text))
    return count


def get_game_day_seconds():
    """读取 REAL_SECONDS_PER_GAME_DAY 常量。"""
    for f in SRC_DIR.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        m = GAME_DAY_RE.search(text)
        if m:
            return int(m.group(1))
    return None


def count_loc(path, patterns=("*.py",), exclude_test=False):
    """统计目录下代码行数。

    exclude_test=True 时排除 .test.ts/.spec.ts/.test.tsx 等测试文件，
    用于前端源码 LOC 统计（测试代码单算）。
    """
    total = 0
    for pattern in patterns:
        for f in path.rglob(pattern):
            if "__pycache__" in f.parts or ".venv" in f.parts:
                continue
            if exclude_test:
                name = f.name
                if ".test." in name or ".spec." in name or name == "test-setup.ts":
                    continue
            try:
                total += sum(1 for _ in f.read_text(encoding="utf-8").splitlines())
            except (UnicodeDecodeError, OSError):
                pass
    return total


def main():
    routes, modules = count_routes()
    tools = count_tools()
    game_day = get_game_day_seconds()
    src_loc = count_loc(SRC_DIR)
    test_loc = count_loc(TESTS_DIR)
    web_loc = count_loc(WEB_SRC, ("*.ts", "*.tsx"), exclude_test=True)
    web_test_loc = 0
    for f in WEB_SRC.rglob("*"):
        if f.is_file() and (".test." in f.name or ".spec." in f.name or f.name == "test-setup.ts"):
            try:
                web_test_loc += sum(1 for _ in f.read_text(encoding="utf-8").splitlines())
            except (UnicodeDecodeError, OSError):
                pass

    stats = {
        "api_routes": routes,
        "api_modules": modules,
        "tools": tools,
        "real_seconds_per_game_day": game_day,
        "game_day_description": f"1 real hour = 1 game day" if game_day == 3600 else f"{game_day} seconds = 1 game day",
        "source_loc": {
            "backend_python": src_loc,
            "frontend_ts": web_loc,
            "tests": test_loc,
            "frontend_tests": web_test_loc,
        },
        "test_to_source_ratio": round((test_loc + web_test_loc) / (src_loc + web_loc), 3) if (src_loc + web_loc) else None,
    }

    out_path = ROOT / "docs" / "stats.json"
    if "--check" in sys.argv:
        if not out_path.exists():
            print("stats.json missing — run gen-stats.py first")
            sys.exit(1)
        old = json.loads(out_path.read_text(encoding="utf-8"))
        if old != stats:
            print("STATS DRIFT DETECTED — run scripts/gen-stats.py to refresh docs/stats.json")
            print(f"  old: {old}")
            print(f"  new: {stats}")
            sys.exit(1)
        print("stats.json up to date")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Generated docs/stats.json:")
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
