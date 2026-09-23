"""守卫：测试期**禁止**使用生产 meta 库。

为什么需要（2026-09-23 独立审计 P1）：
    病灶是 `test_modules_tree` / `test_memory_sinking` 的 `_make_project()` 直接
    ``INSERT INTO projects``，而 meta DB 默认路径 = 生产库，测试结束只关连接、
    从不删行 ⇒ 每跑一次 pytest 永久插一行，实测累积 **1137 行**（后端启动被拖到
    ~140 秒）。

    修法是在 `conftest.py` 里加 `_isolate_meta_db` 夹具。但**那个夹具本身没有任何
    用例守着**：被改名、被改 scope、被新 conftest 覆盖、被谁加一句 early return，
    都不会有红灯 —— 只会再过几百次 CI 后由运维发现"启动又变慢了"。

    也就是说：这个 bug 之所以能累积到 1137 行，根因正是**它在测试里不可观测**。
    本文件就是把"不可观测"变成"可观测"的那两行断言。

判据是**路径**（状态判据），不是感觉、不是文案。
"""

from __future__ import annotations

from pathlib import Path


def _production_meta_db() -> Path:
    """生产 meta 库的绝对路径（apps/hiveweave-py/data/hiveweave.db）。"""
    # 本文件在 apps/hiveweave-py/tests/ ⇒ parents[1] = apps/hiveweave-py
    return (Path(__file__).resolve().parents[1] / "data" / "hiveweave.db").resolve()


def test_meta_db_is_not_the_production_db() -> None:
    """当前测试进程用的 meta 库**不能**是生产库。"""
    from hiveweave.config import settings

    current = Path(settings.get_meta_db_path()).resolve()
    prod = _production_meta_db()
    assert current != prod, (
        f"测试正在使用生产 meta 库：{current}\n"
        "conftest 的 _isolate_meta_db 夹具没生效（或被删/改名/改了作用域）。\n"
        "继续跑会把测试项目永久写进生产库，累积后拖慢后端启动。"
    )


def test_meta_db_lives_under_tmp() -> None:
    """更严一档：路径必须落在临时目录下（连"换个非生产库但仍污染"的形态也拦住）。

    判据取 `tempfile.gettempdir()` —— 与 pytest 的 tmp_path 同源，且不依赖
    具体盘符/大小写（Windows 上 `%TEMP%` 常在 AppData 下）。
    """
    import tempfile

    from hiveweave.config import settings

    current = Path(settings.get_meta_db_path()).resolve()
    tmp_root = Path(tempfile.gettempdir()).resolve()
    assert current.is_relative_to(tmp_root), (
        f"测试用的 meta 库不在临时目录下：{current}（临时根 = {tmp_root}）"
    )


def test_isolation_fixture_still_exists() -> None:
    """夹具必须仍然存在 —— 用于在被删/改名时给出**明确**的报错。

    为什么只断言"存在"、不断言 `autouse`（2026-09-23 初版踩过）：
    `autouse` 只能从 pytest 私有 API ``fn._pytestfixturefunction.autouse`` 读，
    那个字段在新版 pytest 里已不稳定（初版这条实测就红了）。而"夹具是否真的
    autouse 生效"**已经由上面两个测试直接证明** —— 它一旦不生效，那两个测试
    立刻会指向生产库而红。所以这里只需要一个更友好的失败信息，不必重复判据。
    """
    from tests import conftest  # type: ignore[import-not-found]

    assert getattr(conftest, "_isolate_meta_db", None) is not None, (
        "conftest 里找不到 _isolate_meta_db —— meta DB 隔离已被删除。\n"
        "删掉它会让 pytest 重新开始往生产库写垃圾项目（历史上累积到 1137 行，"
        "把后端启动从 12 秒拖到 140 秒）。"
    )
