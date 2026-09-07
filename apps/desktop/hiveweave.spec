# -*- mode: python ; coding: utf-8 -*-
"""HiveWeave Windows EXE — PyInstaller spec（打包税 #3 单进程壳）。

产物：onedir `dist/HiveWeave/`（HiveWeave.exe + _internal/）。ball/、web/、
bin/agent-browser、data/ 按 frozen 口径放在 **EXE 同级**，由 build-exe.bat
在构建后拷入（不进 _internal —— config.resolve_* 的 frozen 候选全按
exe-sibling 解析）。

构建：仓库根 `build-exe.bat`（或 cd apps/hiveweave-py &&
uv run pyinstaller --noconfirm --clean ../desktop/hiveweave.spec
--distpath ../desktop/dist --workpath ../desktop/build）。
"""

import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

SPECDIR = os.path.abspath(SPECPATH)  # apps/desktop
BACKEND_SRC = os.path.abspath(os.path.join(SPECDIR, "..", "hiveweave-py", "src"))

hidden = (
    # launcher→hiveweave.main 是字面 import（静态可见），但包内大量惰性
    # import（services.assistant / api.ball 等）+ uvicorn 的 auto 族走
    # importlib —— 全量收集，宁多勿漏。
    collect_submodules("hiveweave")
    + [
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.middleware.proxy_headers",
        "dotenv",
        "structlog",
        "aiosqlite",
        # pywebview Windows 后端链：winforms 需要 pythonnet/clr_loader，
        # 官方 hook 收 webview 自身，但 clr 的 runtime config 数据要手动带
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "clr_loader",
        "clr_loader.netfx",
        "pythonnet",
    ]
)

datas = (
    collect_data_files("clr_loader")
    + collect_data_files("webview")
)

binaries = collect_dynamic_libs("pythonnet")

a = Analysis(
    [os.path.join(SPECDIR, "launcher.py")],
    pathex=[BACKEND_SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 纯减重：GUI 壳用不到的 GUI 工具箱 / 测试链
        "tkinter",
        "curses",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HiveWeave",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 窗口版；stdout/stderr 由 launcher._frozen_bootstrap_env 重定向到 data/logs
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HiveWeave",
)
