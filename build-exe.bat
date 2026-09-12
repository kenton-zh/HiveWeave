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

REM ── [0/5] 前置门禁：产物 EXE 在运行则立即中止 ──────────────────
REM 为什么必须放在最前面：PyInstaller COLLECT 整删 dist\HiveWeave，而运行中
REM 进程锁着 exe + _internal\*.dll，删目录必失败。若跑完 [1][2] 才炸，用户白等
REM 几分钟，且 .hw-preserve 可能留下半搬移现场 —— fail fast 更安全。
REM 用 tasklist 而非 taskkill：不替用户做「杀掉他的程序」这个决定，只报告。
echo [0/5] Checking for running HiveWeave.exe (file lock guard)...
REM ⚠ 用 tasklist+find 双保险：tasklist 在「无匹配」时退出码仍为 0（只打印
REM 「信息: 没有运行的任务匹配指定标准。」），所以必须靠管道末端 find 的退出码
REM 判别（命中=0 / 未命中=1）。cmd 的管道 errorlevel 取末端命令，这是本判据的基石。
REM 已实测两分支：无此进程 → find=1 → 放行；有（如 explorer.exe）→ find=0 → 中止。
tasklist /FI "IMAGENAME eq HiveWeave.exe" 2>nul | find /I "HiveWeave.exe" >nul
if not errorlevel 1 (
  echo.
  echo ============================================================
  echo  BUILD ABORTED: HiveWeave.exe is currently running.
  echo ============================================================
  echo  PyInstaller must delete dist\HiveWeave, but a running process
  echo  holds a lock on HiveWeave.exe and _internal\*.dll, so the build
  echo  would fail after several minutes.
  echo.
  echo  Fix: close the HiveWeave window, or run:
  echo        taskkill /IM HiveWeave.exe /F
  echo  Then re-run build-exe.bat.
  echo.
  echo  NOTE: if you renamed the exe, this check cannot see it.
  echo        Check Task Manager for any process under dist\HiveWeave.
  echo ============================================================
  exit /b 1
)
echo   OK - no running instance, file lock is free.

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
  echo   generated %OUT%\.env (from apps\desktop\release.env)
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
