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
    move /y "%PRESERVE%\data" "%OUT%\data" >nul
  )
)
if exist "%PRESERVE%\.env" (
  if not exist "%OUT%\.env" copy /y "%PRESERVE%\.env" "%OUT%\.env" >nul
  del /q "%PRESERVE%\.env" >nul 2>&1
)
if exist "%OUT%\data" (
  mkdir "%PRESERVE%" 2>nul
  move /y "%OUT%\data" "%PRESERVE%\data" >nul
  if errorlevel 1 (
    echo DATA MOVE FAILED - HiveWeave.exe 仍在运行？先 taskkill /f /im HiveWeave.exe
    exit /b 1
  )
)
if exist "%OUT%\.env" (
  mkdir "%PRESERVE%" 2>nul
  copy /y "%OUT%\.env" "%PRESERVE%\.env" >nul
  if errorlevel 1 (
    echo ENV COPY TO PRESERVE FAILED - 中止构建以保护原始 .env
    exit /b 1
  )
)

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
robocopy apps\web\dist "%OUT%\web" /MIR /NFL /NDL /NJH /NJS >> "%OUT%\build-assets.log" 2>&1
if errorlevel 8 exit /b 1
if not exist "%OUT%\bin" mkdir "%OUT%\bin"
copy /y apps\web\node_modules\agent-browser\bin\agent-browser-win32-x64.exe "%OUT%\bin\" >> "%OUT%\build-assets.log" 2>&1
if errorlevel 1 (
  echo AGENT-BROWSER COPY FAILED
  exit /b 1
)

REM 回填用户数据（data/ 整目录 move 回 + .env 覆盖回）。任一恢复失败
REM 立即中止且**保留 preserve 现场**——PyInstaller 已清掉原位，preserve
REM 是唯一副本，带锁重试不能删（审计 P1-1：无条件 rmdir 会销毁用户数据）。
if exist "%PRESERVE%\data" (
  move /y "%PRESERVE%\data" "%OUT%\data" >nul
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
