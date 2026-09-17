"""Failure-signature broadcast (F10 — 平台修复计划 2026-08-30).

背景（r4 + 四轮报告）：平台建了三层共享空间，四轮下来**零使用**。可机检的
后果 = 同一失败签名被多个 Agent 各自独立撞到：``unix-only…`` 被 4 个
Agent 撞到、``No worktree branch…`` 被 2 个 Agent 撞到（回归 R7）。

方案：某 Agent 撞到**新**失败签名时，连同根因提示写入项目共享空间
（scope='project' 的 memories，type='failure_signature'），供同项目其他
Agent 在同类调用**前**检索（工具调用前置检查注入 —— 与 F8 的重复检测共用
签名哈希，不依赖 Agent 主动去查）。

设计约束：
- 签名 = 失败 error 的规范化前缀（截断 + 空白归一），至少 12 字符才可写
  （太短的 error 缺信息量，写进去只会制造噪音）。
- 只写「新」签名：DB 里已存在同签名条目（同 project）则只更新时间戳，
  不重复新增（save_memory upsert 由 module_id 保证）。
- best-effort：共享空间写失败只记日志，绝不影响工具执行。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: 签名长度下限 —— 低于此阈值不写入共享空间（info 不足，易误伤）。
_MIN_SIG_LEN = 24
#: 签名截断上限（存储友好 + 前缀可检索）。
_MAX_SIG_LEN = 160

_WS_RE = re.compile(r"\s+")

# ── #5（fixplan-16items）：签名身份 = 错误类别，不是错误原文 ─────────────
# 「空白归一 + 截断后的原文」会把每次生成都不同的噪声（uuid / 时间戳 /
# 绝对路径里的用户名盘符）当进身份 ⇒ 同类错误记成多条互异签名（实测
# 31 条零重复），R7 因此算出 0 的假阴性。下面四类噪声在进身份前剥掉。
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_HEX32_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_EPOCH_MS_RE = re.compile(r"(?<![0-9])1[0-9]{12}(?![0-9])")
# Windows 绝对路径（含盘符）；跨机不稳定的部分是「盘符 + 用户名」，
# 剥到「路径尾两段」——既消灭用户名/盘符，又保留错误里最有信息量的文件名。
#
# ⚠ 字符类**必须容忍 `<` `>`**（0-4 审计 D1 实测）：本模块的占位符形态是
# `<uuid>`/`<hash>`/`<agent>`/`<id8>`，它们**内含尖括号**；而原来的类把
# `<>` 排除了 ⇒ 路径在占位符处被截断、匹配回退 ⇒ **"剥到尾两段"这个不变式
# 在含占位符的绝对路径上不成立**，表现为盘符/用户名段回流：
#   `D:\Temp\<id8>\out.txt` 旧行为 → `D:/Temp/<id8>/out.txt`（盘符回流）。
# 这是 `468bf79` 起就存在的洞（`<uuid>`/`<hash>` 同样踩），0-4 把触发面扩大到
# agent 短号与 8 位 id 后一并收口。
_WIN_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/](?:[^\\/:*?\"|\r\n]+[\\/])*[^\\/:*?\"|\r\n]+"
)
# UNIX 路径同样：段字符类里补 `<>`，否则绝对 unix 路径上的占位符会截断匹配。
_UNIX_PATH_RE = re.compile(r"(?<![\w.@])/(?:[\w.@+-<>]+/)*[\w.@+-<>]+")

# ── 0-4（2026-09-16，实测驱动）：再剥两类「平台自产的标识形状」 ──────────
#
# 为什么必须补：`468bf79` 剥了 uuid/时间戳/hash/路径之后，**同一根因跨 agent
# 仍被判成不同身份** —— 全量重放 `scripts/replay_failure_signatures.py` 实测
# 58 项目：144 条失败 → 85 个签名，其中 **3 组同根因被拆成多个签名**，现场就是：
#   · `命令指向 worktree A075/A076/A077（不是你所在的树）` → 3 个签名
#   · `merging main into hw/A074|A076|A077/work would conflict` → 3 个签名
#   · `.hiveweave/reports/<8位id>` → 每个 task 一个签名
# 这三类里的 `A0xx` 与 8 位十六进制**都是平台自己生成的标识**，
# 与"根因"正交 ⇒ 必须从身份里拿掉。
#
# ⚠ 这是**剥标识形状**，不是"补词表"：判据是「平台会不会生成这个形状」
# （agent 短号 = `A` + 3 位数字，见 `_SHORT_ID_RE`；8 位十六进制 = task id
# 前 8 位，见 `_TASK_BRANCH_RE` 的 `t-([0-9a-fA-F]{8})`），与措辞/语言无关。
# 措辞本身**不剥** —— 两条 stdout 不同的 pwsh 失败是不同的根因，
# 合并它们等于把共享条目变成大杂烩（本仓明写「错解比无解更贵」）。
_AGENT_SHORT_ID_RE = re.compile(r"(?<![A-Za-z0-9])A\d{3}(?:-[a-d])?(?![0-9A-Za-z])")
# 批 C（2026-09-18，#5 应剥未剥）：短号含 worktree 重定位后缀 ——
# `A075-b`/`A075-c` 是**同一个逻辑 agent** 重定位后的形态（§8.8 实测
# 发生 10/9 次），后缀不剥会把同根因**拆成多条**。正则吞掉 `-b..-d` 后缀，
# 与裸短号同归 `<agent>`。
# 8 位十六进制：**恰好 8 个**十六进制字符（前后不得再有十六进制字符）。
# ⚠ 为什么**不**要求"至少含一个 a-f"：task id 是 uuid 前 8 位，**纯数字是合法
# 取值**（概率约 2%）。曾按"含字母"收窄，实测立刻漏掉一条
# `hw/<agent>/t-91765492`（纯数字 task id）⇒ 同根因又被拆开。
# 实测（审计逐 token 查上下文）：58/59 被咬的 23+27 个 token **全部**是
# uuid 前缀（`attestation_id=` / `taskId=` / `.hiveweave/reports/<id>`…），
# 零误咬；两类纯数字的（`91765492`/`51861384`）也确是真 task id。
#
# ⚠ **取舍已显式接受**：`[0-9a-fA-F]{8}` 含纯十进制 ⇒ 恰好 8 位的十进制量
# （`expected 32774618 bytes`）与紧凑日期（`20260916`）会被替成 `<id8>`。
# 判定理由是代价不对称：一个 8 位量被测成"同一条"只损失一点精度，
# 而 task id 漏替会让同一根因**永久**拆成多条。此取舍由
# `test_eight_digit_decimal_is_intentionally_eaten` 钉住（不是口头承诺）。
# 注：7 位与 40 位十六进制**不**替 —— 前者是 git 短 sha 的常见形态、
# 后者由既有用例声明为「内容稳定标识，不归并」；长度差异是有意的。
_TASK_ID8_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}(?![0-9a-fA-F])")
# 占位符（用于"信息量"判定：占位符不计入有效长度）。
_PLACEHOLDER_RE = re.compile(r"<[a-z0-9]+>")

# ── 批 C（2026-09-18，#5 应剥未剥的其余结构化形状）────────────────
# ⚠ 全部是「平台/运行时自产的变化量」形状，与措辞正交 —— 与上面同一判据，
# 不是往词表里堆词。**登记在案不补的**：非十六进制随机 id（无稳定形状可
# 判）；随机 PID/10 位 epoch 秒（无稳定形状）。⚠ 冒号挂接的端口/PID
# （`localhost:8000` / `pid:12345`）由 _LINE_NO_RE **连带覆盖**（同归
# :<ln>）—— 行为方向一致（都是运行时变化量），但口径上是连带非显式
# （批 C 审计 LOW 订正）。
# 行号/列号：`file.py:123` / `file.py:123:45` —— 测试失败定位是高频噪声。
# 时间 "12:34:56" 会被部分吞掉 —— 那本就是易变量，方向一致。
_LINE_NO_RE = re.compile(r"(?<=\w):\d{1,5}(?!\d)")
# 时长 / 容量 / token 数：`1.2s` / `345ms` / `2 min` / `12.3 MB` / `1234 tokens`。
# ⚠ 只剥**数字**保留单位（审计 LOW：容量上限常是配置值——"exceeded
# 512 KB limit" 与 "exceeded 5 MB limit" 可能是不同根因，合并 = 错解温床）；
# CJK 分支不用 \b（\b 落不到 CJK 与数字之间，「等待30秒」会漏——审计 LOW）。
_DURATION_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)(\s?(?:ms|s|sec|secs|min|mins|KB|kB|MB|GB))\b|"
    r"(\d+(?:\.\d+)?)(\s?(?:分钟|秒))(?![0-9A-Za-z])",
    re.IGNORECASE,
)
_TOKEN_COUNT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?tokens?\b", re.IGNORECASE)
# 临时目录随机名：mkdtemp 真实形态 = `tmp` + **恰好 8 位**小写/数字/下划线
# （审计 LOW：{6,}+大小写不敏感会误吃 `tmpDirectory` 这类驼峰标识符）。
_TMP_DIR_RE = re.compile(r"\btmp[a-z0-9_]{8}\b")


def _dur_sub(m: "re.Match[str]") -> str:
    return "<n>" + (m.group(2) or m.group(4) or "")


def _keep_path_tail(match: re.Match) -> str:
    """绝对路径 → 尾两段（文件名 + 直接父目录）。"""
    parts = re.split(r"[\\/]", match.group(0))
    return "/".join(parts[-2:])


def signature_of(error: str | None, root: str | None = None) -> str | None:
    """规范化失败签名：剥噪声（uuid/时间戳/哈希/路径/**平台标识**）+ 空白归一 + 截断。

    ``root`` 给定时，项目根前缀的路径先归一为 ``./`` 相对形态（#5 采纳的
    收窄方向：**直接项目根相对**，不要"相对→绝对→根相对"三步）；随后剩余
    绝对路径剥到尾两段。``root`` 不给（或解析失败）时只做通用剥离 —— 两次
    调用只要同参就同结果，写侧与查侧必须传同样的 root 才能对上签名。

    返回 None = 无有效信息（不广播）。

    ⚠ **仍是"文本判据"的一份**（如实登记，别读成已清干净）：身份由错误原文
    归一而成，只是把**平台自产、与根因正交的标识形状**拿掉了
    （uuid / 时间戳 / 32 位与 8 位十六进制 / agent 短号 / 绝对路径里的
    用户名盘符）。**措辞本身不剥** —— 两条 stdout 不同的 pwsh 失败是不同的
    根因，合并它们会让共享条目变大杂烩（本仓明写「错解比无解更贵」）。
    因此「同一根因」的判定精度受限于措辞差异：换了说法/换了语言的同一根因
    仍会算成两个身份。彻底的做法是改用**结构化身份**（tool + 事实位），
    但那要求事实位先可用 —— 当前 144 条失败里绝大多数位是 `unclassified`
    （= #15 残余 E19 那条），现在就切会把身份塌成"只剩 tool"，属于过度合并。

    ⚠ **算法一旦改动，库里既有条目的签名即失配** —— 但**不是"全部不再命中"**
    （0-4 审计实测订正了这条：初稿写的是"老条目不再被新查询命中"，与实现相反）：
    定位判据是 ``f"| {sig}" in first_line or sig[:48] in first_line`` ——
    **前缀支``sig[:48]``仍会命中相当一部分老条目**（实测 58 = 21 例 / 59 = 31 例
    是"整串不等但前缀命中"），这也正是 59 里 6/7 条「已验证解法」得以存活的原因。
    真正搁浅的是**两个判据都命中不了**的那批：它们的解法行再也读不到、
    `known_signature_hint` 对它恒返回 None —— 实测 59 = **1/7**。
    ⇒ 失配集中在两处**静默**出口（`known_signature_hint` 与
    `note_distinct_hitter` 未命中即 return，无日志）；本函数无法在单点解决，
    处置落在：① 本节如实写明口径；② `scripts/replay_failure_signatures.py`
    报出"库里签名已被算法变更搁浅的条目数"；③ 不改用"兼容旧签名"的方式绕
    （那会变成两份判据）。
    """
    if not error or not error.strip():
        return None
    sig = error.strip()
    if root:
        escaped = re.escape(root.rstrip("\\/"))
        sig = re.sub(
            # (?![\w-])：不吃兄弟目录前缀（root="D:\w\proj1" 不得匹配
            # "D:\w\proj1-archive\..."）—— Windows 盘符大小写不敏感
            escaped + r"(?![\w-])", ".", sig, flags=re.IGNORECASE
        )
    sig = _ISO_TS_RE.sub("<ts>", sig)
    sig = _EPOCH_MS_RE.sub("<ts>", sig)
    sig = _UUID_RE.sub("<uuid>", sig)
    sig = _HEX32_RE.sub("<hash>", sig)
    # 0-4：平台自产标识形状（agent 短号 / 8 位十六进制 id）——
    # 顺序在 uuid/hex32 **之后**：32 位十六进制已被上面吃掉，这里不会再咬它。
    sig = _AGENT_SHORT_ID_RE.sub("<agent>", sig)
    sig = _TASK_ID8_RE.sub("<id8>", sig)
    sig = _WIN_PATH_RE.sub(_keep_path_tail, sig)
    sig = _UNIX_PATH_RE.sub(_keep_path_tail, sig)
    # 批 C（2026-09-18）：在路径归一**之后**应用 —— 路径规则把
    # `D:\x\file.py:123` 收成 `x/file.py:123`（含行号尾巴），行号规则再吃掉
    # `:123`；时长/容量/token 数与临时目录随机名随后。
    sig = _LINE_NO_RE.sub(":<ln>", sig)
    sig = _TOKEN_COUNT_RE.sub("<tok>", sig)
    sig = _DURATION_RE.sub(_dur_sub, sig)
    sig = _TMP_DIR_RE.sub("<tmp>", sig)
    sig = _WS_RE.sub(" ", sig)
    if len(sig) < _MIN_SIG_LEN:
        return None
    # 0-4（审计 D4）：**占位符不计信息量**。
    # 门槛原本只看总长，而归一化会把标识**换成长度不同的占位符**
    # （`A075`(4) → `<agent>`(7)）⇒ `"A075 A076 A077 A078"` 这种**信息量为零**
    # 的输入会被膨胀到 31 字符而**开始广播**（旧实现正确地返回 None）。
    # 反向也可能：`<id8>`(6) 比 8 位原文短 ⇒ 真实签名跌破门槛而**不再广播**。
    # 故门槛落在"剥掉占位符后仍有多少内容"上。实测 58/59 真实语料影响 **0 条**。
    if len(_PLACEHOLDER_RE.sub("", sig)) < _MIN_SIG_LEN:
        return None
    return sig[:_MAX_SIG_LEN]


def _deep_key_sort(value: Any) -> Any:
    """递归按 key 排序 dict（list 保序，元素递归排序）。

    只做**键顺序**归一，不做任何值归一 —— 顺序不同不得洗白（批次 4 纪律）。
    """
    if isinstance(value, dict):
        return {
            k: _deep_key_sort(value[k])
            for k in sorted(value.keys(), key=lambda x: str(x))
        }
    if isinstance(value, list):
        return [_deep_key_sort(v) for v in value]
    return value


def canonicalize(args: Any) -> str:
    """参数规范化：深 key-sort 后 JSON stringify（无空白）。

    与 DSH ``canonicalize`` 同义：**只有键顺序被归一**，值本身（含字符串内
    空白、大小写、等价但不相同的写法）一律保留 —— 顺序不同不得洗白。
    ``None`` / 非 dict 输入按自身 stringify（``default=str`` 兜底）。
    """
    try:
        return json.dumps(
            _deep_key_sort(args if args is not None else {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:  # noqa: BLE001 — 不可序列化对象降级为 str 快照
        return str(args)


def call_identity(tool_name: str, args: Any) -> str:
    """**调用身份** —— 待回填问题的身份键（批次 4 #11）。

    ``tool_name::{canonicalize(args)}``。绑定的是**操作身份**，不是"工具"：
    ``(agent_id, tool_name)`` 会把「工具的下一次成功」当成「同一个问题的解」
    ——实测 ``read_file docs/spec.md``（不存在）回填到 ``{filePath:
    scripts/main.gd, offset: 440}``，条目从"镜子"恶化成"错解"。

    硬性不变式（入口）：
    1. pending 键必须含本函数返回值 —— 不许退化为 ``(agent_id, tool_name)``；
    2. 判定与存储用**同一个** ``canonicalize``（顺序不同 = 不同调用，不洗白）；
    3. 一次失败只对应一个身份，回填必须在同身份的成功上兑现。
    """
    return f"{(tool_name or '').strip()}::{canonicalize(args)}"


def make_module_id(project_id: str, sig: str, tool_name: str | None = None) -> str:
    """memories.module_id —— 按 (project, sig, tool) 稳定，upsert 保证去重。

    把 project_id 纳入哈希，防止不同项目撞同一签名文本时跨项目去重
    （save_memory 的 upsert 键是 (agent_id, scope, module_id)）。

    批 C / F9-A（2026-09-18）：**tool_name 纳入 module_id** —— 此前按
    (project, sig) 两元组，同一签名文本由工具 B 再次撞到时会 upsert
    **覆盖**工具 A 的那一行（首行 tool= 被改写，此后 A 的解法回填因
    首行 tool 失配恒 `backfill_no_entry`）。三元组后不同工具**并存为
    多行**。⚠ 数据级后果（批 C 独立提交的理由）：存量行的 module_id
    不含 tool ⇒ 新写入视为新条目，旧行从此搁浅（解法行读不到）——
    这是**已拍板接受**的代价（搁浅量由 replay 脚本报出），不做兼容
    双判据（那会变成两份判据）。
    """
    tool_part = (tool_name or "").strip()
    return (
        f"failure_sig::{project_id}::"
        f"{hashlib.sha256(f'{tool_part}|{sig}'.encode('utf-8', errors='replace')).hexdigest()[:16]}"
    )


#: 失败签名行使用固定写入方 —— save_memory 的 upsert 键是
#: (agent_id, scope, module_id)。若用真实 agent_id，同签名被 Agent B
#: 再次撞到时会因 upsert 键不同而**再插一行**（跨 agent 去重失效，且放大
#: 共享空间膨胀）。固定写入方使 upsert 键 (fixed, project, sig) 对全项目
#: 稳定 —— 同签名只存一行，首个撞到的 agent 记在 source_agent_id 字段。
_SIGNATURE_WRITER = "__failure_signature_pool__"

#: 签名条目保留上限（P2 复审 2026-08-30）：签名单调累积，compaction/
#: archive 只碰 scope='agent'，永不清理签名行。get_project_memories
#: LIMIT 100，签名逼近上限会物理挤掉合法 constitution 条目。写入时
#: 顺带裁剪到该上限（只删最老签名行，best-effort）。
_SIGNATURE_MAX_ROWS = 50


async def _trim_signature_rows(project_id: str) -> None:
    """删最老签名行到 _SIGNATURE_MAX_ROWS 以内（幂等 best-effort）。"""
    try:
        from hiveweave.db import project as project_db
        from hiveweave.services.memory import get_workspace_write_lock
        from hiveweave.db import meta as meta_db

        workspace = await meta_db.get_project_workspace(project_id)
        if not workspace:
            return
        lock = await get_workspace_write_lock(workspace)
        async with lock:
            conn = await project_db.ensure_project_db(workspace)
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cur = await conn.execute(
                    "SELECT COUNT(*) AS n FROM memories "
                    "WHERE scope = 'project' AND type = 'failure_signature'"
                )
                row = await cur.fetchone()
                await cur.close()
                total = int(row["n"] or 0) if row else 0
                excess = total - _SIGNATURE_MAX_ROWS
                if excess > 0:
                    cur = await conn.execute(
                        "SELECT id FROM memories "
                        "WHERE scope = 'project' AND type = 'failure_signature' "
                        "ORDER BY created_at ASC, rowid ASC LIMIT ?",
                        [excess],
                    )
                    ids = [r["id"] for r in await cur.fetchall()]
                    await cur.close()
                    if ids:
                        ph = ",".join("?" * len(ids))
                        await conn.execute(
                            f"DELETE FROM memories WHERE id IN ({ph})", ids
                        )
                await conn.commit()
                # P3（边界审计 2026-08-30）：trim 删行后失效 project 记忆
                # 缓存 —— 否则 get_project_memories 30s 快照仍含已删签名，
                # known_signature_hint 30s 内对已删签名仍返回 shared-fix。
                try:
                    from hiveweave.services.memory import MemoryService

                    MemoryService.invalidate(
                        project_id, scope="project"
                    )
                except Exception:
                    pass
            except Exception:
                try:
                    await conn.rollback()
                except Exception:
                    pass
                raise
    except Exception as e:
        log.warning("failure_signature.trim_failed", error=str(e))


async def _project_root_of(project_id: str) -> str | None:
    """项目根路径（给 ``signature_of`` 的路径归一用；best-effort ⇒ None）。

    ⚠ **写侧（``record_failure_signature``）与查侧（``known_signature_hint``）
    必须经由同一个解析函数** ⇒ 同一 project_id 永远得到同一 root（或同
    None），两次 ``signature_of`` 才能对上同一签名 —— 一侧带 root 一侧不带
    就是新的「同一事实两处判」。
    """
    try:
        from hiveweave.db import meta as meta_db

        return await meta_db.get_project_workspace(project_id)
    except Exception:
        return None


async def record_failure_signature(
    *,
    project_id: str | None,
    agent_id: str,
    tool_name: str,
    error: str | None,
    attribution: str = "",
) -> dict:
    """把新失败签名写入项目共享空间（R7 → 0 的可机检支撑）。

    Returns ``{"written": bool, "preexisting": bool, "preexisting_source":
    str|None, "sig": str|None}``：``preexisting``=该签名在本次失败**之前**
    已存在（39 审计 P1-3：首撞者不该收"先读它"自指提示——executor 据此
    门控 hint）；``preexisting_source``=首撞者；``sig``=**写侧实际使用的
    规范化签名**（带 root 归一）。executor 侧的 pending/自指去重/组织升级
    必须**复用这个 sig**，不得自己再调 ``signature_of(error)`` —— 那会算出
    不带 root 的另一份签名，与写侧记忆行失配（同一事实两处判）。
    best-effort。
    """
    if not project_id:
        return {
            "written": False,
            "preexisting": False,
            "preexisting_source": None,
            "sig": None,
        }
    sig = signature_of(error, root=await _project_root_of(project_id))
    if sig is None:
        return {
            "written": False,
            "preexisting": False,
            "preexisting_source": None,
            "sig": None,
        }
    try:
        from hiveweave.services.memory import MemoryService

        memory_service = MemoryService()
        # 39 审计 P1-3（签名自指 8 连发）：首撞者不该收到"先读它"提示——
        # 条目内容就是自己 2 秒前写的错误原文。先查签名是否**早已存在**及
        # 其首撞者，调用方（executor）据此门控 hint：只给"别人的坑"发提示。
        preexisting = False
        preexisting_source: str | None = None
        # 件1（回填处置 2026-09-05）：rehit 时旧条目若已回填「已验证解法:」行，
        # 原样携带到新内容 —— 否则本次覆写会把刚回填的解法冲掉（回填白做，
        # hint 恢复也随之失效）。
        carried_solution_line: str | None = None
        prev_meta: dict = {}
        try:
            for m in (await memory_service.get_project_memories(project_id)) or []:
                if m.get("type") != "failure_signature":
                    continue
                fl = (m.get("content") or "").split("\n", 1)[0]
                if fl.startswith("[失败签名]") and (
                    f"| {sig}" in fl or sig[:48] in fl
                ):
                    preexisting = True
                    preexisting_source = (
                        str((m.get("metadata") or {}).get("source_agent_id") or "")
                        or None
                    )
                    prev_meta = m.get("metadata") or {}
                    # 批 C 审计 MEDIUM（2026-09-18）：解法行携带**按 tool 门控**
                    # —— F9-A 后不同工具并存为独立行，工具 B 撞到工具 A 的同
                    # 签名时不再覆盖 A 的行，但如果把 A 的「已验证解法」原样
                    # 携带进 B 的新行，B 自己后续的真实解法回填会因
                    # _has_verified_solution_line 被幂等跳过 —— 等于从写入侧
                    # 把「错解」种进 B 的行。preexisting/hit_count 保持签名级
                    # （撞到同签名文本就是 rehit，不改 P1-3 自指门语义）。
                    for _line in (m.get("content") or "").splitlines():
                        if _line.startswith(_SOLUTION_LINE_PREFIX) and _first_line_matches_tool(
                            _line, tool_name
                        ):
                            carried_solution_line = _line
                            break
                    break
        except Exception:  # noqa: BLE001 — 前查失败按"新签名"处理
            pass
        content = (
            f"[失败签名] tool={tool_name or '?'} | {sig}\n"
            f"根因提示: {attribution or '见错误原文'}\n"
            f"原文尾(含等价写法/修复线索): {(error or '').strip()[-320:]}\n"
            f"首个撞到的 Agent: {agent_id}"
            f"（撞到该签名后请先检索本项目共享空间是否已有解法）"
        )
        if carried_solution_line:
            _lines = content.split("\n")
            _at = len(_lines)  # 兜底：找不到根因行则追加末尾（绝不插首行前，
            # 否则破坏 [失败签名] 首行匹配）
            for _i, _ln in enumerate(_lines):
                if _ln.startswith("根因提示:"):
                    _at = _i + 1
                    break
            _lines.insert(_at, carried_solution_line)
            content = "\n".join(_lines)
        now_ms = int(time.time() * 1000)
        module_id = make_module_id(project_id, sig, tool_name)
        metadata = {
            "kind": "failure_signature",
            "signature": sig,
            "tool_name": tool_name or "",
            "first_hit_at_ms": now_ms,
        }
        if preexisting:
            # 回填溯源字段随 rehit 保留（solved_at/solution_tool + 状态位）——
            # 批 C 审计 MEDIUM：**仅当解法行被携带**（tool 门控通过）时才继
            # 承状态位 —— 否则 B 的行会「状态位=verified 但无解法行」，同样
            # 违反下方的机检不变式（只是反方向）。
            if carried_solution_line:
                for _k in ("solved_at_ms", "solution_tool", "solution_status"):
                    if _k in (prev_meta or {}):
                        metadata[_k] = prev_meta[_k]
            metadata["hit_count"] = int((prev_meta or {}).get("hit_count") or 1) + 1
        else:
            # #16-② 状态位显式落 ``none``：机检口径是
            # ``content LIKE '%已验证解法:%' AND solution_status != 'verified'``
            # 必须为 0（「有解法行」与「状态位」不许分叉）。新条目没有解法行，
            # 但写 ``none`` 让"该字段存在且语义明确"可被断言，而不是靠"字段
            # 缺失"推断 —— 缺失既可能是"还没回填"，也可能是"写入方忘了写"。
            metadata["hit_count"] = 1
            metadata.setdefault("solution_status", SOLUTION_STATUS_NONE)
        # 首撞者写入 metadata（自指抑制门的读取源）：此前只在列里存最新
        # 撞到者、metadata 不落 → 首撞者信息在 rehit 后丢失，门退化为
        # 永远放行（hint 又指回自己刚写的条目）。首撞者须跨 rehit 稳定。
        metadata["source_agent_id"] = (
            (prev_meta or {}).get("source_agent_id") if preexisting else None
        ) or agent_id
        # scope='project' 是团队共享层 —— 全员可见（Project Constitution 注入）。
        # agent_id 用固定写入方：跨 agent 去重（同一签名全项目只存一行）。
        mem_id = await memory_service.save_memory(
            agent_id=_SIGNATURE_WRITER,
            project_id=project_id,
            scope="project",
            content=content,
            type="failure_signature",
            module_id=module_id,
            source_agent_id=agent_id,
            metadata=metadata,
        )
        log.info(
            "failure_signature.broadcast",
            project_id=project_id,
            agent_id=agent_id[:12],
            tool=tool_name,
            sig=sig[:60],
            mem_id=mem_id,
        )
        # P2 复审：写入顺带裁剪最老签名行到上限（防单调累积挤掉合法
        # constitution；best-effort 不阻断）。
        try:
            await _trim_signature_rows(project_id)
        except Exception as trim_err:  # noqa: BLE001 — 裁剪 best-effort
            log.warning("failure_signature.trim_failed", error=str(trim_err))
        return {
            "written": True,
            "preexisting": preexisting,
            "preexisting_source": preexisting_source,
            "sig": sig,
        }
    except Exception as e:
        log.warning("failure_signature.broadcast_failed", error=str(e))
        return {
            "written": False,
            "preexisting": False,
            "preexisting_source": None,
            "sig": None,
        }


async def known_signature_hint(
    project_id: str | None, error: str | None, agent_id: str | None = None
) -> str | None:
    """同项目共享空间里是否已有该失败签名 —— 供工具调用前置检查注入。

    Returns ``"[shared fix] …"`` 提示文案或 None（未命中/不可用/自指）。

    **自指抑制（2026-09-01，s3-clone_06）**：F10 的 hook 是「先写签名、后取提
    示」——同一次失败写入的条目会被自己立刻命中，而该条目内容只有错误原文 +
    占位根因（``见错误原文``）。提示它去「先读它」等于指它读自己刚写的一面镜子，
    零信息量，且因为看起来在工作而极难被发现（TEST_DSH_38 实测 18/18 失败步
    全部收到该提示，dev server 同一堵墙连撞 3 次）。

    因此：命中的签名条目**必须携带超出错误原文的信息**（根因提示非占位）才广播。
    只读、best-effort —— 查询失败仅返回 None，绝不阻断工具执行。
    """
    if not project_id:
        return None
    sig = signature_of(error, root=await _project_root_of(project_id))
    if sig is None:
        return None
    try:
        from hiveweave.services.memory import MemoryService

        memory_service = MemoryService()
        mems = await memory_service.get_project_memories(project_id)
        for m in mems or []:
            if m.get("type") != "failure_signature":
                continue
            content = m.get("content") or ""
            first_line = content.split("\n", 1)[0] if content else ""
            if not first_line.startswith("[失败签名]"):
                continue
            # 签名行格式：`[失败签名] tool=xxx | <sig>`
            if f"| {sig}" in first_line or sig[:48] in first_line:
                if not _signature_has_solution(content):
                    log.debug(
                        "failure_signature.hint_suppressed_self_reference",
                        agent_id=(agent_id or "")[:12],
                        sig=sig[:60],
                    )
                    return None
                return (
                    "[shared fix] 团队共享空间已有该失败签名条目 —— 先读它，"
                    "别重复撞同一个坑。"
                )
        # 0-4（审计 M2）：未命中此前**完全静默** —— 而"算法变更导致老条目不可达"
        # 与"这个失败确实是新的"在日志上长得一样，无从分辨。
        # 这里只在**项目里确实存在签名条目**时留痕，并降到 debug：
        # hint 是每次工具调用前都会跑的路径，"未命中"是常态 ⇒ 用 info 会刷屏，
        # 反而让真正要看的东西被淹掉（口径与 `git_hardening_degraded` 一致）。
        n_entries = sum(
            1 for _m in (mems or []) if _m.get("type") == "failure_signature"
        )
        if n_entries:
            log.debug(
                "failure_signature.hint_no_match",
                n_entries=n_entries,
                sig_prefix=sig[:48],
                note="no entry matched — if the signature algorithm changed, "
                "older entries may have become unreachable",
            )
        return None
    except Exception:
        return None


def attribution_of(result: dict) -> str:
    """从工具回执推导一句话归因（供共享签名条目使用）。

    判定顺序按「信息量从具体到笼统」——s3-clone_06 P0-3：方言不兼容必须先
    于 blocked 判定，否则"bash 写法在受限 shell 不认"会被报成"平台护栏拒绝
    （权限/沙箱/安全）"，把撞坑 Agent 指向错误的排查方向（DSH postmortem
    0004：宽泛签名 → 误归因，同构缺陷）。

    ⚠ 位的**读取与优先级**由 ``tools/fact_positions.fact_from_bits()`` **单一
    实现**（#15，2026-09-14：判据只许有一份，免得两处各自演化成"哪份才是漏的"）。
    本函数只负责把事实位翻成**给人看的一句话**（含方言专项文案）。
    """
    try:
        # 延迟 import：`services` → `tools` 的方向只在调用期发生，避免模块级环。
        from hiveweave.tools.fact_positions import fact_from_bits

        kind = fact_from_bits(result)
        if kind == "runner_failed":
            if result.get("dialect_failed"):
                return (
                    "runner_failed: shell 方言不兼容 —— 命令从未执行。"
                    "改写为 pwsh 写法（见错误原文的等价表）或直接调 pwsh 工具；"
                    "不要用不同的 unix flag 重试"
                )
            return "runner_failed: 命令未执行（执行器/方言/权限/审批）"
        if kind == "command_failed":
            return "command_failed: 命令执行了但失败（业务/测试未过）"
        # #15：`bad_args` 被 L6 从 blocked 改判出来后（保留端口等）**没有对应文案**
        # ⇒ 归因退回空串，撞坑 Agent 拿不到方向。补上它。
        # 判据读**事实位**而非文本：位是声明出来的状态，与措辞/语言无关
        # （``bad_args`` 不会出现在布尔位里 —— 布尔位只有 dialect/runner/command，
        # 故这里直接读 ``fact``）。
        if str(result.get("fact") or "") == "bad_args" or kind == "bad_args":
            return (
                "bad_args: 调用方参数错（平台无责）—— 换个参数即可通过，"
                "别用同一组参数重试"
            )
        if result.get("blocked"):
            return "blocked: 平台护栏拒绝（权限/沙箱/安全）"
    except Exception:  # noqa: BLE001 — 归因是旁支，绝不能因它挂掉工具回执
        return ""
    return ""


# ``record_failure_signature`` 写入根因提示时的占位值 —— 表示「没有可用根因，
# 去看错误原文」。条目停留在占位状态 = 它只是错误原文的副本（自指镜子）。
_ROOT_CAUSE_PLACEHOLDER = "见错误原文"


def _signature_has_solution(content: str) -> bool:
    """签名条目是否携带超出错误原文的信息（可指导下一步动作）。

    判定看两处：①「已验证解法:」行（backfill_solution 回填，非空即有解，
    hint 恢复广播）；②「根因提示:」行：缺失 / 空 / 等于占位值 → 无信息量。
    未来若新增结构化解法字段，应在此一并纳入判定。
    """
    has_root_cause = False
    for line in (content or "").splitlines():
        if line.startswith(_SOLUTION_LINE_PREFIX):
            if line.split(":", 1)[1].strip():
                return True
            continue
        if line.startswith("根因提示:"):
            value = line.split(":", 1)[1].strip()
            has_root_cause = bool(value) and value != _ROOT_CAUSE_PLACEHOLDER
    return has_root_cause


# ── 解法回填（42 轮实测处置 2026-09-05）──────────────────
# 18/18 hint 无效的根因：条目常由失败者自己刚写（first_hit 晚 0.008-30s），
# 且只有错误原文没有解法。本节提供「问题解决 → 回填」的写回通道：
# executor 在失败时记 pending，同 agent 同工具随后一次成功即回填成功参数
# 摘要作为解法（纯机械，无 LLM 总结）。

#: 已验证解法行前缀 —— backfill_solution 追加、_signature_has_solution 认可。
_SOLUTION_LINE_PREFIX = "已验证解法:"

#: 解法状态位（#16-②）—— 条目 metadata 上的唯一权威判定：
#: ``none`` = 无已验证解法（占位根因，镜子条目）；``verified`` = 已被
#: backfill_solution 回填并经身份校验。机检口径：
#: ``content LIKE '%已验证解法:%' AND solution_status != 'verified'`` 必须为 0
#: —— 即「有解法行」与「状态位」不允许分叉。
SOLUTION_STATUS_NONE = "none"
SOLUTION_STATUS_VERIFIED = "verified"
_SOLUTION_STATUSES = frozenset({SOLUTION_STATUS_NONE, SOLUTION_STATUS_VERIFIED})

#: 解法文本长度下限 —— 低于此阈值视为无实质内容，不回填（防噪音）。
_MIN_SOLUTION_LEN = 8


def _is_substantive_solution(solution: str | None) -> bool:
    """只接受实质解法：非空、达长度阈值、且非占位文案。

    占位判定语义与 _signature_has_solution 一致 —— 防把「见错误原文」
    当解法回填（回填了等于没回填，还会让 hint 误恢复广播）。
    """
    value = (solution or "").strip()
    if not value or len(value) < _MIN_SOLUTION_LEN:
        return False
    return value != _ROOT_CAUSE_PLACEHOLDER


def _has_verified_solution_line(content: str) -> bool:
    """条目是否已含「已验证解法:」行（幂等回填判定用）。"""
    for line in (content or "").splitlines():
        if line.startswith(_SOLUTION_LINE_PREFIX):
            return True
    return False


def _first_line_matches_tool(first_line: str, tool_name: str) -> bool:
    """首行 ``[失败签名] tool=<name> | <sig>`` 是否属于该工具（#11-(c)）。

    同签名不同工具**必须能区分**：``signature_of`` 会截断长错误文本，
    两条不同错误可能落到同一签名；此时按签名定位会先把解法填进「不是它的」
    条目（错解比无解更贵 —— ``_signature_has_solution`` 会认它并对全员
    广播「先读它」）。``tool_name`` 为空时**不做二次筛选**（调用方没给出
    可供筛选的身份，此时按签名定位是唯一可选行为；不假装做了校验）。
    解析失败（首行没有 ``tool=`` 段）同样放行 —— 历史条目可能早于该格式。
    """
    name = (tool_name or "").strip()
    if not name:
        return True
    head = (first_line or "").split("|", 1)[0]
    marker = "tool="
    idx = head.find(marker)
    if idx < 0:
        return True
    got = head[idx + len(marker):].strip()
    return got == name


async def repair_solution_status(project_id: str) -> int:
    """把「已有解法行但状态位没跟上」的历史条目**补齐**状态位（#16-②）。

    这条路径独立于 ``backfill_solution`` 存在，因为后者有两条合法早退：
    ① 传入的解法不实质（``_is_substantive_solution`` 拒）→ 直接 return；
    ② 条目已有解法行 → 幂等跳过。
    只有 ② 会补状态位，而**最需要补的那批条目恰恰是「已有解法行」的**——
    它们多数由旧代码写入，回填当时还没有状态位字段。若只能靠"再回填一次
    实质解法"触发，那已经被占位拒绝的条目永远补不上，机检口径
    ``content LIKE '%已验证解法:%' AND solution_status != 'verified'``
    会长期非零而无从修复。

    Returns 补齐的条目数。best-effort：任何异常只记日志返回 0。
    """
    if not project_id:
        return 0
    fixed = 0
    try:
        from hiveweave.services.memory import MemoryService

        memory_service = MemoryService()
        for m in (await memory_service.get_project_memories(project_id)) or []:
            if m.get("type") != "failure_signature":
                continue
            content = m.get("content") or ""
            if not _has_verified_solution_line(content):
                continue
            metadata = dict(m.get("metadata") or {})
            if metadata.get("solution_status") == SOLUTION_STATUS_VERIFIED:
                continue
            if not m.get("module_id"):
                continue
            metadata["solution_status"] = SOLUTION_STATUS_VERIFIED
            await memory_service.save_memory(
                agent_id=_SIGNATURE_WRITER,
                project_id=project_id,
                scope="project",
                content=content,
                type="failure_signature",
                module_id=m.get("module_id"),
                source_agent_id=m.get("source_agent_id"),
                metadata=metadata,
            )
            fixed += 1
        if fixed:
            log.info("failure_signature.solution_status_repaired", count=fixed)
    except Exception as e:  # noqa: BLE001 — 补齐 best-effort，不阻断任何主流程
        log.warning("failure_signature.status_repair_failed", error=str(e))
    return fixed


async def backfill_solution(
    signature_key: str,
    tool_name: str,
    solution: str,
    *,
    project_id: str | None = None,
) -> bool:
    """问题解决后把解法回填进既有失败签名条目（R7 恶化项处置）。

    ``signature_key`` = 规范化失败签名（``signature_of`` 的返回值；executor
    在失败时存入 pending，同 agent **同调用身份**随后一次成功后原样带回
    —— 身份由 ``call_identity`` 定义，见其 docstring 的三条不变式）。
    ``project_id`` 定位 per-project DB（executor 在失败/成功两侧都拿得到，
    随 pending 传递；keyword-only 以保持三参位置调用契约）。

    定位 = 同 project、scope='project'、type='failure_signature'、首行
    **同时**含该签名与 ``tool=<tool_name>``（#11-(c) 写入侧补校验：签名相撞
    时必须指向正确条目，不许把解法填进别条；与 ``known_signature_hint``
    同一签名匹配语义）。回填 = 在「根因提示:」行后插入「已验证解法:
    <solution>」行（UPDATE 整条 content，不加行数，50 行裁剪逻辑不受影响）；
    经 MemoryService.save_memory 的固定写入方 upsert 落库，写锁与缓存失效
    沿用既有路径。**同时**把 ``metadata.solution_status`` 置 ``verified``
    （#16-② 解法必填校验：有解法行 ⇒ 状态位必须为 verified，机检见
    ``SOLUTION_STATUS_VERIFIED`` 注释）。

    只接受实质解法（``_is_substantive_solution``）；条目已有解法行时幂等
    跳过（并补写状态位，修占位期的不一致）。best-effort：任何失败只记日志
    返回 False，绝不影响工具执行。
    """
    sig = (signature_key or "").strip()
    if not sig or not project_id:
        return False
    if not _is_substantive_solution(solution):
        log.info(
            "failure_signature.backfill_rejected_non_solution",
            tool=tool_name,
            sig=sig[:60],
        )
        return False
    try:
        from hiveweave.services.memory import MemoryService

        memory_service = MemoryService()
        target = None
        for m in (await memory_service.get_project_memories(project_id)) or []:
            if m.get("type") != "failure_signature":
                continue
            fl = (m.get("content") or "").split("\n", 1)[0]
            if fl.startswith("[失败签名]") and (
                f"| {sig}" in fl or sig[:48] in fl
            ):
                # #11-(c)：签名相撞时按首行 tool= 二次定位 —— 解法必须落到
                # 与本次成功调用**同工具**的条目上（否则是"错解"，比没有
                # 解更贵：_signature_has_solution 会认它并对全员广播）。
                if not _first_line_matches_tool(fl, tool_name):
                    continue
                target = m
                break
        if target is None:
            log.info(
                "failure_signature.backfill_no_entry",
                sig=sig[:60],
                tool=tool_name,
            )
            return False
        if not target.get("module_id"):
            # save_memory 的 upsert 键依赖 module_id —— 缺失会 INSERT 成新行
            log.warning("failure_signature.backfill_missing_module_id")
            return False
        content = target.get("content") or ""
        metadata = dict(target.get("metadata") or {})
        if _has_verified_solution_line(content):
            # 幂等跳过，不重复追加；但补齐状态位（历史条目可能只有解法行
            # 没有 solution_status —— 机检口径要求两者不许分叉）。
            if metadata.get("solution_status") != SOLUTION_STATUS_VERIFIED:
                metadata["solution_status"] = SOLUTION_STATUS_VERIFIED
                await memory_service.save_memory(
                    agent_id=_SIGNATURE_WRITER,
                    project_id=project_id,
                    scope="project",
                    content=content,
                    type="failure_signature",
                    module_id=target.get("module_id"),
                    source_agent_id=target.get("source_agent_id"),
                    metadata=metadata,
                )
            return True
        lines = content.splitlines()
        solution_line = f"{_SOLUTION_LINE_PREFIX} {solution.strip()}"
        insert_at = None
        for i, line in enumerate(lines):
            if line.startswith("根因提示:"):
                insert_at = i + 1
                break
        if insert_at is None:
            lines.append(solution_line)
        else:
            lines.insert(insert_at, solution_line)
        metadata["solved_at_ms"] = int(time.time() * 1000)
        metadata["solution_tool"] = tool_name or ""
        metadata["solution_status"] = SOLUTION_STATUS_VERIFIED
        await memory_service.save_memory(
            agent_id=_SIGNATURE_WRITER,
            project_id=project_id,
            scope="project",
            content="\n".join(lines),
            type="failure_signature",
            module_id=target.get("module_id"),
            source_agent_id=target.get("source_agent_id"),
            metadata=metadata,
        )
        log.info(
            "failure_signature.solution_backfilled",
            project_id=project_id,
            tool=tool_name,
            sig=sig[:60],
        )
        return True
    except Exception as e:
        log.warning("failure_signature.backfill_failed", error=str(e))
        return False

# ══════════════════════════════════════════════════════════════════
# #16-③ · 组织级升级（distinct_hitters 梯度）
# ══════════════════════════════════════════════════════════════════
#
# **病因**：`metadata.hit_count` 计的是**次数**不是**不同 agent 数**，且
# **无论撞几次只更新条目、从不通知** ⇒ 「N 人各撞一遍」在数据可见、行为
# 无反应 —— R7 = 50:17 / 51:11 就是后果。
#
# **判据来源（重要）**：DSH 的 `repeat-tool-reminder` chain 是
# **per-agent、会话内、WeakMap**，README 明写 "chains stay isolated per
# agent" ⇒ **它不做跨 agent，是因为它的 agents 不共享工作区与任务池**
# （那是它的边界，不是它的判断）。
# 我们有 **CEO → 中层 → 叶子** 的编制 + 共享一个项目与任务池 ⇒
# 「同一堵墙被 N 个不同 agent 各撞一遍」是**真实且可行动的组织级信号**。
#
# **可借用 / 不可借用**：
# - 梯度**形状**可借用（DSH 通用消息设计）：升序阈值数组、首档轻推不点名、
#   后档详细、**只在精确命中阈值时发、越顶静默**、幂等。
# - **聚合维度与受众必须自研**：计数维度 = `distinct_hitters`（不同 agent
#   集合，不是"同 agent 连续重复"）；受众 = 本项目全员 + CEO/上级。
#
# **投递**：走 `health_notice.deliver_notice` 的 platform-reserved 通道 ——
# 绝不 `result["error"] +=`（见 health_notice 模块 docstring 的三问）。

#: 组织级升级阈值（**升序**，必须保持升序 —— 有结构化断言守）。
#: 只在 ``distinct_hitters`` **精确等于**某一档时发；越过最高档静默。
DISTINCT_HITTERS_THRESHOLDS: tuple[int, ...] = (3, 5, 8)

#: `distinct_hitters` 集合上限（有界 —— 防 metadata 无界膨胀）。
#: 到顶后**不再新增**，但计数继续（`distinct_hitter_count` 记真实数）。
_MAX_DISTINCT_HITTERS = 32

#: metadata 里 distinct_hitters 的键名。
_HITTERS_KEY = "distinct_hitters"
_HITTERS_OVERFLOW_KEY = "distinct_hitter_count"
_ORG_ESCALATED_AT_KEY = "org_escalated_at_ms"
_ORG_ESCALATED_TIERS_KEY = "org_escalated_tiers"


def _assert_thresholds_ascending() -> None:
    """结构化断言：阈值必须升序且互不相同（import 期执行）。

    梯度靠"精确等于某档"判定；非升序会让"越顶静默"失去意义（后一档先于
    前一档触发），且 `==` 判定会跳过中间档。这条断言是**唯一**能阻止有人
    把 `(3, 5, 8)` 改成 `(5, 3, 8)` 的机制。
    """
    ts = DISTINCT_HITTERS_THRESHOLDS
    assert ts, "organization escalation thresholds must not be empty"
    assert all(isinstance(t, int) and t > 0 for t in ts), ts
    assert list(ts) == sorted(ts), f"thresholds must be ascending: {ts}"
    assert len(set(ts)) == len(ts), f"thresholds must be distinct: {ts}"


_assert_thresholds_ascending()


def merge_distinct_hitters(prev_meta: dict, agent_id: str) -> tuple[list[str], int]:
    """把本次撞到者并入 `distinct_hitters`；返回 ``(集合, 真实总数)``。

    集合**有界**（`_MAX_DISTINCT_HITTERS`）：到顶后不再新增，但真实总数
    记在第二个返回值里（`distinct_hitter_count`），阈值判定用真实数 ——
    这样"集合被截断"不会被误读成"撞的人变少了"。
    """
    raw = prev_meta.get(_HITTERS_KEY) or []
    hitters: list[str] = [str(h) for h in raw if str(h or "").strip()]
    overflow = int(prev_meta.get(_HITTERS_OVERFLOW_KEY) or 0)
    aid = (agent_id or "").strip()
    if aid and aid not in hitters:
        if len(hitters) < _MAX_DISTINCT_HITTERS:
            hitters.append(aid)
        else:
            overflow += 1
    # 真实总数 = 集合大小 + 被截断溢出的数量（去重后不可知，故只作为下界）
    return hitters, len(hitters) + overflow


def escalation_tier(distinct_count: int) -> int | None:
    """``distinct_hitters`` 精确命中哪一档；未命中返回 ``None``。

    **只在精确相等时返回** —— 这是"越顶静默"的实现：从 5 跳到 8 时，
    ``distinct_count==5`` 已发过，``==6/7`` 不命中，``==8`` 再发一次。
    """
    ts = DISTINCT_HITTERS_THRESHOLDS
    for idx, t in enumerate(ts):
        if distinct_count == t:
            return idx
    return None


def build_org_escalation_text(
    *,
    sig: str,
    tool_name: str,
    tier_idx: int,
    distinct_count: int,
    entry_hint: str = "",
) -> str:
    """构造组织级升级正文。

    **首档轻推不点名**（对齐 DSH 的 gentle 档：只说"有人在重复撞同一堵墙、
    先分析上次结果"），后档 detailed（点名工具、人数、并指向共享条目）。
    点名是个有代价的动作 —— 首档就点名会让叶子把平台提示读成"点名批评"，
    进而不读；后档人数已经说明这不是个人问题，点名才不伤信任。
    """
    ts = DISTINCT_HITTERS_THRESHOLDS
    if tier_idx == 0:
        return (
            f"【组织级信号】本项目的 agent 们正在重复撞同一堵墙 —— "
            f"已有 {distinct_count} 个不同 agent 撞到同一个失败。"
            "（这是**团队的**问题，不是某个人的。）"
            "在重试之前，先分析上一次的结果：同一写法原样重试不会通过。"
        )
    tail = f" 共享条目: {entry_hint}" if entry_hint else ""
    if tier_idx == len(ts) - 1:
        head = (
            f"【组织级信号 · 最高档】{distinct_count} 个不同 agent 反复撞同一堵墙"
        )
    else:
        head = f"【组织级信号】{distinct_count} 个不同 agent 撞同一堵墙"
    return (
        f"{head}（工具 {tool_name}，签名 {sig[:60]}）。"
        "这已不是个别 agent 的写法问题 —— 需要有人（协调者/CEO）决定"
        "是修平台、改流程，还是把这条路彻底封掉。"
        f"单靠各自换路重试已证明无效。{tail}"
    )


async def note_distinct_hitter(
    *,
    project_id: str | None,
    signature_key: str,
    tool_name: str,
    agent_id: str,
    entry_hint: str = "",
) -> str:
    """把撞到者并入条目的 `distinct_hitters`，命中梯度则返回**升级正文**。

    返回 ``""`` 表示本次不发（首撞/未命中档/已发过该档/条目不存在）。
    升级只改 metadata，**不改动条目正文** —— 它是通知，不是内容变更。

    幂等：``metadata.org_escalated_tiers`` 记录已发过的档位（列表），
    同档只发一次（进程重启后仍成立 —— 状态在 metadata 里，不在内存）。

    best-effort：任何异常只记日志返回 ""（绝不阻断工具执行）。
    """
    sig = (signature_key or "").strip()
    aid = (agent_id or "").strip()
    if not project_id or not sig or not aid:
        return ""
    try:
        from hiveweave.services.memory import MemoryService

        memory_service = MemoryService()
        mems = await memory_service.get_project_memories(project_id)
        target = None
        for m in mems or []:
            if m.get("type") != "failure_signature":
                continue
            fl = (m.get("content") or "").split("\n", 1)[0]
            if fl.startswith("[失败签名]") and (
                f"| {sig}" in fl or sig[:48] in fl
            ):
                if not _first_line_matches_tool(fl, tool_name):
                    continue
                target = m
                break
        if target is None or not target.get("module_id"):
            # 0-4（审计 M2）：这条也是静默出口 —— 与 `known_signature_hint` 同理，
            # 未命中 = 「新签名」与「算法变更后老条目不可达」在日志上同形。
            # 只在项目里存在签名条目时留痕，且用 debug（调用频次高）。
            if target is None:
                n_entries = sum(
                    1 for _m in (mems or [])
                    if _m.get("type") == "failure_signature"
                )
                if n_entries:
                    log.debug(
                        "failure_signature.hitter_no_match",
                        n_entries=n_entries,
                        sig_prefix=sig[:48],
                    )
            return ""

        prev_meta = dict(target.get("metadata") or {})
        hitters, distinct_count = merge_distinct_hitters(prev_meta, aid)
        tier_idx = escalation_tier(distinct_count)
        already = [int(t) for t in (prev_meta.get(_ORG_ESCALATED_TIERS_KEY) or [])]
        fire = tier_idx is not None and tier_idx not in already

        metadata = dict(prev_meta)
        metadata[_HITTERS_KEY] = hitters
        metadata[_HITTERS_OVERFLOW_KEY] = max(0, distinct_count - len(hitters))
        if fire:
            metadata[_ORG_ESCALATED_TIERS_KEY] = already + [tier_idx]
            if not metadata.get(_ORG_ESCALATED_AT_KEY):
                metadata[_ORG_ESCALATED_AT_KEY] = int(time.time() * 1000)

        # 只在**有必要**时写库：撞到者已在集合里且没有新档要打 → 免写。
        _changed = (aid not in (prev_meta.get(_HITTERS_KEY) or [])) or fire
        if _changed:
            await memory_service.save_memory(
                agent_id=_SIGNATURE_WRITER,
                project_id=project_id,
                scope="project",
                content=target.get("content") or "",
                type="failure_signature",
                module_id=target.get("module_id"),
                source_agent_id=target.get("source_agent_id"),
                metadata=metadata,
            )

        if not fire:
            return ""
        log.info(
            "failure_signature.org_escalated",
            project_id=project_id,
            sig=sig[:60],
            tool=tool_name,
            tier=tier_idx,
            distinct=distinct_count,
        )
        return build_org_escalation_text(
            sig=sig,
            tool_name=tool_name,
            tier_idx=tier_idx,
            distinct_count=distinct_count,
            entry_hint=entry_hint,
        )
    except Exception as e:  # noqa: BLE001 — 升级通知 best-effort
        log.warning("failure_signature.org_escalation_failed", error=str(e))
        return ""
