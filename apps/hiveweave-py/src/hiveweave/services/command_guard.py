"""Bash 命令模式护栏（slack-clone_01 复盘 P0）— 移植 opencode 模式化权限模型。

事故背景：agent 在 tool loop 内执行 `taskkill //F //IM python.exe` /
`taskkill //PID <pid> //F`，把平台宿主（后端 uvicorn 进程）一并杀掉，
13 个 agent 全体冻死。镜像名护栏（check_platform_process_kill）拦不住
按 PID 的杀灭。

设计（对齐 opencode `permission/index.ts` + `tool/shell.ts`，见
observations/slack-clone_01/RETRO.md 定案）：

1. **规则表**：有序 `(pattern, action)` 规则数组，findLast（后写覆盖先写）。
   三态：``deny`` 硬拦 + 报错回 agent / ``allow`` 放行 / ``ask`` ——
   HiveWeave 无人在线审批，ask 一律降级为 deny + 疏通提示（告诉 agent
   安全替代写法，疏通优先而非单纯堵截）。
2. **命令识别**：首 token + 参数通配的近似（覆盖 `taskkill //IM`、`//PID`、
   `-im` 变体、复合命令、shell 包装 `powershell -c "..."` / `cmd /c ...`）。
   通配符语义同 opencode：`*`→任意，`" *"` 结尾同时匹配裸前缀；
   Windows 下大小写不敏感。tree-sitter AST 解析是后续增强。
3. **进程级硬保护（底线，不受规则开关影响）**：受保护 PID 集合
   （后端进程自身 + 祖先链 + `HIVEWEAVE_PROTECTED_PIDS` 注入），
   kill/taskkill/Stop-Process/tskill/wmic 命中受保护 PID 一律 deny；
   kill 族命令经变量/命令替换间接引用 PID（无法审计展开值）同样降级 deny。
   **防意外不防蓄意**：本层挡的是事故型误杀（taskkill //IM、rm -rf 越界），
   字符串混淆/`.bat`/`.ps1` 文件间接执行仍可绕过——完全隔离
   由 ACL 写受限令牌沙箱（`services/acl_sandbox`，`HIVEWEAVE_ACL_SANDBOX=on`）
   提供内核级强制写边界。

挂点：`tools/bash.py::_validate_command_safety`（execute_bash /
execute_run_command / game_time alarm 三处共用）+
`tools/pipeline.py::_check_shell_security`（shell 类工具预检，早失败）。

逃生门：env `HIVEWEAVE_BASH_COMMAND_GUARD=off` 只关闭规则表层；
PID 硬保护层永不关闭。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Callable, Sequence

import structlog

log = structlog.get_logger(__name__)

# ════════════════════════════════════════════════════════════════════
# 通配符匹配（opencode packages/core/src/util/wildcard.ts 的 Python 移植）
# ════════════════════════════════════════════════════════════════════

_REGEX_META = re.compile(r"[.+^${}()|[\]\\]")


def wildcard_match(text: str, pattern: str) -> bool:
    """opencode Wildcard.match 语义。

    - `*` → `.*`，`?` → `.`，其余正则元字符转义；`\\` 归一为 `/`。
    - 以 `" *"` 结尾的模式改写成 `( .*)?`：`git checkout *` 同时匹配
      裸 `git checkout` 与 `git checkout main`。
    - Windows 大小写不敏感，其他平台大小写敏感。
    """
    normalized = (text or "").replace("\\", "/")
    # 注意：替换模板必须用 \g<0>（\0 会被 re 当成 NUL 字符，吞掉被转义的字符）
    escaped = _REGEX_META.sub(r"\\\g<0>", (pattern or "").replace("\\", "/"))
    escaped = escaped.replace("*", ".*").replace("?", ".")
    if escaped.endswith(" .*"):
        escaped = escaped[:-3] + "( .*)?"
    flags = re.S | (re.I if sys.platform == "win32" else 0)
    try:
        return re.match("^" + escaped + "$", normalized, flags) is not None
    except re.error:
        return False


# ════════════════════════════════════════════════════════════════════
# 命令切分与归一化（首 token + 参数 近似，tree-sitter 的轻量替代）
# ════════════════════════════════════════════════════════════════════


def split_compound(command: str) -> list[str]:
    """按 `&&`/`||`/`;`/`|`/换行 切分复合命令（引号内不切）。

    单个 `&`（后台运行、`2>&1`）保留在子命令文本内。
    引号字符保留在文本里（匹配用）；token 化见 `_tokenize`。
    """
    parts: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i, n = 0, len(command or "")
    while i < n:
        ch = command[i]
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            i += 1
            continue
        if ch in ("\n", ";"):
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        if ch == "|":
            parts.append("".join(cur))
            cur = []
            i += 2 if i + 1 < n and command[i + 1] == "|" else 1
            continue
        if ch == "&":
            if i + 1 < n and command[i + 1] == "&":
                parts.append("".join(cur))
                cur = []
                i += 2
                continue
            cur.append(ch)
            i += 1
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _tokenize(text: str) -> list[str]:
    """空白分词，单/双引号内容视为一个 token（引号字符剥离）。

    附带收益：`"python.ex"+"e"` 这类拼接混淆在 token 化后部分去混淆。
    """
    tokens: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    has_cur = False
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
        elif ch in ("'", '"'):
            quote = ch
            has_cur = True
        elif ch.isspace():
            if cur or has_cur:
                tokens.append("".join(cur))
                cur = []
                has_cur = False
        else:
            cur.append(ch)
    if cur or has_cur:
        tokens.append("".join(cur))
    return tokens


_TOKEN_EXTS = (".exe", ".bat", ".cmd", ".ps1", ".com")


def _basename_token(tok: str) -> str:
    """剥路径与可执行扩展名：`C:\\Windows\\System32\\taskkill.exe` → `taskkill`。"""
    t = re.split(r"[\\/]", tok)[-1]
    lower = t.lower()
    for ext in _TOKEN_EXTS:
        if lower.endswith(ext):
            return t[: -len(ext)]
    return t


def _normalize_sub(sub: str) -> tuple[str, list[str]]:
    """归一化子命令：剥 `sudo`/PS 调用符 `&` 前缀 + 首 token basename 化。

    返回 (匹配用归一文本, tokens)。归一文本把多余空白压平、首 token
    换成 basename，使 `taskkill *` 能匹配全路径/带扩展名形式。
    """
    s = sub.strip()
    while s.startswith("&"):
        s = s[1:].strip()
    tokens = _tokenize(s)
    while tokens and tokens[0].lower() == "sudo":
        tokens = tokens[1:]
    if not tokens:
        return "", []
    tokens = [_basename_token(tokens[0]), *tokens[1:]]
    return " ".join(tokens), tokens


_WRAPPER_SHELLS = frozenset({"powershell", "pwsh", "cmd", "bash", "sh"})


def _is_encoded_switch(tl: str) -> bool:
    """powershell 编码命令开关判定：`-EncodedCommand` 的任意前缀缩写（M3）。

    PowerShell 参数允许不歧义前缀缩写：`-ec`/`-enc`/`-enco`… 都是
    `-EncodedCommand`。`-e` 单字母歧义于 `-ExecutionPolicy`，但实践中
    `powershell -e <base64>` 几乎总是 EncodedCommand 缩写（ExecutionPolicy
    有专用缩写 `-ep`，`"encodedcommand".startswith("ep")` 为 False），
    故 `-e` 按不可审计保守处理。
    """
    if not tl.startswith("-"):
        return False
    name = tl[1:]
    if not name:
        return False
    if name == "e":
        return True
    return "encodedcommand".startswith(name)


def _strip_scriptblock(inner: str) -> str:
    """剥离 PowerShell scriptblock 外层花括号：`{ Stop-Process ... }` → 内层。"""
    s = inner.strip()
    if s.startswith("{") and s.endswith("}"):
        return s[1:-1].strip()
    return inner


def _wrapper_inner(first: str, tokens: list[str]) -> str | None:
    """提取 shell 包装的内层命令；返回 "" 表示存在但无法审计（编码命令）。"""
    lower_first = first.lower()
    if lower_first in ("powershell", "pwsh"):
        for i, tok in enumerate(tokens[1:], 1):
            tl = tok.lower()
            if tl in ("-c", "-command"):
                # M2：PS 把 -Command 后所有剩余参数拼接执行 → join（同 cmd），
                # 并剥离 scriptblock 外层 {}（`{ Stop-Process ... }` 形式）。
                rest = tokens[i + 1:]
                if not rest:
                    return None
                return _strip_scriptblock(" ".join(rest))
            if _is_encoded_switch(tl):
                return ""  # -EncodedCommand 及前缀缩写：base64 无法审计
        return None
    if lower_first == "cmd":
        for i, tok in enumerate(tokens[1:], 1):
            if tok.lower() in ("/c", "/k"):
                rest = tokens[i + 1:]
                return " ".join(rest) if rest else None
        return None
    if lower_first in ("bash", "sh"):
        for i, tok in enumerate(tokens[1:], 1):
            if tok == "-c":
                return tokens[i + 1] if i + 1 < len(tokens) else None
        return None
    return None


# ════════════════════════════════════════════════════════════════════
# 规则表（数据驱动；findLast —— 后写的规则覆盖先写的）
# ════════════════════════════════════════════════════════════════════

# ask 降级说明后缀（统一措辞，语言无关协议常量用英文，疏通提示用中文）
_ASK_DEGRADE = "[ask→deny: 平台无在线审批，按提示改用安全形式]"

_RM_RF_HINT = (
    "rm -rf 递归强删过宽（可越界清掉工作区/平台文件）。"
    "改用 delete_directory 工具，或 rm -r <精确路径>（不带 -f）。"
)
_DEL_S_HINT = (
    "del/erase /s 递归删除过宽。改用 delete_file/delete_directory 工具。"
)
_RD_S_HINT = "rd/rmdir /s 递归删除过宽。改用 delete_directory 工具。"
_RMITEM_HINT = "Remove-Item -Recurse 过宽。改用 delete_directory 工具。"
_SIGKILL_HINT = (
    "避免 SIGKILL 一刀切：先 kill <pid>（SIGTERM）温和终止；"
    "顽固进程用 taskkill //PID <pid> //F（仅你自己启动的进程）。"
)
_PKILL_HINT = (
    "按名杀灭过宽（python/node 是平台宿主镜像，曾灭掉全平台）。"
    "改用精确 PID：kill <pid> 或 taskkill //PID <pid> //F。"
)
_TASKKILL_HINT = (
    "禁止批量/按镜像杀灭进程（taskkill //IM python.exe 曾灭掉平台宿主）。"
    "清理测试残留：先 tasklist / Get-Process 找到你自己启动的进程 PID，"
    "再 taskkill //PID <pid> //F。"
)
_STOPPROC_HINT = "禁止 Stop-Process。改用精确 PID：taskkill //PID <pid> //F。"
_WMIC_HINT = "禁止 wmic 进程删除/终止。改用精确 PID：taskkill //PID <pid> //F。"
_ENCODED_HINT = "无法审计 base64 编码命令（-EncodedCommand）；改用明文 -c 重写。"
_INDIRECT_PID_HINT = (
    "命令通过变量/命令替换间接引用 PID，护栏无法审计展开值"
    "（可能命中平台宿主进程）。改用字面 PID：先 tasklist / Get-Process "
    "确认进程归属，再 taskkill //PID <数字> //F 或 kill <数字>。"
)


def _pred_rm_recursive_force(tokens: list[str]) -> bool:
    """rm 的「递归 + 强制」组合判定。

    通配符表达不了 flag 组合（`rm -r frontend` 会被 `-*r*f*` 误伤——
    目标名含 f），必须用 token 级谓词：聚合所有短 flag 字符 +
    长 flag 集合，recursive∧force 才算危险。
    """
    short_flags = ""
    long_flags: set[str] = set()
    for tok in tokens[1:]:
        if tok.startswith("--"):
            long_flags.add(tok[2:].split("=", 1)[0].lower())
        elif tok.startswith("-") and len(tok) > 1 and not tok[1].isdigit():
            short_flags += tok[1:].lower()
    recursive = "r" in short_flags or "recursive" in long_flags
    force = "f" in short_flags or "force" in long_flags
    return recursive and force


_KILL_DANGER_SIGNALS = frozenset({"9", "kill", "sigkill"})


def _pred_kill_dangerous_signal(tokens: list[str]) -> bool:
    """kill 携带 SIGKILL 类信号判定：`-9` / `-KILL` / `-SIGKILL` / `-s 9|KILL|SIGKILL`。

    通配符表达不了信号参数的位置自由度（`-s SIGKILL` 两个 token、`-9` 单 token、
    大小写变体），token 级谓词统一覆盖（L4：补 `-s SIGKILL` 标准写法）。
    审计 F1：补 GNU 长选项 `--signal=9` / `--signal=<sig>`（`--signal SIGKILL`
    两 token 形式）——此前 `tl[1:]="-signal=9"` 不在危险集，SIGKILL 引导被绕过。
    bare `kill <pid>`（SIGTERM 温和终止）不匹配 → 维持 allow。
    """
    i, n = 1, len(tokens)
    while i < n:
        tl = tokens[i].lower()
        if tl == "-s":
            if i + 1 < n and tokens[i + 1].lower() in _KILL_DANGER_SIGNALS:
                return True
            i += 2
            continue
        if tl == "--signal":
            # `--signal SIGKILL`（两 token）：下一 token 是危险信号 → 命中
            if i + 1 < n and tokens[i + 1].lower() in _KILL_DANGER_SIGNALS:
                return True
            i += 2
            continue
        if tl.startswith("--signal="):
            # `--signal=9` / `--signal=SIGKILL`（单 token 内联值）
            if tl.split("=", 1)[1] in _KILL_DANGER_SIGNALS:
                return True
            i += 1
            continue
        if tl.startswith("-") and tl[1:] in _KILL_DANGER_SIGNALS:
            return True
        i += 1
    return False


def _pred_taskkill_exact_pid(tokens: list[str]) -> bool:
    """taskkill 精确 PID 豁免判定（slack-clone_01 事故修复核心，S1）。

    仅当所有开关都属于安全集（`/pid <纯数字>`、`/f`、`/t`，单双斜杠均可）
    且至少解析出一个字面 PID 时才豁免。出现 `//IM`（按镜像名）、`/FI`
    （过滤器）、变量引用或任何无法归类的开关 → 不豁免，落到 `taskkill *` deny。
    关键：事故命令 `taskkill //F //IM python.exe` 追加 `//PID` 的混合开关变体
    （IM+PID 同现）谓词返回 False → deny，堵住通配符 `taskkill */pid *` 的洞。
    """
    saw_pid = False
    i, n = 1, len(tokens)
    while i < n:
        sw = tokens[i].lstrip("/").lower()
        if sw == "pid":
            if i + 1 < n and tokens[i + 1].isdigit():
                saw_pid = True
                i += 2
                continue
            return False  # /pid 后缺字面 PID（变量/引用/缺失）→ 不豁免
        if sw in ("f", "t"):
            i += 1
            continue
        return False  # im / fi / 其他开关或非开关参数 → 不豁免
    return saw_pid


_PREDICATES: dict[str, Callable[[list[str]], bool]] = {
    "rm_recursive_force": _pred_rm_recursive_force,
    "kill_dangerous_signal": _pred_kill_dangerous_signal,
    "taskkill_exact_pid": _pred_taskkill_exact_pid,
}


@dataclass(frozen=True)
class GuardRule:
    """一条 bash 命令规则。

    ``pattern`` 为通配符模式；``pred`` 非空时 pattern 表示命令名
    （首 token 匹配才进入谓词判定）。``action``: deny/allow/ask。
    ``hint`` 在 deny/ask 时回给 agent 的疏通指引。
    """

    pattern: str
    action: str  # "deny" | "allow" | "ask"
    hint: str = ""
    pred: str = ""


# 有序规则表 —— findLast，后写覆盖先写（opencode Permission.merge 语义）。
# 注意顺序：通用模式在前，更具体的豁免/加严在后。
DEFAULT_BASH_RULES: tuple[GuardRule, ...] = (
    GuardRule("*", "allow"),
    # ── 递归/强制删除（ask → 降级 deny + 疏通） ──
    GuardRule("rm", "ask", hint=_RM_RF_HINT, pred="rm_recursive_force"),
    GuardRule("del */s *", "ask", hint=_DEL_S_HINT),
    GuardRule("erase */s *", "ask", hint=_DEL_S_HINT),
    GuardRule("rd */s *", "ask", hint=_RD_S_HINT),
    GuardRule("rmdir */s *", "ask", hint=_RD_S_HINT),
    GuardRule("remove-item *-recurse*", "ask", hint=_RMITEM_HINT),
    # ── POSIX kill：裸 kill <pid> 放行；SIGKILL 类信号 ask（谓词统一判定） ──
    GuardRule("kill *", "allow"),
    GuardRule("kill", "ask", hint=_SIGKILL_HINT, pred="kill_dangerous_signal"),
    # ── 按名杀灭：deny（镜像名误杀是事故根因） ──
    GuardRule("pkill *", "deny", hint=_PKILL_HINT),
    GuardRule("killall *", "deny", hint=_PKILL_HINT),
    GuardRule("taskkill *", "deny", hint=_TASKKILL_HINT),
    # 精确 PID 豁免（谓词：仅当所有开关安全且含字面 PID；IM/变量 → 不豁免。
    # 受保护 PID 仍由进程硬层拦，见 evaluate_command 第二步）
    GuardRule("taskkill", "allow", pred="taskkill_exact_pid"),
    GuardRule("stop-process *", "deny", hint=_STOPPROC_HINT),
    GuardRule("tskill *", "deny", hint=_TASKKILL_HINT),
    GuardRule("wmic *process*delete*", "deny", hint=_WMIC_HINT),
    GuardRule("wmic *process*terminate*", "deny", hint=_WMIC_HINT),
    GuardRule("wmic *call*terminate*", "deny", hint=_WMIC_HINT),
    # ── 编码包装（无法审计） ──
    GuardRule("powershell *-enc*", "ask", hint=_ENCODED_HINT),
    GuardRule("pwsh *-enc*", "ask", hint=_ENCODED_HINT),
)

# 运行期追加规则（未来 Settings/项目级覆盖入口；追加在默认表之后 → 覆盖默认）
_extra_rules: list[GuardRule] = []


def add_rules(rules: Sequence[GuardRule]) -> None:
    """追加项目/会话级规则（findLast：后追加的覆盖内置默认）。"""
    _extra_rules.extend(rules)


def _active_rules() -> list[GuardRule]:
    return [*DEFAULT_BASH_RULES, *_extra_rules]


def _match_rule(
    norm_text: str, tokens: list[str], rules: Sequence[GuardRule]
) -> GuardRule | None:
    """findLast：最后一条命中的规则胜出（含谓词规则）。"""
    matched: GuardRule | None = None
    for rule in rules:
        if rule.pred:
            fn = _PREDICATES.get(rule.pred)
            if fn is None:
                continue
            if tokens and tokens[0].lower() == rule.pattern.lower():
                try:
                    if fn(tokens):
                        matched = rule
                except Exception:
                    continue
        elif wildcard_match(norm_text, rule.pattern):
            matched = rule
    return matched


# ════════════════════════════════════════════════════════════════════
# 进程级硬保护（P0-2：底线层，规则无法覆盖）
# ════════════════════════════════════════════════════════════════════

_protected_pids: set[int] = set()

_KILL_FAMILY = frozenset(
    {"taskkill", "kill", "stop-process", "tskill", "wmic", "pkill", "killall"}
)

_TASKKILL_PID_RE = re.compile(r"/{1,2}pid\s+(\d+)", re.IGNORECASE)
_STOPPROC_PID_RE = re.compile(r"-(?:id|processid)\s+(\d+)", re.IGNORECASE)
_WMIC_PID_RE = re.compile(r"processid\s*=\s*(\d+)", re.IGNORECASE)


def _extract_target_pids(first: str, tokens: list[str], norm_text: str) -> set[int]:
    """从杀灭类命令中提取目标 PID 集合（仅 kill 族命令才解析）。"""
    pids: set[int] = set()
    first_l = first.lower()
    if first_l == "taskkill":
        for m in _TASKKILL_PID_RE.finditer(norm_text):
            pids.add(int(m.group(1)))
    elif first_l in ("kill", "tskill"):
        for tok in tokens[1:]:
            if tok.isdigit():
                pids.add(int(tok))
    elif first_l == "stop-process":
        for m in _STOPPROC_PID_RE.finditer(norm_text):
            pids.add(int(m.group(1)))
    elif first_l == "wmic":
        for m in _WMIC_PID_RE.finditer(norm_text):
            pids.add(int(m.group(1)))
    return pids


_WIN_VAR_RE = re.compile(r"%[^%\s]+%")


def _has_indirect_ref(tokens: list[str]) -> bool:
    """检测参数中的 shell 间接引用（变量/命令替换）—— 执行期才展开，护栏看不到真实值。

    覆盖 POSIX `$VAR` / `${VAR}` / `$(cmd)` / 反引号 与 cmd `%VAR%`（M1：
    `P=4000; taskkill //PID $P //F` 这类变量间接引用可穿透字面 PID 提取）。
    """
    for tok in tokens[1:]:
        if "$" in tok or "`" in tok or _WIN_VAR_RE.search(tok):
            return True
    return False


def register_protected_pid(pid: int) -> None:
    """登记受保护 PID（平台宿主进程）。"""
    if pid and pid > 0:
        _protected_pids.add(int(pid))


def protected_pids() -> frozenset[int]:
    return frozenset(_protected_pids)


def _reset_protected_for_tests() -> None:
    _protected_pids.clear()


def _ancestors_psutil() -> set[int]:
    try:
        import psutil  # 可选依赖；有就走最准的祖先链
    except ImportError:
        return set()
    try:
        return {p.pid for p in psutil.Process(os.getpid()).parents()}
    except Exception:
        return set()


def _ancestors_win32_toolhelp() -> set[int]:
    """stdlib ctypes Toolhelp32 快照：全量 pid→ppid 映射后自举祖先链。"""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    # L2：显式 restype/argtypes——缺省时返回值按 c_int 截断，句柄失效且
    # INVALID_HANDLE_VALUE(-1) 检测成死代码。c_void_p 语义下 -1 → 0xFF..FF。
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)
    ]
    kernel32.Process32NextW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return set()
    parent_of: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parent_of[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    ancestors: set[int] = set()
    pid = os.getpid()
    for _ in range(64):  # 防御环
        ppid = parent_of.get(pid)
        if not ppid or ppid in ancestors or ppid == pid:
            break
        ancestors.add(ppid)
        pid = ppid
    return ancestors


def _ancestors_proc_fs() -> set[int]:
    """Linux /proc 兜底：逐层读 ppid。"""
    ancestors: set[int] = set()
    pid = os.getpid()
    for _ in range(64):
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="ascii", errors="ignore") as f:
                stat = f.read()
            # comm 可能含空格/括号 —— 取最后一个 ')' 之后的字段，ppid 是第 2 个
            tail = stat.rsplit(")", 1)[-1].split()
            ppid = int(tail[1])
        except Exception:
            break
        if ppid <= 0 or ppid in ancestors or ppid == pid:
            break
        ancestors.add(ppid)
        pid = ppid
    return ancestors


def init_process_protection(extra_pids: Sequence[int] | None = None) -> frozenset[int]:
    """登记平台宿主进程保护集：自身 PID + 祖先链 + env/参数注入。

    后端进程生存期内 PID 不变，lifespan 启动时调用一次即可。
    `taskkill /T` 杀进程树会连带祖先下的整棵子树，所以祖先链必须入集。
    """
    register_protected_pid(os.getpid())
    ppid = os.getppid()
    if ppid:
        register_protected_pid(ppid)
    ancestors = _ancestors_psutil()
    if ancestors:
        for pid in ancestors:
            register_protected_pid(pid)
    else:
        # psutil 缺席/无结果时的 stdlib 兜底（L1：显式 if，非 for...else——
        # 原写法循环无 break，else 无条件执行，与注释意图相悖）
        try:
            if sys.platform == "win32":
                for pid in _ancestors_win32_toolhelp():
                    register_protected_pid(pid)
            else:
                for pid in _ancestors_proc_fs():
                    register_protected_pid(pid)
        except Exception as e:
            log.debug("command_guard_ancestors_failed", error=str(e))
    env_pids = os.environ.get("HIVEWEAVE_PROTECTED_PIDS", "")
    for part in env_pids.split(","):
        part = part.strip()
        if part.isdigit():
            register_protected_pid(int(part))
    for pid in extra_pids or ():
        register_protected_pid(pid)
    log.info("command_guard_protected_pids", count=len(_protected_pids))
    return frozenset(_protected_pids)


# ════════════════════════════════════════════════════════════════════
# 求值入口
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class GuardVerdict:
    """命令护栏判定结果。

    ``action`` 三值（T2.2）：``"allow"`` / ``"deny"`` / ``"ask"``。
    ask 不再在 guard 内降级 —— 判定保持 ask 原样上浮，由消费方决定走
    :func:`resolve_ask_with_approval`（在线审批）还是 :func:`degrade_ask`
    （非交互路径，如 game_time 定时器）。
    """

    blocked: bool
    action: str  # "allow" | "deny" | "ask"
    reason: str = ""
    rule: str = ""  # 命中的 pattern（遥测/审计用）


_ALLOW = GuardVerdict(False, "allow")

_MAX_WRAP_DEPTH = 3


def _guard_enabled() -> bool:
    return os.environ.get("HIVEWEAVE_BASH_COMMAND_GUARD", "").lower() not in (
        "off",
        "0",
        "disabled",
    )


def evaluate_command(
    command: str,
    *,
    rules: Sequence[GuardRule] | None = None,
    protected: frozenset[int] | set[int] | None = None,
    _depth: int = 0,
) -> GuardVerdict:
    """评估一条 shell 命令是否允许执行。

    判定顺序（每个子命令，任一 deny 即整体 deny）：
    1. shell 包装解包（powershell/cmd/bash -c …），递归最深 3 层；
    2. **PID 硬保护**：kill 族命令命中受保护 PID → deny（不受规则开关影响）；
    3. 规则表 findLast；ask 降级为 deny + 疏通提示。
    """
    rule_set = list(rules) if rules is not None else _active_rules()
    protected_set = _protected_pids if protected is None else protected
    guard_on = _guard_enabled()

    for sub in split_compound(command):
        norm_text, tokens = _normalize_sub(sub)
        if not tokens:
            continue
        first = tokens[0].lower()

        # 1. shell 包装解包（内层命令递归评估）
        if first in _WRAPPER_SHELLS:
            # L7：嵌套包装超深 → 无法审计内层 → 拒绝（与编码命令同一原则，
            # 不能「无法审计即放行」）。这是真 deny，不是 ask 降级。
            if _depth >= _MAX_WRAP_DEPTH:
                return GuardVerdict(
                    True,
                    "deny",
                    f"shell 包装嵌套超过 {_MAX_WRAP_DEPTH} 层，无法审计内层命令，"
                    f"请展开为直接命令。",
                    "wrapper:too_deep",
                )
            inner = _wrapper_inner(tokens[0], tokens)
            if inner == "":
                hint = _ENCODED_HINT
                return GuardVerdict(True, "deny", hint, "wrapper:encoded")
            if inner:
                inner_verdict = evaluate_command(
                    inner,
                    rules=rule_set,
                    protected=protected_set,
                    _depth=_depth + 1,
                )
                if inner_verdict.blocked:
                    # T2.2: 内层 ask 保持 ask（包装内的 ask 命令仍可走审批），
                    # deny 照常传播。
                    return GuardVerdict(
                        True,
                        inner_verdict.action,
                        f"(经由 {tokens[0]} 包装) {inner_verdict.reason}",
                        inner_verdict.rule,
                    )

        # 2. PID 硬保护（底线层；规则表关闭时也生效）
        for pid in _extract_target_pids(first, tokens, norm_text):
            if pid in protected_set:
                reason = (
                    f"PID {pid} 属于 HiveWeave 平台宿主进程（后端或其祖先链），"
                    f"受保护不可杀灭。清理你自己启动的进程："
                    f"tasklist / Get-Process 确认 PID 归属后再操作。"
                )
                log.warning(
                    "command_guard_protected_pid",
                    pid=pid,
                    command_head=norm_text[:80],
                )
                return GuardVerdict(True, "deny", reason, "__protected_pid__")

        # 2b. M1：kill 族命令目标为间接引用（$VAR / %VAR% / `...` / $(...)）→
        # 展开值执行期才定，护栏无法审计 → 降级 deny。防「变量 = 受保护 PID」
        # 穿透字面 PID 提取（taskkill 的 /pid 后变量已被 taskkill_exact_pid
        # 谓词挡，但 bare kill/tskill/stop-process 的变量参数靠这道兜底）。
        if first in _KILL_FAMILY and _has_indirect_ref(tokens):
            log.warning(
                "command_guard_indirect_pid",
                command_head=norm_text[:80],
            )
            return GuardVerdict(
                True, "deny", _INDIRECT_PID_HINT, "__indirect_pid__"
            )

        if not guard_on:
            continue

        # 3. 规则表（findLast）
        matched = _match_rule(norm_text, tokens, rule_set)
        if matched is None or matched.action == "allow":
            continue
        if matched.action == "deny":
            reason = matched.hint or f"命令被规则 {matched.pattern} 拒绝。"
            log.info(
                "command_guard_deny",
                rule=matched.pattern,
                command_head=norm_text[:80],
            )
            return GuardVerdict(True, "deny", reason, matched.pattern)
        # T2.2: ask 判定原样上浮（不再降级）。消费方二选一：
        # resolve_ask_with_approval（在线审批）/ degrade_ask（非交互降级）。
        reason = matched.hint or "该命令需审批。"
        log.info(
            "command_guard_ask",
            rule=matched.pattern,
            command_head=norm_text[:80],
        )
        return GuardVerdict(True, "ask", reason, matched.pattern)

    return _ALLOW


async def resolve_ask_with_approval(
    verdict: GuardVerdict,
    *,
    agent_id: str,
    tool_name: str,
    tool_args: dict | None = None,
) -> GuardVerdict:
    """T2.2: ask 判定接已有的在线审批通道（ApprovalService，复用不走新建）。

    - 用户批准 → allow（本次放行；remember 规则由 ApprovalService 落库）；
    - PermissionTimeout → deny（超时文案与 executor/pipeline 注册路径一致）；
    - PermissionRejected → deny（用户主动拒绝）；
    - 审批通道本身故障 → fail-closed 降级 deny（原 _ASK_DEGRADE 语义）。
    非 ask 判定原样返回。
    """
    if verdict.action != "ask":
        return verdict
    # s3-clone_07 报告 P0-2：无人值守下 ask 必须**确定性秒拒**，绝不入队等满 120s。
    # executor/pipeline 注册路径已 honored（F5），但本函数是 shell 命令护栏的 ask
    # 出口，此前直连 request_permission 绕过了 unattended 检查——实测 18 次
    # Remove-Item -Recurse 审批 ≈28 分钟墙钟烧在无人应答的等待上。
    # 查询失败则静默回落到原审批路径（行为不变），不因本检查引入新故障面。
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.approval import is_unattended_mode

        agent_row = await meta_db.get_agent_by_id(agent_id)
        project_id = (agent_row or {}).get("project_id")
        if project_id and await is_unattended_mode(project_id):
            log.info(
                "command_guard_ask_unattended_reject",
                rule=verdict.rule,
                agent_id=agent_id,
            )
            return GuardVerdict(
                True,
                "deny",
                (
                    "[unattended mode] 命中命令护栏 ask 类规则（"
                    f"{verdict.rule}），无人值守下不等待人工审批、直接拒绝。"
                    "请改用专用工具（如 delete_directory 清理目录）或缩小"
                    "命令作用范围后重试；不要原样重试。"
                ),
                verdict.rule,
            )
    except Exception:  # noqa: BLE001 — best-effort 前置检查
        pass
    try:
        from hiveweave.services.approval import (
            APPROVAL_TIMEOUT_HINT,
            PermissionRejected,
            PermissionTimeout,
            approval_service,
        )

        await approval_service.request_permission(
            agent_id=agent_id,
            tool_name=tool_name,
            tool_args=tool_args,
            description=(
                f"Command guard asks approval ({verdict.rule}): "
                f"{verdict.reason[:160]}"
            ),
        )
        log.info(
            "command_guard_ask_approved",
            rule=verdict.rule,
            agent_id=agent_id,
        )
        return GuardVerdict(False, "allow", "", verdict.rule)
    except PermissionTimeout:
        return GuardVerdict(
            True,
            "deny",
            APPROVAL_TIMEOUT_HINT,
            verdict.rule,
        )
    except PermissionRejected as exc:
        return GuardVerdict(
            True, "deny", f"Permission rejected: {exc}", verdict.rule
        )
    except Exception as exc:  # noqa: BLE001 — 通道故障 fail-closed
        log.warning(
            "command_guard_ask_channel_error",
            rule=verdict.rule,
            error=str(exc),
        )
        return GuardVerdict(
            True,
            "deny",
            f"{verdict.reason} {_ASK_DEGRADE}",
            verdict.rule,
        )


def degrade_ask(verdict: GuardVerdict) -> GuardVerdict:
    """非交互路径（game_time 定时器等无审批通道）把 ask 降级回 deny。

    保持原降级语义（降级说明 + 疏通提示），供无法 await 审批的消费方。
    """
    if verdict.action != "ask":
        return verdict
    return GuardVerdict(
        True,
        "deny",
        f"{verdict.reason} {_ASK_DEGRADE}",
        verdict.rule,
    )
