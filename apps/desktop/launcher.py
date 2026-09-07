"""HiveWeave 桌面悬浮球启动器（spec §10 / D8 / D9，打包税 #2 #3）。

单进程结构（D9）：主线程 pywebview GUI 循环 + 子线程 uvicorn；
``--headless`` 只跑 uvicorn（服务器/CI 无 GUI 降级），pywebview 惰性
导入 —— 无 GUI 环境/未安装 pywebview 时 headless 与 selfcheck 不崩。

窗口参数（F4 承重事实）：on_top / frameless / focus=False / easy_drag=False
+ DRAG_REGION_SELECTOR（球体与面板头，data-drag-region）；窗口移动由
pywebview 拖拽机制承担，js_api 只做位置存取与球态↔展开态缩放。
透明窗口是 F4 自相矛盾点 → 默认不透明保底视觉，env
``HIVEWEAVE_BALL_TRANSPARENT=1`` 才试验性开启（遗留验证清单 #2）。

用法（cwd 建议为 apps/hiveweave-py，.env 灌入依赖 cwd）::

    .venv/Scripts/python.exe -u ../desktop/launcher.py           # 球
    .venv/Scripts/python.exe -u ../desktop/launcher.py --headless
    .venv/Scripts/python.exe -u ../desktop/launcher.py --selfcheck
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

# 球壳窗口几何（§10：球 ≈60px，展开 ≈380px；四周留阴影余量）
BALL_W, BALL_H = 76, 76
PANEL_W, PANEL_H = 380, 540
_STARTUP_TIMEOUT_S = 60.0


def _repo_root_from_launcher() -> Path | None:
    """launcher.py 位于 <repo>/apps/desktop/ → 仓库根（脚本模式）。"""
    here = Path(__file__).resolve()
    if here.parent.name == "desktop" and here.parent.parent.name == "apps":
        return here.parent.parent.parent
    return None


def _prepare_cwd() -> None:
    """把 cwd 切到 apps/hiveweave-py（.env 与默认数据目录约定依赖 cwd）。

    frozen 模式不切（数据一律走 EXE 同级 data/，见 config.get_data_root），
    改做 frozen 启动环境注入（.env/日志/agent-browser/无控制台输出兜底）。
    """
    if getattr(sys, "frozen", False):
        _frozen_bootstrap_env()
        return
    repo = _repo_root_from_launcher()
    if repo is not None:
        backend = repo / "apps" / "hiveweave-py"
        if backend.is_dir():
            os.chdir(backend)
            src = backend / "src"
            if src.is_dir() and str(src) not in sys.path:
                sys.path.insert(0, str(src))
            # 镜像 _frozen_bootstrap_env：.env 必须先于 main() 的端口解析
            # 灌入，否则脚本模式下 .env 的 HIVEWEAVE_PORT/HOST 被静默忽略
            # （_bootstrap_dotenv 要到 import config 才跑，晚了——审计实锤）
            env_file = backend / ".env"
            if env_file.is_file():
                try:
                    from dotenv import load_dotenv

                    load_dotenv(env_file, override=False)
                except Exception:
                    pass


def _frozen_bootstrap_env() -> None:
    """Frozen EXE 启动环境（全部 setdefault 语义：显式导出优先）。

    - ``<exe>/.env``：与脚本模式 ``apps/hiveweave-py/.env`` 同语义灌入
      （dotenv override=False；包内 ``__init__._bootstrap_dotenv`` 在 onedir
      下 parents[2] 恰为 exe 目录也能命中，此处是 onefile/布局变化的双保险）。
    - ``HIVEWEAVE_LOG_FILE``：frozen 下 ``main._default_log_file`` 落
      PyInstaller 解包临时目录（退出即焚），默认改指数据根 logs/。
    - ``HIVEWEAVE_BROWSE_BIN``：``<exe>/bin/agent-browser-*.exe`` ——
      ``resolve_browse_bin`` 的 node_modules 祖先走查在 frozen 下不可达，
      与 Electron 壳同一注入法（bin 名口径对齐 config.agent_browser_bin_name）。
    - 窗口版（console=False）``sys.stdout/stderr`` 为 None：重定向到
      ``<exe>/data/logs/launcher.out.log``，print / FATAL 不丢也不炸。
    """
    exe_dir = Path(sys.executable).resolve().parent
    logs_dir = exe_dir / "data" / "logs"

    env_file = exe_dir / ".env"
    if env_file.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file, override=False)
        except Exception:
            pass

    os.environ.setdefault(
        "HIVEWEAVE_LOG_FILE", str(logs_dir / "server.out.log")
    )

    if not (os.environ.get("HIVEWEAVE_BROWSE_BIN") or "").strip():
        name = None
        if sys.platform == "win32":
            name = "agent-browser-win32-x64.exe"
        elif sys.platform == "darwin":
            import platform as _platform

            name = (
                "agent-browser-darwin-arm64"
                if _platform.machine() == "arm64"
                else "agent-browser-darwin-x64"
            )
        if name:
            bin_path = exe_dir / "bin" / name
            if bin_path.is_file():
                os.environ["HIVEWEAVE_BROWSE_BIN"] = str(bin_path)

    if sys.stdout is None or sys.stderr is None:
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            # 文本模式 + 行缓冲：print/structlog 都写 str，二进制 FileIO
            # （"ab"+buffering=0）会让第一条日志 TypeError（审计 B1 实锤）
            out = open(  # noqa: SIM115 —— 进程级常驻句柄，随进程生命周期
                logs_dir / "launcher.out.log", "a", encoding="utf-8", buffering=1
            )
            sys.stdout = out
            sys.stderr = out
        except Exception:
            pass  # 无处可写就维持 None：print 失败静默（GUI 模式本无控制台）


def _load_position() -> dict:
    """读球位置（数据根 ball_position.json —— spec §10 位置记忆）。"""
    try:
        from hiveweave.config import get_ball_position_file

        p = get_ball_position_file()
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            x, y = data.get("x"), data.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                return {"x": int(x), "y": int(y)}
    except Exception:
        pass
    return {}


def _save_position(x: int | None, y: int | None) -> None:
    try:
        if x is None or y is None:
            return
        from hiveweave.config import get_ball_position_file

        p = get_ball_position_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"x": int(x), "y": int(y)}), encoding="utf-8"
        )
    except Exception:
        pass  # 位置记忆失败不影响运行


class BallApi:
    """pywebview js_api 桥 — 窗口移动/位置存取/球态缩放（§10）。

    pywebview 在后台线程调用这些方法；webview 对象经 ``_win`` 注入。
    """

    def __init__(self) -> None:
        self._win = None
        self._server = None

    def attach(self, win, server) -> None:
        self._win = win
        self._server = server

    # ── 内部 ────────────────────────────────────────────────
    def _cur_xy(self) -> tuple[int | None, int | None]:
        try:
            return getattr(self._win, "x", None), getattr(self._win, "y", None)
        except Exception:
            return None, None

    # ── JS 桥方法 ───────────────────────────────────────────
    def save_position(self) -> dict:
        x, y = self._cur_xy()
        _save_position(x, y)
        return {"ok": True, "x": x, "y": y}

    def expand(self) -> dict:
        """球态 → 展开态（保持左上角锚点，向右下扩）。"""
        try:
            if self._win is not None:
                self._win.resize(PANEL_W, PANEL_H)
        except Exception:
            pass
        return {"ok": True}

    def collapse(self) -> dict:
        try:
            if self._win is not None:
                self._win.resize(BALL_W, BALL_H)
                self.save_position()
        except Exception:
            pass
        return {"ok": True}

    def get_api_key(self) -> str:
        """审计 M3：HIVEWEAVE_API_KEY 启用时球页经此取 key（同进程内存，
        不落盘不入 URL）。无 key 部署返回空串，球页按原样匿名请求。"""
        try:
            from hiveweave.config import settings

            return str(getattr(settings, "api_key", "") or "")
        except Exception:
            return ""

    def open_main(self) -> dict:
        """打开主界面：优先 Vite dev :5173，未运行则回落 :4000（打包税 #2）。"""
        import webbrowser

        try:
            from hiveweave.config import settings

            port = settings.port
        except Exception:
            port = 4000
        url = os.environ.get("HIVEWEAVE_MAIN_URL") or ""
        if not url:
            import urllib.request

            dev = "http://127.0.0.1:5173"
            try:
                urllib.request.urlopen(dev, timeout=1)
                url = dev
            except Exception:
                url = f"http://127.0.0.1:{port}/"
        webbrowser.open(url)
        return {"ok": True, "url": url}

    def hide_ball(self) -> dict:
        """隐藏到托盘的 P0 替代：藏窗口（进程与后端保留）。"""
        try:
            if self._win is not None:
                self._win.hide()
        except Exception:
            pass
        return {"ok": True}

    def quit(self) -> dict:
        try:
            if self._server is not None:
                self._server.should_exit = True
            # 关掉全部窗口（球 + 平台主窗）再停后端：只销毁球会留下一个
            # 后端已死的平台窗（2026-09-07 双窗口改造）
            import webview as _webview

            for w in list(_webview.windows):
                try:
                    w.destroy()
                except Exception:
                    pass
        except Exception:
            pass
        return {"ok": True}


def _run_selfcheck() -> int:
    """无人环境静态验收：可导入、数据根可解析、前端构建产物自洽。"""
    failures: list[str] = []

    from hiveweave.config import (
        get_assistant_workspace,
        get_ball_position_file,
        get_data_root,
        is_frozen,
        resolve_web_dist,
        settings,
    )

    print(f"frozen={is_frozen()}")
    print(f"data_root={get_data_root()}")
    print(f"meta_db={settings.get_meta_db_path()}")
    print(f"web_dist={resolve_web_dist()}")
    print(f"assistant_workspace={get_assistant_workspace()}")
    print(f"ball_position_file={get_ball_position_file()}")

    from hiveweave.api.ball import resolve_ball_static_dir

    ball_dir = resolve_ball_static_dir()
    print(f"ball_static_dir={ball_dir}")
    if ball_dir is None:
        print("WARN: ball static dir not found (fallback page will serve)")
        failures.append("ball_static_dir_missing")

    import hiveweave.main as main_mod  # noqa: F401  (app 可构建)

    print("backend_app_import=ok")

    for name in (
        "services.assistant",
        "services.ball_bridge",
        "services.user_message",
        "api.ball",
    ):
        __import__(f"hiveweave.{name}")
        print(f"import {name}=ok")

    try:
        import webview  # noqa: F401

        print("pywebview=installed")
    except Exception:
        print("pywebview=NOT installed (GUI disabled; --headless still works)")

    if failures:
        print("SELFCHECK FAIL: " + ", ".join(failures))
        return 1
    print("SELFCHECK OK")
    return 0


def _wait_server(server, timeout: float, thread=None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return True
        if getattr(server, "should_exit", False):
            return False
        # bind 失败时 uvicorn 在线程内 sys.exit（不置 started/should_exit），
        # daemon 线程死了主线程无感——必须查线程存活，否则 GUI 模式假死满
        # 60s（审计建议 #2）
        if thread is not None and not thread.is_alive():
            return False
        time.sleep(0.2)
    return False


def _port_free(port: int, host: str) -> bool:
    """裸 bind 探测（不加 SO_REUSEADDR，与 uvicorn 实际绑定同语义；
    宁可跳过 TIME_WAIT 的端口也不撞车）。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _resolve_port(requested: int, host: str) -> int:
    """端口占用自动顺延（opencode 同款思路）：默认 4000 被占（最常见=
    dev 后端在跑）时从 requested+1 起最多试 20 个；全部被占则原样返回
    让 uvicorn 给出它自己的报错。显式 --port 也回退——桌面场景可用性优先，
    落地端口有日志与 open_main 对齐，不会悄悄漂。"""
    if _port_free(requested, host):
        return requested
    for candidate in range(requested + 1, requested + 21):
        if _port_free(candidate, host):
            print(f"[HiveWeave] port {requested} busy -> using {candidate}")
            return candidate
    print(
        f"[HiveWeave] no free port in {requested}..{requested + 20}, "
        f"will fail on {requested}"
    )
    return requested


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HiveWeave 桌面悬浮球启动器")
    parser.add_argument("--headless", action="store_true", help="只跑后端，不开球窗口")
    parser.add_argument("--url", default="", help="覆盖球页面 URL（默认 :<port>/ball）")
    parser.add_argument("--port", type=int, default=None, help="后端端口（默认 HIVEWEAVE_PORT/4000）")
    parser.add_argument("--selfcheck", action="store_true", help="无人环境静态自检后退出")
    args = parser.parse_args(argv)

    _prepare_cwd()

    if args.selfcheck:
        return _run_selfcheck()

    import uvicorn

    # 端口解析必须在任何 hiveweave 导入之前：config.settings 是导入期单例，
    # 先把回退后的端口写进 HIVEWEAVE_PORT 再导 settings，进程内所有
    # settings.port 消费方（lifespan 日志 / ball open_main 回落）才与
    # uvicorn 实际绑定一致。
    try:
        _requested = args.port or int(
            (os.environ.get("HIVEWEAVE_PORT") or "").strip() or 4000
        )
    except ValueError:
        print(
            f"[HiveWeave] invalid HIVEWEAVE_PORT="
            f"{os.environ.get('HIVEWEAVE_PORT')!r}, falling back to 4000"
        )
        _requested = args.port or 4000
    _host = (os.environ.get("HIVEWEAVE_HOST") or "").strip() or "127.0.0.1"
    port = _resolve_port(_requested, _host)
    os.environ["HIVEWEAVE_PORT"] = str(port)

    from hiveweave.config import settings
    # 传 app 对象而非 "hiveweave.main:app" 字符串：frozen 下 uvicorn 的
    # 字符串导入走 importlib，PyInstaller 静态分析看不到这条引用链，
    # 传对象让 Analysis 顺着 launcher → hiveweave.main 自然收集。
    from hiveweave.main import app as hiveweave_app

    host = settings.host or _host

    if getattr(sys, "frozen", False):
        # frozen 下球页 open_main 的回落必须是本进程 :port（dist 已挂载），
        # 不再探测 dev :5173；显式 --port 时尤其重要（open_main 只认
        # HIVEWEAVE_MAIN_URL / settings.port，看不到 CLI 覆盖值）。
        os.environ.setdefault("HIVEWEAVE_MAIN_URL", f"http://127.0.0.1:{port}/")

    config = uvicorn.Config(
        hiveweave_app,
        host=host,
        port=port,
        workers=1,
        log_config=None,  # structlog 已配置，别让 uvicorn 再动 logging
        timeout_keep_alive=30,
    )

    if args.headless:
        # 打包税 #3：headless = 主线程跑 uvicorn（正常信号处理）。
        # 勿用 uvicorn.run(config)：run() 第一参数是 app，会把 Config 当
        # ASGI 应用包进一个新的默认 Config（端口回落 8000、lifespan 丢失
        # ——2026-09-07 EXE 冒烟实锤），与 GUI 分支同用 Server(config)。
        uvicorn.Server(config).run()
        return 0

    # GUI 模式：子线程 uvicorn，主线程 pywebview（D9：GUI 循环必须主线程）
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    server_thread.start()
    if not _wait_server(server, _STARTUP_TIMEOUT_S, server_thread):
        print(
            f"FATAL: backend did not start on {host}:{port} within "
            f"{int(_STARTUP_TIMEOUT_S)}s",
            file=sys.stderr,
        )
        return 1

    ball_url = (
        args.url
        or os.environ.get("HIVEWEAVE_BALL_URL", "").strip()
        or f"http://127.0.0.1:{port}/ball"
    )

    try:
        import webview
    except Exception as e:
        print(
            "FATAL: pywebview 未安装（GUI 模式需要；headless 不需要）。"
            f"安装：uv sync --extra desktop（或 pip install pywebview）。原因：{e}",
            file=sys.stderr,
        )
        server.should_exit = True
        return 1

    # DRAG_REGION_SELECTOR（§10）：球体与面板头都标 data-drag-region。
    # 必须 webview.settings["DRAG_REGION_SELECTOR"] —— pywebview 6.x 把它
    # 改成只读 module_property，老的 `webview.DRAG_REGION_SELECTOR = x`
    # 赋值静默不落地（2026-09-07 实测：settings 仍为默认
    # .pywebview-drag-region，选择器永不匹配 → 球窗不可拖）。
    webview.settings["DRAG_REGION_SELECTOR"] = "[data-drag-region]"

    pos = _load_position()
    api = BallApi()
    transparent = os.environ.get("HIVEWEAVE_BALL_TRANSPARENT", "").lower() in (
        "1", "true", "yes"
    )
    # 置顶挡屏逃生口：HIVEWEAVE_BALL_ON_TOP=0 关 TopMost（默认开=F4 设计）
    on_top = os.environ.get("HIVEWEAVE_BALL_ON_TOP", "1").strip().lower() not in (
        "0", "false", "no",
    )
    # OS 球开关（2026-09-07 用户澄清：悬浮球是应用内组件，网页端同源；
    # frozen 默认只开平台窗——球在页面里。OS 独立球改 opt-in：
    # HIVEWEAVE_BALL=1 才同开（脚本模式 start-ball.bat 默认仍开球）。
    _ball_default = "0" if getattr(sys, "frozen", False) else "1"
    ball_on = os.environ.get("HIVEWEAVE_BALL", _ball_default).strip().lower() in (
        "1", "true", "yes",
    )
    win = None
    if ball_on:
        win = webview.create_window(
            "HiveWeave",
            ball_url,
            js_api=api,
            width=BALL_W,
            height=BALL_H,
            x=pos.get("x"),
            y=pos.get("y"),
            frameless=True,      # 无边框（F4）
            on_top=on_top,       # Windows TopMost（F4；env 可关）
            focus=False,         # 不抢焦点（F4）
            easy_drag=False,     # 拖动只认 DRAG_REGION_SELECTOR（§10）
            transparent=transparent,  # 默认 False：不透明保底视觉（F4 矛盾点）
        )
    api.attach(win, server)

    # frozen 产品形态：双击 EXE = 平台主窗口（用户预期「启动看到平台」，
    # 2026-09-07 反馈）。HIVEWEAVE_MAIN_WINDOW=0 回落只开球。
    platform_win = None
    if getattr(sys, "frozen", False):
        main_on = os.environ.get(
            "HIVEWEAVE_MAIN_WINDOW", "1"
        ).strip().lower() not in ("0", "false", "no")
        if main_on:
            platform_win = webview.create_window(
                "HiveWeave 平台",
                f"http://127.0.0.1:{port}/",
                width=1600,
                height=1000,
                min_size=(1024, 700),
            )

    # 兜底：MAIN_WINDOW=0 + BALL=0 会一个窗口都没有（webview.start 空窗
    # 报错）——至少保一个平台窗。
    if not webview.windows:
        platform_win = webview.create_window(
            "HiveWeave 平台", f"http://127.0.0.1:{port}/"
        )

    # private_mode=False + storage_path：WebView2 localStorage 持久化
    # （网页端 apiKey/设置存 localStorage，默认隐私模式每次启动全丢）。
    # 只 frozen 生效；脚本模式维持默认不扰开发。
    start_kwargs: dict = {}
    if getattr(sys, "frozen", False):
        try:
            from hiveweave.config import get_data_root

            storage = get_data_root() / "webview"
            storage.mkdir(parents=True, exist_ok=True)
            start_kwargs = {"private_mode": False, "storage_path": str(storage)}
        except Exception:
            pass
        # 关窗确认文案汉化（confirm_close 弹的是 pywebview 内建 MessageBox）
        start_kwargs["localization"] = {
            "global.quitConfirmation": (
                "确定要退出 HiveWeave 吗？\n运行中的项目将被停止（全员下班）。"
            )
        }

    # 条件关窗确认：有项目在跑（上班中）才弹确认，闲时直接关。
    # confirm_close 是普通属性、winforms on_closing 关闭时才读 → 守护线程
    # 轮询 /api/projects 动态翻转即可，不动 GUI 线程。
    #
    # 代理纪律（用户 09-07 钦定，参照 DSH util/http-proxy）：内部 loopback
    # 轮询**一律直连**——ProxyHandler({}) 让 env/注册表代理全部失明（代理
    # 吃掉自家 loopback 流量 = 路由环，绕过不是可选项）；需要代理的外部
    # 流量由用户显式自己配（后端 httpx 读 env 的现状不动）。
    if platform_win is not None and getattr(sys, "frozen", False):
        import json as _json
        import threading as _th
        import urllib.request as _ur

        def _watch_running_projects(win, backend_port: int) -> None:
            url = f"http://127.0.0.1:{backend_port}/api/projects"
            key = (os.environ.get("HIVEWEAVE_API_KEY") or "").strip()
            headers = {"x-api-key": key} if key else {}
            opener = _ur.build_opener(_ur.ProxyHandler({}))  # 永远直连
            while True:
                running = False
                try:
                    req = _ur.Request(url, headers=headers)
                    with opener.open(req, timeout=2) as r:
                        data = _json.loads(r.read().decode("utf-8"))
                    running = any(
                        p.get("isStarted") or p.get("is_started")
                        for p in data.get("projects", [])
                    )
                except Exception as e:
                    running = False  # 后端未就绪/临时故障 → 不拦关窗
                    print(
                        f"[confirm-watch] poll failed: {e}", file=sys.stderr
                    )
                try:
                    if win.confirm_close != running:
                        win.confirm_close = running
                        print(
                            f"[confirm-watch] confirm_close -> {running}",
                            flush=True,
                        )
                except Exception:
                    return  # 窗口已销毁 → 守护线程随之退出
                time.sleep(3)

        print("[confirm-watch] started", flush=True)
        _th.Thread(
            target=_watch_running_projects,
            args=(platform_win, port),
            name="confirm-close-watch",
            daemon=True,
        ).start()

    webview.start(**start_kwargs)
    # GUI 退出 → 停后端，进程随 daemon 线程结束
    server.should_exit = True
    server_thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
