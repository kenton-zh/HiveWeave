"""Shell 方言词表 —— bash/unix 惯用法 → pwsh 等价写法的**唯一真值源**。

**为什么单独成模块（2026-09-12）**：这两张表此前住在 ``tools/bash.py``，而
``prompts/executor.py`` 的方言段是**手抄**的一份「禁用清单」，只列了 ~11 项。
实测两边漂移：代码实际拒 **67 项**（类 1 共 55 + 类 2 共 12），提示词只点名 11 项。
漏掉的全是 unix 肌肉记忆高频项（``cp`` / ``mv`` / ``sort`` / ``kill`` / ``find`` /
``tail`` / ``chmod`` / ``du`` / ``which`` …）⇒ agent 照提示词避开了 ``grep``/``sed``，
却照样写 ``cp -r`` / ``sort -u`` 撞墙。**清单类内容一旦双写就必然漂移。**

**模块分层理由**：不直接让 ``prompts/`` import ``tools/bash.py`` —— 后者是 3600+ 行、
依赖 ``structlog`` / ``path_guard`` / ``fact_positions`` 的重模块。提示词构造是
**每次请求都要跑**的热路径，不该为此拉起整个 bash 工具栈。本模块**零依赖**（纯 dict），
双方各自 import 它即可，谁都不需要 import 对方。

**两张表的区别**（拦截时机不同，别混）：

- :data:`UNIX_ONLY_HINTS`（类 1）——pwsh **PATH 上根本没有同名可执行文件**。
  ``sed`` / ``awk`` / ``head`` / ``grep`` … 一旦出现即拦。
- :data:`ALIAS_FLAG_HINTS`（类 2）——pwsh **有同名别名或同名 exe**，但 unix flag
  语义对不上。**仅当带 unix 短 flag 时**才拦（裸 ``cat f`` 是合法的 pwsh）。
  类 2 更阴险：``system32\\sort.exe`` / ``find.exe`` 会**静默做出完全不同的行为**
  （``find`` 是查字符串而非查文件），不报错但结果错。

**修改纪律**：只改本文件。``tools/bash.py`` 与 ``prompts/executor.py`` 都从这里取数，
两边**不允许**再出现手抄副本。

⚠ **本模块只保证「同一份名单不被写第二遍」，不保证「名单内容本身正确」** ——
某条命令该不该在这里、类 1 的措辞是否对每个词都成立，**机器判不了**，需人/LLM 复核。
（已知实例：``man`` 在 pwsh 里是 ``help`` 的别名、``sudo.exe`` 在 Win11 存在、``time`` /
``wait`` 是保留字 —— 它们被拦是对的（拦截以「平台会拒」为准），但类 1 那句
「PATH 上根本没有同名可执行文件」对这几个词并不严格成立。措辞已在下文放宽。）
"""

from __future__ import annotations

# 类 1：平台会**前置拒绝**的 unix 惯用命令（绝大多数是 pwsh PATH 上无同名 exe）。
#
# ⚠ 表头措辞注意（2026-09-12 实测）：说「PATH 上无同名可执行文件」对**大多数**成立，
# 但有几个不是 —— `man` 是 `help` 的别名、`sudo` 在 Win11 有 `System32\sudo.exe`、
# `time`/`wait` 是 pwsh 保留字。它们**仍应留在本表**（平台会拒，且给了正确 pwsh 替代），
# 但别再把本表理解成「pwsh 里不存在」。**权威语义是「本表 = 平台会拒 + 已给替代写法」**。
# 实测：`Get-Command man` → Alias；`Get-Command sudo` → Application System32\sudo.exe。
UNIX_ONLY_HINTS: dict[str, str] = {
    "sed": "逐行替换用 (Get-Content f) -replace 'A','B' | Set-Content f；"
           "取行区间用 Get-Content f | Select-Object -Skip N -First M",
    "awk": "取列用 Get-Content f | ForEach-Object { ($_ -split '\\s+')[0] }",
    "wc": "行数用 (Get-Content f).Count",
    "xargs": "用管道 + ForEach-Object：Get-ChildItem … | ForEach-Object { … $_ }",
    "head": "Get-Content f -TotalCount N",
    "tail": "Get-Content f -Tail N（跟随写入加 -Wait）",
    "grep": "Select-String -Pattern P -Path f；递归 "
            "Get-ChildItem -Recurse -File dir | Select-String -Pattern P",
    "find": "按名/递归用 Get-ChildItem -Recurse -File -Filter '*x*'（或 "
            "Where-Object { $_.Name -like '*x*' }）；删除用 "
            "Get-ChildItem … | Remove-Item -Force（先 Select-Object FullName 看清单）",
    "touch": "New-Item -ItemType File -Force -Path f",
    "which": "Get-Command <名> | Select-Object -ExpandProperty Source",
    "cut": "($line -split ',')[0] 或 Import-Csv",
    "tr": "-replace 运算符：$s -replace 'a','b'",
    "uniq": "Select-Object -Unique 或 Sort-Object -Unique",
    "du": "(Get-ChildItem -Recurse -File . | Measure-Object Length -Sum).Sum",
    "df": "Get-PSDrive -PSProvider FileSystem",
    "basename": "Split-Path -Leaf <路径>",
    "dirname": "Split-Path -Parent <路径>",
    "realpath": "Resolve-Path <路径>",
    "readlink": "Resolve-Path <路径>",
    "chmod": "Windows 无 POSIX 权限位；用 icacls（通常不需要）",
    "chown": "Windows 无 POSIX 属主；用 icacls（通常不需要）",
    "printf": "Write-Output 或 -f 格式化：'{0}' -f $v",
    "stat": "Get-Item f | Format-List *",
    "seq": "范围运算符：1..3",
    "ln": "New-Item -ItemType SymbolicLink -Path L -Target T",
    "nl": "Get-Content f | ForEach-Object { \"$($_.ReadCount): $_\" }",
    "less": "Get-Content f（分页无必要，输出已截断）",
    "env": "Get-ChildItem Env:",
    "md5sum": "Get-FileHash f -Algorithm MD5",
    "sha256sum": "Get-FileHash f -Algorithm SHA256",
    "mktemp": "New-TemporaryFile",
    "pgrep": "Get-Process -Name <名>",
    # 45 轮 P0：kill 族等价建议必须指向护栏放行的形式——护栏 deny
    # stop-process/pkill/taskkill(批量)，suggesting Stop-Process 会把
    # 「方言正确」的改写再送进护栏拒绝，agent 两头撞墙。
    "pkill": "按名杀灭会被护栏拒绝（按名误杀曾灭平台宿主）。先 "
             "Get-Process -Name <名> 查 PID，再 kill <pid>（精确 PID 放行）",
    "sudo": "Windows 无 sudo；平台已按需授权，去掉 sudo 直接跑",
    "man": "Get-Help <命令>",
    "dos2unix": "(Get-Content f -Raw) -replace \"`r`n\",\"`n\" | "
                "Set-Content f -NoNewline",
    # ── 45 轮 s3-clone_10 实锤/盘点补充（pwsh 无同名命令或 builtin）──
    "export": "$env:NAME='val'（pwsh 无 export，赋值即生效）",
    "od": "Format-Hex -Path f（字节转储）",
    "xxd": "Format-Hex -Path f",
    "base64": "[Convert]::ToBase64String([IO.File]::ReadAllBytes(f))；"
              "解码用 [Convert]::FromBase64String",
    "uname": "无等价；系统信息看 $PSVersionTable",
    "id": "whoami（当前用户）",
    "strings": "Select-String -Path f -Pattern '[\\x20-\\x7E]{4,}'（或 python 一行）",
    "tac": "$c=Get-Content f; [Array]::Reverse($c); $c",
    "rev": "-join ($s[-1..-$s.Length])",
    "shuf": "Get-Random -InputObject $arr -Count $arr.Count",
    "split": "分批用 Get-Content f | Select-Object -Skip N -First M 逐段写出",
    "column": "Format-Table",
    "paste": "两文件并排少用；Import-Csv 或 python 一行",
    "join": "Import-Csv 后按 key 合并，或 python 一行",
    "iconv": "[IO.File]::ReadAllText(f, [Text.Encoding]::GetEncoding('源编码'))",
    "nohup": "后台用 bash 工具的 background 参数，或 Start-Process -NoNewWindow",
    "time": "Measure-Command { … }",
    "lsof": "端口看 netstat -ano；文件句柄看 Get-Process | Select-Object Id,ProcessName,Path",
    "wait": "Wait-Process -Id <pid>",
}

# 类 2：pwsh 有同名别名/同名 exe，但 unix flag 语义对不上 —— 会报参数错误
# 或（更糟）静默做别的事。仅当带 unix 短 flag 时才拦。
ALIAS_FLAG_HINTS: dict[str, str] = {
    "ls": "Get-ChildItem -Force（-l/-h 无对应；要长格式用 "
          "Format-Table 或 Select-Object）",
    "cat": "Get-Content f（-n 无对应，行号用 "
           "ForEach-Object { \"$($_.ReadCount): $_\" }）",
    "rm": "Remove-Item -Recurse -Force <路径>（-rf 会被当成 -Filter 歧义拒绝）",
    "cp": "Copy-Item -Recurse -Force <源> <目标>",
    "mv": "Move-Item -Force <源> <目标>",
    "echo": "Write-Output（-e 会被当成 -ErrorAction 歧义拒绝；"
            "换行用双引号里的 `n）",
    "sort": "Sort-Object -Unique（system32\\sort.exe 不认 -u，会静默排错）",
    "find": "Get-ChildItem -Recurse -File -Filter '*.py'"
            "（system32\\find.exe 是查字符串，不是查文件）",
    "kill": "裸 kill <pid> 即温和终止（护栏放行）；顽固进程 "
            "taskkill //PID <pid> //F（仅你自己启动的进程）",
    "ps": "Get-Process（ps aux 会把 aux 当进程名）",
    "tee": "Tee-Object -FilePath f（-a 用 -Append）",
    "diff": "Compare-Object (Get-Content a) (Get-Content b)",
}


def all_rejected_commands() -> list[str]:
    """两个词表覆盖的全部命令名（去重、保序：类 1 在前、类 2 在后）。

    ``find`` 同时出现在两张表里（类 1 是「pwsh 无此命令」，类 2 是
    「有同名 exe 但语义不同」）—— 去重后只保留首次出现。
    """
    out: list[str] = []
    seen: set[str] = set()
    for table in (UNIX_ONLY_HINTS, ALIAS_FLAG_HINTS):
        for name in table:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def rejected_commands_inline() -> str:
    """提示词用的一行式命令清单（反引号包裹，``/`` 分隔，自动折行）。

    由 :func:`all_rejected_commands` 生成，**不是手抄** —— 这是本模块存在的
    全部理由。新增词条后提示词自动跟上，不会再漂移。
    """
    names = all_rejected_commands()
    return " / ".join(f"`{n}`" for n in names)
