@echo off
REM ============================================================
REM  HiveWeave Windows EXE build (PyInstaller onedir + assets)
REM  Usage: build-exe.bat
REM  Output: apps\desktop\dist\HiveWeave\  (HiveWeave.exe + _internal
REM          + ball/ + web/ + bin/agent-browser —— frozen 口径 exe 同级)
REM  重建前必须 taskkill HiveWeave.exe（文件锁会炸 PyInstaller 清目录）。
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set NODE22=%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64
set PATH=%NODE22%;%PATH%

set OUT=apps\desktop\dist\HiveWeave
set PRESERVE=apps\desktop\dist\.hw-preserve

REM ── [0/5] 前置门禁：产物被占用则立即中止 ──────────────────────
REM 为什么必须放在最前面：PyInstaller COLLECT 整删 dist\HiveWeave，而运行中的
REM 进程锁着 exe + _internal\*.dll + data\（SQLite DB），删目录必失败。若跑完
REM [1][2] 才炸，用户白等几分钟，且 .hw-preserve 可能留下半搬移现场。
REM 用 tasklist 而非 taskkill：不替用户做「杀掉他的程序」这个决定，只报告。
REM
REM ⚠ 判据铁律（2026-09-13 实测，极易做反）：**必须探目录，不能只探 exe 文件**。
REM   实测：`ren dist\HiveWeave\HiveWeave.exe` 会**成功** —— 因为上次构建替换过该
REM   文件，而 Windows 运行中的映像指向**已被替换掉的旧文件对象**，路径上的新文件
REM   不被它锁 ⇒ 只看 exe 会得出「没被锁」的**反向结论**（我因此差点误判成"门禁
REM   误报"）。真正决定成败的是**目录能否整删**：实测被占的是 data\ 与 _internal\，
REM   而 web\ ball\ bin\ .env 是自由的。故此处判两件事：
REM     (a) 按镜像名找进程（快速 + 给出 PID）；
REM     (b) 对**产物目录**做 ren 探针 —— 这才是 PyInstaller 真正会撞的墙。
echo [0/5] Checking for running HiveWeave.exe (file lock guard)...
REM ⚠ 用 tasklist+find 双保险：tasklist 在「无匹配」时退出码仍为 0（只打印
REM 「信息: 没有运行的任务匹配指定标准。」），所以必须靠管道末端 find 的退出码
REM 判别（命中=0 / 未命中=1）。cmd 的管道 errorlevel 取末端命令，这是本判据的基石。
REM 已实测两分支：无此进程 → find=1 → 放行；有（如 explorer.exe）→ find=0 → 中止。
set HW_PROCS=0
tasklist /FI "IMAGENAME eq HiveWeave.exe" 2>nul | find /I "HiveWeave.exe" >nul
if not errorlevel 1 set HW_PROCS=1

REM ── 探针残留处置（自愈 / 孤儿检测 / 失败 fail-loud）────────────
REM 上次探针若在「ren 出去」与「ren 回来」之间被中断，目录会停在
REM <OUT>.__lockprobe__，导致后面全找不到 %OUT%。**必须分三种情形**
REM （2026-09-15 独立审计 P1，旧写法只判了第 ① 种）：
REM   ① 只有 leftover      → 自愈：ren 回 %OUT%；
REM   ② leftover **与 %OUT% 并存** → ren 会因**目标重名**失败，旧写法把它
REM                          误读成「目录被占用」并提示 taskkill —— 而
REM                          taskkill 对这情形**完全无效**。且 leftover 里
REM                          可能就装着用户 data/，不许静默删 ⇒ fail-loud。
REM   ③ 都没有             → 正常（首次构建）。
set HW_PROBE=%OUT%.__lockprobe__
REM MEASURED NOTES, 2026-09-15. This guard broke twice while being reworked; the
REM symptom was the entire [0/5] region becoming unparseable, reported by cmd as
REM "was unexpected at this time" plus a mojibake equivalent. Two **independently
REM re-verified** causes:
REM   1. an UNBALANCED ASCII right paren inside block echo text, with more text
REM      following on the same line - cmd closes the block right there and runs
REM      the remainder as a top-level command. Balanced parens inside block echo
REM      are FINE (re-verified), so do not "fix" the balanced ones below.
REM   2. a variable whose VALUE contains a right paren, e.g. a path under
REM      "Program Files x86" - same effect, because expansion happens first.
REM Flattening: the abort paths below are top-level labels, not nested if-blocks.
REM Observable reason: in the nested form one abort printed its banner yet the
REM script still exited 0. NOTE - an independent re-review could NOT reproduce
REM that with a minimal nested exit /b, so treat the flattening as a deliberate
REM clarity/robustness choice for THIS file. Do NOT cite it as a general rule
REM that nested exit /b never propagates.
REM Encoding: this file is UTF-8 without BOM, but cmd decodes it with the OEM
REM code page. A controlled A/B - all non-ASCII bytes replaced, same length and
REM logic - removed 11 stderr garbage lines from this region alone, so misparses
REM DO happen and currently land harmlessly. Every other .bat in this repo is
REM pure ASCII; ASCII-izing this file is a tracked follow-up. Do NOT re-save it
REM as ANSI or GBK - those trail bytes can be backslash, pipe or brace.
if not exist "%HW_PROBE%" goto :hw_probe_clean
if not exist "%OUT%" goto :hw_probe_recover
echo ============================================================
echo  BUILD ABORTED: product dir AND probe leftover BOTH exist.
echo ============================================================
echo    product : %OUT%
echo    leftover: %HW_PROBE%
echo.
echo  A previous run was interrupted between probe-rename and rename-back,
echo  and a product dir was created afterwards. The leftover may hold user
echo  data dir and .env - inspect it before deleting anything.
echo  Fix: merge or move %HW_PROBE% as you see fit, then re-run.
echo  NOTE: this is NOT a file lock - taskkill will not help here.
echo ============================================================
exit /b 1

:hw_probe_recover
echo   recovering probe leftover: ren %HW_PROBE% back to %OUT%
ren "%HW_PROBE%" "HiveWeave" >nul 2>&1
if not errorlevel 1 goto :hw_probe_clean
echo BUILD ABORTED: cannot restore probe leftover to %OUT%
echo    rename failed: %HW_PROBE%  -^>  HiveWeave
exit /b 1

:hw_probe_clean
set HW_DIRLOCKED=0
set HW_PROBED=0
if not exist "%OUT%" goto :hw_probe_done
set HW_PROBED=1
ren "%OUT%" "HiveWeave.__lockprobe__" >nul 2>&1
if errorlevel 1 goto :hw_probe_locked
ren "%HW_PROBE%" "HiveWeave" >nul 2>&1
if not errorlevel 1 goto :hw_probe_done
echo ============================================================
echo  BUILD ABORTED: probe rename-back FAILED - dir stuck at
echo    %HW_PROBE%
echo ============================================================
echo  The ren-out succeeded, so the dir is NOT locked; the rename
echo  back failed for another reason - transient handle or AV scan.
echo  Fix: rename %HW_PROBE% back to HiveWeave manually, re-run.
echo ============================================================
exit /b 1

:hw_probe_locked
set HW_DIRLOCKED=1

:hw_probe_done
if "%HW_PROCS%%HW_DIRLOCKED%"=="00" goto :hw_lock_ok

echo.
echo ============================================================
echo  BUILD ABORTED: the product directory is in use.
echo ============================================================
if "%HW_PROCS%"=="1" for /f "tokens=2 delims=," %%p in ('tasklist /FI "IMAGENAME eq HiveWeave.exe" /FO CSV /NH 2^>nul') do echo   running PID %%~p
echo   lock probe (ren product dir): locked=%HW_DIRLOCKED%   (1=locked/ren failed, 0=free)
echo   (the "both exist" and "rename-back failed" cases abort BEFORE this point)
echo.
echo  PyInstaller must DELETE dist\HiveWeave, but the running instance holds
echo  data\ (SQLite DB) and _internal\*.dll, so the build would fail after
echo  several minutes. Details of the probe:
echo    - ren'ing the exe FILE may still succeed, so a file-only check LIES;
echo      what matters is whether the DIRECTORY can be renamed/deleted.
echo    - closing the platform window is NOT enough: this app is two-window
echo      (platform + ball); closing only the platform window leaves the
echo      process alive by design (ball keeps the loop running).
echo.
echo  Fix: exit from the ball window menu, or run:
echo        taskkill /IM HiveWeave.exe /F
echo  Then re-run build-exe.bat.
echo.
echo  NOTE: if you renamed the exe, the name check cannot see it - the
echo        directory probe above is the authoritative signal.
echo ============================================================
exit /b 1

:hw_lock_ok
echo   OK - no running instance, product dir is not locked.

echo [1/5] Building frontend (pnpm --filter @hiveweave/web build)...
call pnpm --filter @hiveweave/web build
if errorlevel 1 (
  echo FRONTEND BUILD FAILED
  exit /b 1
)

echo [2/5] Syncing backend deps (uv sync --extra dev --extra desktop)...
cd apps\hiveweave-py
uv sync --extra dev --extra desktop
if errorlevel 1 (
  echo UV SYNC FAILED
  exit /b 1
)
cd ..\..

REM ── 用户数据保留（模型配置/项目登记全在 EXE 同级 data/ 与 .env）────
REM PyInstaller COLLECT 会整删 dist\HiveWeave 再重建——先把 data/ 与 .env
REM 挪到 dist\.hw-preserve，构建完成后回填。上次构建若中途失败留下
REM stranded preserve，这里先回填再继续（preserve 视作最近已知数据）。
REM
REM ⚠ 不变式（2026-09-15 审计 P1-4）：探针跑过 ⇒ 目录必须已经还回来。
REM   否则下面 `if exist "%OUT%\data"` **静默跳过** ⇒ 旧 data/ 不会被保留，
REM   而 PyInstaller 随后重建 dist ⇒ 旧数据变孤儿、.env 被 release.env 覆盖。
REM   首次构建 HW_PROBED=0，天然放行。
REM   ⚠ 同样扁平化：不用 `( )` 块包 `exit /b 1`。理由与边界见上面 [0/5] 的
REM     MEASURED NOTES（注意那条**不是**"嵌套 exit /b 一律失效"的通用结论）。
if not "%HW_PROBED%"=="1" goto :hw_inv_ok
if exist "%OUT%" goto :hw_inv_ok
echo BUILD ABORTED: product dir missing after lock probe: %OUT%
echo   （探针把目录 ren 出去却没 ren 回来 ⇒ 用户数据保留会被静默跳过）
exit /b 1
:hw_inv_ok
echo [3/5] Preserving user data (data/ + .env)...
if exist "%PRESERVE%\data" (
  if exist "%OUT%\data" (
    rmdir /s /q "%PRESERVE%\data"
  ) else (
    call :hw_merge_tree "%PRESERVE%\data" "%OUT%\data"
    if errorlevel 1 exit /b 1
  )
)
if exist "%PRESERVE%\.env" (
  if not exist "%OUT%\.env" copy /y "%PRESERVE%\.env" "%OUT%\.env" >nul
  del /q "%PRESERVE%\.env" >nul 2>&1
)
if exist "%OUT%\data" (
  mkdir "%PRESERVE%" 2>nul
  call :hw_merge_tree "%OUT%\data" "%PRESERVE%\data"
  if errorlevel 1 (
    echo DATA MOVE FAILED - HiveWeave.exe 仍在运行？先 taskkill /f /im HiveWeave.exe
    exit /b 1
  )
  REM 兜底门禁必须紧跟在这里：此刻白名单已搬走、其余仍在原位，
  REM 正好能看出"哪些留下会被 PyInstaller 连同删掉"。
  call :hw_scan_unknown "%OUT%\data"
  if errorlevel 1 exit /b 1
)
if exist "%OUT%\.env" (
  mkdir "%PRESERVE%" 2>nul
  copy /y "%OUT%\.env" "%PRESERVE%\.env" >nul
  if errorlevel 1 (
    echo ENV COPY TO PRESERVE FAILED - 中止构建以保护原始 .env
    exit /b 1
  )
)
goto :hw_preserve_done

REM ══ 子过程：选择性搬运 data/ 内容（2026-09-17）══════════════════════
REM 为什么不再整目录 move：**实测每次构建白搬 372 MB**——
REM   data\webview\ 212 MB（WebView2 storage_path，纯浏览器缓存；launcher 每次
REM     启动 `mkdir(parents=True, exist_ok=True)` 会重建 + default profile 重填）
REM   data\logs\    160 MB（服务端/壳的追加日志，本日已加轮转）
REM   两者都不是"用户数据"，搬它们只是拖慢每一次构建。
REM
REM **保留白名单**（code-derived；不在表内的视为可重建）：
REM   hiveweave.db      : Meta DB —— 模型配置/项目登记/全局设置，唯一不可再生
REM   git-anchor\       : git 信任锚 `.id`（删掉要重新走用户授权流程）
REM   assistant\        : 平台级助理系统工作区（隐藏系统项目）
REM   skill_cache\      : 技能详情磁盘缓存（有 TTL，重建要重新抓取源）
REM   *.json            : ball_position.json / process_registry.json 等小状态
REM
REM **全集（2026-09-17 从代码枚举，勿凭记忆改）**：`get_data_root()` 的消费者只有
REM   config.py:43 assistant / :48 ball_position.json / :192 hiveweave.db /
REM   git_anchor.py:293 git-anchor / skill_registry.py:1254 skill_cache /
REM   win_subprocess.py:230 empty-git-hooks / launcher webview / uvicorn+shell logs。
REM   其中 **empty-git-hooks 故意不保留**：它由 `mkdir(parents=True, exist_ok=True)`
REM   重建、且**必须为空**（`core.hooksPath` 指向它来禁用钩子）⇒ 搬它毫无收益。
REM
REM ⚠ **白名单的固有软肋 = 新增路径会被静默漏掉**（本文件已把"静默跳过"列为
REM   致命形状）。故 [3/5] 搬完后会调 `:hw_scan_unknown` 兜底：把 data\ 下
REM   **既不在白名单、又还留在原位**的顶层项列出来 —— 留在原位就说明
REM   PyInstaller 会连它一起删掉，那是真丢数据的路径，故**直接中止**让人决策。
REM
REM ⚠ **实测踩点（2026-09-17，逐条跑过，别凭直觉改）**：
REM   ① `/IF` 是**纯文件**过滤器，**匹配不到目录**（`/IF git-anchor` → rc=0、
REM      一个文件都不拷）。故目录必须**逐个单独调用**，不能写进 `/IF`。
REM   ② 反之 `/XD`（黑名单）**会递归**排除同名子目录（`/XD logs` 会连
REM      `skill_cache\logs\` 一起排掉）⇒ 不能用"黑名单 + 1 个 robocopy"。
REM   ③ `/E` 也是递归的，会把 `logs\*.log` 顺着白名单扩展名捞进来 ⇒ 必须
REM     用 `/S`（只复制有文件的子目录，不改变根选择语义）。
REM   ④ 目录不存在时**必须 `if exist` 守卫**：直接拿不存在的路径当源会
REM     报"系统找不到指定的路径"（rc=16），被下面的 fail-loud 判据拦成
REM      假失败（`empty-git-hooks\` 在某些部署里不一定存在）。
REM
REM ⚠ **失败必须 fail-loud**：robocopy /MIR < maxexit 只放行 0-7，8（至少一个
REM   文件失败）/16（严重错误）一律中止。若静默放行，PyInstaller 随后清掉原位
REM   ⇒ 用户数据真丢。`:hw_copy_one` 把这两条口径收在一处，避免逐处重复。
REM
REM 顶层小文件：`/MIR /IF` 会把 `logs`、`webview` 建成**空目录**（因为 /MIR
REM 按"目录不存在"补建），但**不落任何文件**——空目录无体积、无副作用，
REM 且下次 launcher 启动本来就会重建，故接受。
:hw_merge_tree
REM ⚠⚠ 必须是 `/S`（递归但不 purge），**绝不能**用 `/MIR`（2026-09-17 实测）。
REM   为什么：`/IF *.json hiveweave.db` 只是**文件**过滤器 —— 它保护得了
REM   目标端的文件（dest-only 的 *.txt/*.dat 都实测存活），但**保护不了目录**。
REM   实测 `/MIR /IF *.json hiveweave.db` 会把目标端"源里没有的目录"整个删掉：
REM     dest 有 empty-git-hooks\keepme ⇒ 跑完 dest 只剩 git-anchor\ + hiveweave.db
REM     （rc=3，**不报错、不提示**，静默删）
REM   同组实测对照：`/S` 下同一目录**存活**。
REM   生产后果（回填路径）：`%OUT%\data` 里凡"白名单没搬、但确实存在"的目录
REM   （首当其冲是豁免的 `empty-git-hooks\`）都会被静默抹掉。故改 `/S`。
call :hw_copy_one "%~1" "%~2" /S /IF *.json hiveweave.db
if errorlevel 1 exit /b 1
REM ⚠ 下面每一行"搬运"都必须在 :hw_is_known_dir 里**同名登记**（登记在"搬运名单"区），
REM   否则末端的门禁会把**刚搬走的目录**判成未知路径并中止构建（实为本文件 2026-09-17
REM   踩过的自伤：assistant/skill_cache 搬走了却没登记 ⇒ 每次构建必假失败，
REM   而且失败点在搬运**之后**，data 已经被搬出去一半）。
REM   两边同源纪律见 :hw_is_known_dir 顶部的注释。
call :hw_copy_dir "%~1" "%~2" git-anchor
if errorlevel 1 exit /b 1
call :hw_copy_dir "%~1" "%~2" assistant
if errorlevel 1 exit /b 1
call :hw_copy_dir "%~1" "%~2" skill_cache
if errorlevel 1 exit /b 1
REM browse = 每项目的 browse 取证产物（browse-audit.jsonl / console.log /
REM network.log），QA 验收要读它 ⇒ **不是可重建缓存，必须保存**。
REM （09-17 实测：data\browse\<uuid>\ 下确有 3 个文件，436 KB。）
call :hw_copy_dir "%~1" "%~2" browse
if errorlevel 1 exit /b 1
exit /b 0

REM 兜底门禁：列出 data\ 下"既没被搬走、又没被豁免"的顶层目录。
REM 留下即会被 PyInstaller 删掉 ⇒ 中止让人决策（新增路径可能是新的不可再生数据）。
REM 参数 %1 = 原位的 data 目录（此刻尚未被 PyInstaller 清掉）。
:hw_scan_unknown
REM 目录维度：`/ad` 只列目录。白名单里全是目录名 ⇒ 这一层不需要文件豁免表。
for /f "delims=" %%I in ('dir /b /ad "%~1" 2^>nul') do (
  call :hw_is_known_dir "%%I"
  if errorlevel 1 (
    echo USER DATA GATE FAILED: data\%%I 既不在搬运名单也不在豁免名单，构建会删掉它
    echo   if not rebuildable: add a call :hw_copy_dir in :hw_merge_tree
    echo   if rebuildable    : add an exemption below in :hw_is_known_dir
    exit /b 1
  )
)
REM 文件维度（2026-09-17 审计 P1-1 补）：`/ad` 不看文件，而搬运的
REM   `/IF *.json hiveweave.db` 只认这两个模式 ⇒ data\ 下**新增的顶层文件**
REM   （实测 newstate.txt / something.wal）既不匹配 `*.json`、也不被本门禁枚举，
REM   **静默丢失**（PyInstaller 会清 data\）。这正是门禁想防的形状，故补这一层。
REM   当前真实存在的顶层文件只有 hiveweave.db 与 process_registry.json，都在
REM   `*.json` 模式内 ⇒ 门禁对今天**不会有任何新报错**，只在未来新增文件类型时
REM   才会拦人（那正是目的：fail-loud 而非静默丢）。
for /f "delims=" %%J in ('dir /b /a-d "%~1" 2^>nul') do (
  call :hw_is_known_file "%%J"
  if errorlevel 1 (
    echo USER DATA GATE FAILED: data\%%J 不匹配搬运的 /IF 模式，构建会删掉它
    echo   fix: extend the /IF patterns in :hw_merge_tree
    exit /b 1
  )
)
exit /b 0

REM data\ 顶层**文件**豁免表：与 :hw_merge_tree 的 `/IF` 模式逐项对应。
REM `*.json` 覆盖 ball_position.json / process_registry.json；hiveweave.db 具名。
REM
REM ⚠ 为什么用**后缀字符串比较**而不是 `findstr` 正则：本仓纪律禁用文本子串/
REM   正则判据（改措辞即绕过、且转义跨 shell 易错）。这里判的是**文件名后缀**
REM   这个确定的状态量，`.json` 与 `hiveweave.db` 两点由 :hw_merge_tree 的
REM   `/IF` 模式唯一决定。`%%~x1` 取扩展名（不含点前缀），无需正则。
:hw_is_known_file
if /i "%~x1"==".json" exit /b 0
if /i "%~1"=="hiveweave.db" exit /b 0
exit /b 1

REM ⚠⚠ 单一真值源纪律：本子过程必须与 :hw_merge_tree 的搬运清单**逐项对齐**。
REM   两条清单分开维护 = 迟早漂移（本日实测漂移一次，后果是构建假失败）。
REM   改动其中一边时，必须同时改另一边，否则门禁会把正常目录判成未知。
REM   「搬运」= 会被保存的目录；「豁免」= 可重建、无需保存的目录。
REM   名录来自**代码派生**（grep `get_data_root()` 的全部消费点），不是拍脑袋：
REM     config.py:43 assistant / :48 ball_position.json(文件) / :192 hiveweave.db(文件)
REM     git_anchor.py:293 git-anchor / win_subprocess.py:230 empty-git-hooks
REM     launcher.py:685 webview / browse 工具 browse\
:hw_is_known_dir
REM ── 搬运名单（与 :hw_merge_tree 一一对应，会被保存）──
if /i "%~1"=="git-anchor" exit /b 0
if /i "%~1"=="assistant" exit /b 0
if /i "%~1"=="skill_cache" exit /b 0
if /i "%~1"=="browse" exit /b 0
REM ── 豁免名单（可重建，不保存也不报错）──
REM logs            = 追加日志（util/log_rotate.py 已加轮转，无保留价值）
REM webview         = WebView2 storage_path 缓存，launcher 每次启动重建
REM empty-git-hooks = 必须**保持空目录**（core.hooksPath 指向它来屏蔽仓库 hooks），
REM                   win_subprocess.py:230 每次 `mkdir(parents=True, exist_ok=True)`
REM                   重建 ⇒ 搬运反而有风险（带进多余文件会让 hooks 生效）。
if /i "%~1"=="logs" exit /b 0
if /i "%~1"=="webview" exit /b 0
if /i "%~1"=="empty-git-hooks" exit /b 0
exit /b 1

:hw_copy_one
REM `%%~1`/`%%~2` 是源/目标；其余参数（开关 + 匹配模式）**原样透传**。
REM
REM ⚠ cmd 的 `shift` 会**同步右移 `%%~N`**（`%%~1` 与 `%1` 同源，`~` 只负责剥掉
REM   外层引号），所以"先取走前两个、再 shift 两次"的顺序是对的，shift 之后的
REM   `%1..%9` 正好是"原本的第 3 个参数起"，即透传区。**但必须用 `set` 落快照**：
REM   `if ... (` 块内的 `%var%` 在**块解析时**就展开完了，之后再引用会拿到旧值。
set "_hw_src=%~1"
set "_hw_dst=%~2"
shift
shift
robocopy "%_hw_src%" "%_hw_dst%" %1 %2 %3 %4 %5 %6 %7 %8 %9
if errorlevel 8 exit /b 1
exit /b 0

:hw_copy_dir
REM 扁平写法（不用 `if ... (` 块）：`%%~N` 虽不受块展开影响，但块内一旦有人
REM 未来加一句 `%var%` 就会踩提前展开的坑。本文件 [0/5] 的 MEASURED NOTES
REM 已经把"块内 exit /b"列为历史事故点，这里保持扁平。
if not exist "%~1\%~3" exit /b 0
call :hw_copy_one "%~1\%~3" "%~2\%~3" /S
if errorlevel 1 exit /b 1
exit /b 0

:hw_preserve_done

echo [4/5] Running PyInstaller...
cd apps\hiveweave-py
uv run pyinstaller --noconfirm --clean ..\desktop\hiveweave.spec --distpath ..\desktop\dist --workpath ..\desktop\build
if errorlevel 1 (
  echo PYINSTALLER FAILED
  exit /b 1
)
cd ..\..

if not exist "%OUT%\HiveWeave.exe" (
  echo EXE MISSING: %OUT%\HiveWeave.exe
  exit /b 1
)

echo [5/5] Copying exe-sibling assets (ball/ web/ bin/) + restoring user data...
if not exist "apps\web\dist\index.html" (
  echo WEB DIST INCOMPLETE: apps\web\dist\index.html missing
  exit /b 1
)
robocopy apps\desktop\ball "%OUT%\ball" /MIR /NFL /NDL /NJH /NJS >> "%OUT%\build-assets.log" 2>&1
if errorlevel 8 exit /b 1
REM ── web/ 镜像：排除美术中间产物 + 先清目标端（2026-09-17 实测）───────
REM 问题：/MIR 是「镜像源目录全部内容」，**不读 .gitignore** —— 于是
REM apps/web/public/office-assets/ 下被 gitignore 排除的 LaMa 修图中间产物
REM 照样被拷进产物。实测 7 个文件 / **7.93 MB**：
REM   office-scene-bg.with-desks.bak.png    2.75 MB
REM   office-scene-bg.patched.png           2.71 MB
REM   office-scene-bg.pre-lama2.bak.png     2.69 MB
REM   office-desk-{back,front}.pre-{chair,iso}.bak.png  ~0.1 MB
REM 运行时零引用：ASSET_URLS（components/office/constants.ts）只指向无后缀
REM 生产图 office-scene-bg.png；全仓唯一提到 .bak 名的是同文件 138 行的
REM **注释**（解释「原带桌版备份在哪个文件」），不是 URL。故排除安全。
REM
REM ⚠ /XF 的实测边界（务必看完再改）：/XF 只排除**复制**，**不豁免删除**——
REM   robocopy 把被排除文件对两边都视为「不存在」，故 /MIR 的 purge 阶段
REM   也**不会**删掉目标端已残留的同名文件（实测：目标端预置 stale.bak.png
REM   + /XF *.bak.png → rc=3 且该文件**仍在**）。
REM   ⇒ 光靠 /XF，若目标端已有旧产物，它们会一直躺在那。故此处**先清 web/**
REM   再镜像，不依赖 purge。清空是安全的：web/ 全部内容都由本步骤由
REM   apps\web\dist 重建，无用户数据（用户数据在 data/ 与 .env，另段处理）。
REM
REM 注：全量构建时 [4/5] PyInstaller COLLECT 已整删 dist\HiveWeave，web/
REM 本就不存在 ⇒ 这段清除是**幂等**的防御；它真正兜住的是「增量/重跑/
REM COLLECT 未如期清空」等非预期状态。
REM 根治（未做，建议排期）：让 LaMa 中间产物直接输出到 tasks/ 或
REM observations/，不进 public/ —— 否则 public/ 永远在给「顺手存图」当回收站。
if exist "%OUT%\web" rmdir /s /q "%OUT%\web"
if exist "%OUT%\web" (
  echo WEB DIR PURGE FAILED: %OUT%\web 仍在（被占用？）
  exit /b 1
)
robocopy apps\web\dist "%OUT%\web" /MIR /NFL /NDL /NJH /NJS /XF *.bak.png *.patched.png >> "%OUT%\build-assets.log" 2>&1
if errorlevel 8 exit /b 1
REM 出厂门禁：确认中间产物确实没进产物（防 /XF 被误删/写错后静默放行）。
REM
REM ⚠ 判据选型（2026-09-17 独立审计 D1/D2 修正；旧写法有假阳性 + 漏检）：
REM   **旧写法**：`dir /b /s ... > "%TEMP%\hw_bak.txt"` + findstr。三个洞：
REM     ① **重定向失败不截断旧文件** —— 若该临时文件已存在且不可覆盖
REM        （只读/被占用/ACL 拒），dir 写不进去、**旧内容仍在** ⇒ findstr
REM        读到陈旧命中 ⇒ **门禁误杀构建**（审计已实测复现：attrib +R 后
REM        findstr rc=0，文件内容仍是上一轮的）；
REM     ② `%TEMP%` 未定义时路径降级成 `\hw_bak.txt`（写 C 盘根），同样
REM        可能读到历史残留；
REM     ③ 只查 `*.bak.png`，而 `*.patched.png` **不被该模式匹配** ⇒
REM        `.patched.png` 完全没有门禁（本次入包的恰有
REM        office-scene-bg.patched.png 2.71MB）。
REM
REM **现写法**：`robocopy /L`（只列不拷、**不落任何文件**）读退出码。
REM   不落盘 ⇒ 根本不存在「陈旧读取」这一面，这是它优于 dir+findstr 的根因。
REM   实测退出码三值（直调，非推断）：
REM     源存在 + 无匹配 ⇒ **rc=0**  ⇒ 放行
REM     源存在 + 有匹配 ⇒ **rc=1**  ⇒ 中止（就是「中间产物进包了」）
REM     源不存在/不可访问 ⇒ **rc=16** ⇒ 中止（异常态，fail loud 是安全侧）
REM   故判据为 `if errorlevel 1`：rc>=1 一律中止。它把 rc=16 也归入中止，
REM   这是**有意的**——源目录都读不到时不该继续出包。
REM ⚠ 必须同时列两种模式（见洞③）：`*.bak.png *.patched.png`。
REM ⚠ `/L` 的目标目录参数只是占位（/L 下绝不创建/写入），用 %TEMP% 下
REM   不存在的路径即可，不会污染磁盘；但**源路径必须真实存在**，
REM   否则撞 rc=16 误判（本次调试踩过：POSIX 路径 /tmp/x 会被解析成
REM   D:\tmp\x 而报「错误 3 系统找不到指定的路径」）。
robocopy "%OUT%\web" "%TEMP%\hw_gate_nonexistent" /L /S /NJH /NJS /NS /NC /FP /NDL *.bak.png *.patched.png >nul 2>&1
if errorlevel 1 (
  REM 注：此 echo 文案刻意全用全角括号/无括号 —— 块内 echo 里的
  REM 半角括号必须转义且极易写错，本文件历史上已被括号坑过两次
  REM （见顶部 MEASURED NOTES），故不引入转义括号。
  echo ARTIFACT GATE FAILED: 美术中间产物进了产物目录，或产物 web/ 不可读
  echo   下面列出被检出的文件，请检查 /XF 是否失效：
  robocopy "%OUT%\web" "%TEMP%\hw_gate_nonexistent" /L /S /NJH /NJS /NS /NC /FP /NDL *.bak.png *.patched.png 2>nul
  exit /b 1
)
if not exist "%OUT%\bin" mkdir "%OUT%\bin"
copy /y apps\web\node_modules\agent-browser\bin\agent-browser-win32-x64.exe "%OUT%\bin\" >> "%OUT%\build-assets.log" 2>&1
if errorlevel 1 (
  echo AGENT-BROWSER COPY FAILED
  exit /b 1
)

REM 回填用户数据（data/ 按白名单搬回 + .env 覆盖回）。任一恢复失败
REM 立即中止且**保留 preserve 现场**——PyInstaller 已清掉原位，preserve
REM 是唯一副本，带锁重试不能删（审计 P1-1：无条件 rmdir 会销毁用户数据）。
REM
REM ⚠ 与 [3/5] 用**同一个** `:hw_merge_tree` 白名单子过程：两处若各写一份
REM   清单，迟早漂移成"搬出去的东西回不来"（那是真丢数据）。
if exist "%PRESERVE%\data" (
  if not exist "%OUT%\data" mkdir "%OUT%\data" 2>nul
  call :hw_merge_tree "%PRESERVE%\data" "%OUT%\data"
  if errorlevel 1 (
    echo DATA RESTORE FAILED - 用户数据保留在 %PRESERVE%\data，请关占用后重试
    exit /b 1
  )
)
if exist "%PRESERVE%\.env" (
  copy /y "%PRESERVE%\.env" "%OUT%\.env" >nul
  if errorlevel 1 (
    echo ENV RESTORE FAILED - 用户 .env 保留在 %PRESERVE%\.env，请手动回填
    exit /b 1
  )
)
if exist "%PRESERVE%" rmdir /s /q "%PRESERVE%"

REM ── 生成分发配置（fixlist #4① / P0-4）──────────────────────────
REM 此前本脚本只在构建间「保留 / 回填」已有 .env，**从不生成** ⇒ 首次
REM 打包产物没有 .env ⇒ launcher._frozen_bootstrap_env 走
REM `if is_file(): load else: pass`（静默跳过缺失 referent）⇒ 预算静默
REM 回落到代码默认值（实测 hard=570 而 dev .env 写 1710，4 个 run 精确
REM 死在 600.0s）。故此处**在构建期生成**，而不是留到首启：
REM   · 首启时 EXE 可能装在 Program Files 这类只读目录，写入必失败；
REM   · 构建期生成 = 产物面自带配置，与源码面彻底分离。
REM 三条纪律：
REM   1) 已有 .env **永远优先**（上一段用户数据回填刚放回来的，或在
REM      dist 里手工调过的）—— 绝不覆盖用户/运维的配置；
REM   2) 模板丢了就 **exit /b 1**（fail-closed）—— 宁可不出包，也不出
REM      一个「没配置的包」，那正是 P0-4 的形状；
REM   3) 落点 <exe>/.env —— 与 launcher 的 `frozen_env_file_present` 探针
REM      及 `_frozen_bootstrap_env` 的读取点同一处。
if not exist "%OUT%\.env" (
  if not exist "apps\desktop\release.env" (
    echo RELEASE ENV TEMPLATE MISSING: apps\desktop\release.env
    echo   分发产物必须自带配置，缺模板即中止（不产出无配置产物）
    exit /b 1
  )
  copy /y "apps\desktop\release.env" "%OUT%\.env" >nul
  if errorlevel 1 (
    echo RELEASE ENV GENERATION FAILED: %OUT%\.env
    exit /b 1
  )
  echo   generated %OUT%\.env - from apps\desktop\release.env
) else (
  echo   kept existing %OUT%\.env (user config always wins)
)

REM 出厂门禁：产物必须确证吃上配置（缺 .env 的包不许出厂）。
REM --require-env 的判据是 fail-closed 的（未打印 frozen_env_file_present
REM 也判失败），所以这条同时兜住"陈旧的旧产物"。
if exist "%OUT%\.env" (
  echo [release gate] checking artifact env: %OUT%\.env
) else (
  echo RELEASE ENV MISSING AFTER GENERATION - refusing to ship %OUT%
  exit /b 1
)

echo.
echo ============================================================
echo  DONE: %OUT%\HiveWeave.exe
echo  User data preserved: %OUT%\data + .env
echo  Smoke: python scripts\smoke_release.py --require-env
echo  Run  : "%OUT%\HiveWeave.exe"
echo ============================================================
endlocal
