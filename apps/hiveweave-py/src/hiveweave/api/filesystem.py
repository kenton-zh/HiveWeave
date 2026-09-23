"""Filesystem operation endpoints (contract 19, group 16).

契约 19: Filesystem — 浏览 + 读 + 写 + grep（路径限定在项目工作空间内）
- GET  /api/filesystem/browse?path=&projectId=   列目录
- GET  /api/filesystem/read?path=&projectId=     读文件
- POST /api/filesystem/write                     写文件
- GET  /api/filesystem/grep?pattern=&path=&projectId=  内容搜索
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

import structlog

from hiveweave.db import meta as meta_db

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/filesystem", tags=["filesystem"])

# 独立 router 用于全局文件系统浏览（无 prefix，前端调用 /api/fs/browse）
fs_router = APIRouter(tags=["filesystem"])

#: 单次读取最大字节数（防 OOM）
_MAX_READ_BYTES = 512 * 1024
#: 单次 grep 最大返回行数
_MAX_GREP_LINES = 200


def _resolve_safe(workspace: str, rel_path: str) -> Path:
    """把 rel_path 解析到 workspace 内的绝对路径，拒绝越界。"""
    ws = Path(workspace).resolve()
    target = (ws / rel_path).resolve()
    try:
        target.relative_to(ws)
    except ValueError:
        raise HTTPException(
            status_code=403, detail="Path escapes project workspace"
        )
    return target


async def _workspace_for(project_id: str) -> str:
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Project workspace not found")
    return workspace


@router.get("/browse")
async def browse(
    path: str = Query(default=""),
    projectId: str = Query(...),
) -> dict:
    """列目录（返回 entries: name/type/size）。"""
    workspace = await _workspace_for(projectId)
    target = _resolve_safe(workspace, path)
    # 审计 2026-09-12：本端点此前**没有**走单一入口 —— 下面的循环只跳过
    # 「名字叫 .hiveweave 的子项」，所以把 `.hiveweave` 自身当 path 传进来时，
    # data.db / tool_outputs/ / merge-quarantine/ 的名字与大小照样被列出。
    # 按读语义过一次同一策略：放行 6 个工作子目录 + 只读的 merge-quarantine，
    # 拦住 .hiveweave 根与 tool_outputs/ 等受保护区。
    _reject_protected_hiveweave(workspace, target, write=False)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if target.is_file():
        return {"path": path, "entries": [], "isFile": True}
    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            # 跳过 .hiveweave 内部目录
            if child.name == ".hiveweave":
                continue
            try:
                stat = child.stat()
                size = stat.st_size
            except OSError:
                size = 0
            entries.append(
                {
                    "name": child.name,
                    "type": "directory" if child.is_dir() else "file",
                    "size": size,
                }
            )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    return {"path": path, "entries": entries, "isFile": False}


def _reject_protected_hiveweave(
    workspace: str, target: Path, *, write: bool = False
) -> None:
    """HTTP 与工具层共用同一 .hiveweave 保护策略（治根：单一入口）。

    ``write`` 必须由调用方按端点语义显式传入（read → False / write → True）。
    漏传会让写端点继承"读放行"的口径：`merge-quarantine` 是**只读**放行的
    子目录（report TEST_DSH_54 #5），读放行/写保护 —— 写端点不传就等于
    把平台自管的隔离区向 HTTP 敞开了写（审计 2026-09-12 实测：改动前 403，
    漏传后 200）。拒绝理由也按操作类型分列，与工具层文案同口径。
    """
    from hiveweave.tools.file import _check_hiveweave_dir

    if _check_hiveweave_dir(str(target), workspace, write=write):
        detail = (
            "Path targets protected .hiveweave internals (write denied)"
            if write
            else "Path targets protected .hiveweave internals (read denied)"
        )
        raise HTTPException(status_code=403, detail=detail)


@router.get("/read")
async def read_file(
    path: str = Query(...),
    projectId: str = Query(...),
) -> dict:
    """读文件（限 512KB，二进制返回 base64? 此处按文本返回）。"""
    workspace = await _workspace_for(projectId)
    target = _resolve_safe(workspace, path)
    _reject_protected_hiveweave(workspace, target, write=False)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        data = target.read_bytes()[:_MAX_READ_BYTES]
        text = data.decode("utf-8", errors="replace")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    return {
        "path": path,
        "content": text,
        "size": target.stat().st_size,
        "truncated": target.stat().st_size > _MAX_READ_BYTES,
    }


class WriteFileBody(BaseModel):
    projectId: str
    path: str
    content: str
    append: bool = False


@router.post("/write")
async def write_file(body: WriteFileBody) -> dict:
    """写文件（append=True 追加，否则覆盖）。"""
    workspace = await _workspace_for(body.projectId)
    target = _resolve_safe(workspace, body.path)
    _reject_protected_hiveweave(workspace, target, write=True)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if body.append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(body.content)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Write failed: {e}")
    return {"ok": True, "path": body.path, "bytes": len(body.content)}


@router.get("/grep")
async def grep_files(
    pattern: str = Query(...),
    path: str = Query(default=""),
    projectId: str = Query(...),
    caseInsensitive: bool = Query(default=False),
) -> dict:
    """在工作空间内递归搜索文本（限 200 行匹配）。"""
    workspace = await _workspace_for(projectId)
    root = _resolve_safe(workspace, path)
    if not root.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    flags = re.IGNORECASE if caseInsensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        raise HTTPException(status_code=422, detail=f"Invalid regex: {e}")

    matches: list[dict] = []
    search_root = root if root.is_dir() else root.parent
    try:
        for candidate in search_root.rglob("*"):
            if not candidate.is_file():
                continue
            # 跳过 .hiveweave / 二进制大头文件
            if ".hiveweave" in candidate.parts:
                continue
            try:
                if candidate.stat().st_size > _MAX_READ_BYTES:
                    continue
                text = candidate.read_text(encoding="utf-8", errors="ignore")
            except (OSError, PermissionError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    try:
                        rel = str(candidate.relative_to(workspace)).replace("\\", "/")
                    except ValueError:
                        rel = str(candidate)
                    matches.append({"file": rel, "line": lineno, "text": line[:500]})
                    if len(matches) >= _MAX_GREP_LINES:
                        return {
                            "matches": matches,
                            "truncated": True,
                            "count": len(matches),
                        }
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    return {"matches": matches, "truncated": False, "count": len(matches)}


# ── 全局文件系统浏览（新建项目用，不需要 projectId）────────────

# Windows 驱动器检测
import platform


def _list_windows_drives() -> list[str]:
    """列出 Windows 可用驱动器（如 ['C:\\', 'D:\\']）。"""
    drives: list[str] = []
    if platform.system() != "Windows":
        return drives
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
        for i in range(26):
            if bitmask & (1 << i):
                letter = chr(ord("A") + i)
                drives.append(f"{letter}:\\")
    except Exception:
        # 回退：扫描 A-Z
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:\\"
            if Path(drive).exists():
                drives.append(drive)
    return drives


@fs_router.get("/api/fs/browse")
def fs_browse(path: str = Query(default="")) -> dict:
    """全局文件系统浏览（用于新建项目选择目录）。

    不需要 projectId — 浏览文件系统任意位置。
    返回前端 BrowseResult 格式: currentPath, parentPath, entries, drives。

    ⚠ 刻意用同步 ``def``（不给 ``async def``）：函数体全是阻塞 IO
    （``resolve()`` / ``iterdir()`` / ``stat()``），其中映射盘（如 SMB 的 ``Z:``）
    会真的走网络。写成 ``async def`` 会在事件循环里阻塞**全站** API；写成同步
    ``def`` 后 FastAPI 自动丢进线程池，只阻塞那一个请求（审计 2026-09-23 P2-9）。
    """
    # 清理粘贴噪声：资源管理器「复制文件地址」会带首尾双引号，用户也常带空白
    path = path.strip().strip('"').strip("'").strip()

    # 确定要浏览的路径
    if not path:
        # 默认：用户主目录
        path = str(Path.home())

    target = Path(path).resolve()

    # 路径不存在 ⇒ 明确 404，**不要**静默改写成 home（2026-09-23 审计实证：
    # C:\__nope__ / 带引号的地址 / UNC「上级目录」/ 失效重解析点，原先全部被
    # 静默重定向到 home —— 用户以为自己导航成功了，实际被瞬移到别处，
    # 无法建立「我点到哪了」的因果。这种「看起来能用、实则误导」比报错更糟。）
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    # 如果是文件 → 浏览其父目录
    if target.is_file():
        target = target.parent

    # 计算父目录
    parent = str(target.parent) if target.parent != target else None

    # 列目录内容
    entries: list[dict] = []
    #: 因 stat 失败被降级为 size=0 的条目数（前端据此提示「列表可能不完整」）
    skipped = 0
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            # 跳过隐藏文件和系统目录
            if child.name.startswith(".") and child.name not in (".", ".."):
                # 不跳过 . 开头的目录（用户可能需要）
                pass
            # 跳过 Windows 系统目录（减少噪声）
            if child.name in ("$RECYCLE.BIN", "System Volume Information", "$WinREAgent"):
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                # 整条丢掉，但要计入 skipped —— 否则计数只覆盖「stat 失败」而漏掉
                # 「is_dir 失败」，调用方会误以为列表是完整的（审计 2026-09-23 P2-5）
                skipped += 1
                continue

            if is_dir:
                size = 0
            else:
                # ⚠ 这里的容错不能省（2026-09-23 加固）。失效的重解析点（本机
                # 实测 C:\Users\<user>\jzmq.dll 与 libzmq-mt-4_3_6.dll 均为
                # WinError 3「系统找不到指定的路径」）会让 `stat()` 抛
                # FileNotFoundError。原写法 `child.stat().st_size if child.exists()
                # else 0` 只是**靠三元求值顺序侥幸**躲过（exists() 对失效链接返回
                # False），一旦顺序被改或因竞态出现「exists 为真、stat 失败」，
                # 异常就会冒泡到外层 `except Exception` —— 那会把**整个目录列表**
                # 变成空表，用户看到的是「这个目录选不了」而不是「有一项读不到」。
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                    skipped += 1

            entries.append({
                "name": child.name,
                "path": str(child),
                "fullPath": str(child),
                "isDir": is_dir,
                "is_dir": is_dir,
                "size": size,
            })
    except PermissionError:
        return {
            "currentPath": str(target),
            "parentPath": parent,
            "entries": entries,
            "skipped": skipped,
            "partial": True,
            "drives": _list_windows_drives() if platform.system() == "Windows" else [],
            "error": "Permission denied",
        }
    except Exception as e:
        # 中断也要保住**已经读到的**条目（2026-09-23 审计实证）：返回 [] 会把
        # 「读了一半」变成「这目录是空的」，而前端据此渲染的「（空目录）」是
        # 彻头彻尾的误导（审计用 monkeypatch 让 iterdir 产出 3 条后抛 OSError，
        # 旧代码把 3 条全丢光）。partial=True 让调用方知道列表不完整。
        log.warning("fs_browse_failed", path=str(target), error=str(e))
        return {
            "currentPath": str(target),
            "parentPath": parent,
            "entries": entries,
            "skipped": skipped,
            "partial": True,
            "drives": _list_windows_drives() if platform.system() == "Windows" else [],
            "error": str(e),
        }

    return {
        "path": str(target),
        "currentPath": str(target),
        "parentPath": parent,
        "parent": parent,
        "entries": entries,
        "skipped": skipped,
        "isFile": False,
        "isRoot": parent is None,
        "drives": _list_windows_drives() if platform.system() == "Windows" else [],
    }


# ── 系统原生文件夹选择器（由后端弹窗）────────────────────────────
#
# 为什么必须由**后端**来弹（2026-09-23）：
#   浏览器沙箱下，Web 页面拿不到用户所选目录的**绝对路径** ——
#     · `<input type="file" webkitdirectory>`：不上报路径，且会把目录内文件全传上来；
#     · File System Access API（`showDirectoryPicker`）：**能**弹出系统选择器，
#       但只返回 `FileSystemDirectoryHandle`，**没有 `.path`**（Chromium 仅允许
#       Electron 这类壳暴露该属性）。
#   而 HiveWeave 必须拿到绝对路径才能当项目 workspace。所以「网页端 + 系统原生
#   选择器 + 绝对路径」这三者只有**后端弹窗**能同时满足 —— 后端本来就是本机进程，
#   与用户桌面处在同一会话。
#
#   对照参考：OpenCode 桌面端走的是 `DesktopAPI.openFilePicker` /
#   `electronService.selectDirectory()`，也就是 Electron 原生 dialog —— 与本仓
#   `apps/web/electron/main.cjs:33` 的 `dialog.showOpenDialog` 是同一条路；
#   而它的 **web** 模式同样是自研目录浏览器，并带着完全相同的 Windows 缺陷
#   （anomalyco/opencode#7597：web 模式「打开项目」只列主目录、绝对路径定位不到）。
#   本端点让我们比那条路多一种可用形态：**纯浏览器也能拿到系统原生对话框**。

# ── 线程模型（2026-09-23 审计 P2-2 / P2-3 后重做）────────────────
#
# 为什么不用 ThreadPoolExecutor：它的 worker 是**非 daemon** 的，进程退出时
# atexit 里的 `concurrent.futures.thread._python_exit` 会 join 所有 worker ——
# 系统对话框还开着的时候，后端按 Ctrl+C 会停不掉（审计实测：退出从 0.5s 拖到
# 9.7s，且再按 Ctrl+C 无效，只剩 taskkill /F 这类硬杀通道）。而 `main.py` 明确
# 为「可见控制台」保留了 SIGINT 优雅停机，所以这条不能留。
# 改成 daemon 线程：进程退出不理会它。
#
# 另：锁要保护的是**对话框本身**（acquire 在请求进来时、release 在对话框真正
# 结束后），而不是某个 executor 队列 —— 否则异常/取消路径会提前放锁，下一个
# 请求就变成排队挂死而不是立刻收到 busy。

#: 同一时刻只允许一个系统对话框。**这是"超时自动关闭"的替代方案**：
#: Windows 上没法给 tk 原生目录对话框定时（见 _pick_folder_blocking 的说明），
#: 所以改成"已有窗口在等就直接告诉用户"，既不排队堆积也不会静默卡住。
_picker_lock = threading.Lock()


def _pick_folder_blocking() -> dict:
    """在专用线程里弹**系统原生**文件夹选择器（tkinter → 本机原生目录对话框）。

    返回 ``{"available": bool, "path": str|None, "reason": str|None}``：

    * ``available=False`` ⇒ 无 GUI 会话 / tkinter 不可用；调用方**必须**回退到
      网页版目录浏览器（远程访问、无桌面会话的 EXE 都会走这里）；
    * ``available=True, path=None`` ⇒ 用户取消，调用方应直接收摊。

    ⚠ **为什么不设超时自动关闭**（2026-09-23 实测教训）：Windows 上
    ``askdirectory`` 最终落到原生模态目录对话框，它有自己的消息循环 ——
    Tcl 的 ``after`` 定时器在它等待期间**不会触发**；而
    ``filedialog.Directory`` 继承 ``commondialog.Dialog``、**不是 Widget**，
    根本没有 ``destroy``（写 ``root.after(ms, dialog.destroy)`` 会当场抛
    AttributeError）。所以超时由 ``_picker_lock`` 的"拒绝并发"来替代。

    ⚠ 本函数跑在 **daemon 工作线程** 里（见上面的线程模型）。非 Windows 一律
    直接返回 ``available=False``：macOS 上 Tk 必须在主线程，工作线程里创建 Tk
    可能触发 C 层 abort（``except Exception`` 抓不住，会掀掉整个后端）——
    而我们的分发目标本来就是 Windows EXE，代价为零（审计 2026-09-23 P2-9）。
    """
    if platform.system() != "Windows":
        return {
            "available": False,
            "path": None,
            "reason": f"native picker is Windows-only (got {platform.system()})",
        }

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:  # noqa: BLE001
        return {"available": False, "path": None, "reason": f"tkinter unavailable: {e}"}

    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        # 无桌面会话（服务账号 / 无头运行）会在这里抛 TclError
        return {"available": False, "path": None, "reason": f"no GUI session: {e}"}

    try:
        root.withdraw()
        # 置顶：否则对话框可能藏在浏览器窗口后面，被用户当成「点了没反应」
        try:
            root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        chosen = filedialog.askdirectory(
            parent=root, title="选择工作区目录", mustexist=True
        )
    except Exception as e:  # noqa: BLE001
        return {"available": False, "path": None, "reason": f"dialog failed: {e}"}
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    if not chosen:
        return {"available": True, "path": None, "reason": None}
    return {"available": True, "path": str(chosen), "reason": None}


def _is_local_caller(request: Request) -> bool:
    """只允许**同源/本机**页面触发本机 GUI 对话框。

    为什么需要（2026-09-23 审计 P2-6 实测）：本端点是一个 **CORS 简单请求**
    （无自定义头、无 JSON Content-Type），所以 CORS 白名单只管得住「读响应」，
    管不住副作用 —— 审计用一个 origin=null 的页面成功让本机弹出了对话框。
    于是任何用户访问的网页都能反复弹窗，并把唯一那个对话框槽位占住。

    判据只用 Fetch 元数据（Fetch Metadata Request Headers），**不解析任何自然
    语言**：``Sec-Fetch-Site`` 为 ``same-origin`` / ``none`` 放行；缺失（curl、
    本机 CLI 等非浏览器客户端）也放行 —— 它们是合法调用方；``cross-site`` /
    ``same-site`` 一律拒绝。
    """
    site = request.headers.get("sec-fetch-site", "").strip().lower()
    if not site:
        return True
    return site in ("same-origin", "none")


@fs_router.post("/api/fs/pick-folder")
async def fs_pick_folder(request: Request) -> dict:
    """弹系统原生文件夹选择器并返回**绝对路径**。

    前端约定：

    * ``available=False`` ⇒ 回退网页版目录浏览器（远程 / 无桌面会话 / 非 Windows）；
    * ``busy=True`` ⇒ 已有一个选择窗口在等待，提示用户去完成它；
    * ``path=None`` 且非 busy ⇒ 用户主动取消，直接关闭弹层。
    """
    if not _is_local_caller(request):
        raise HTTPException(status_code=403, detail="Cross-site folder picker denied")

    if not _picker_lock.acquire(blocking=False):
        return {
            "available": True,
            "busy": True,
            "path": None,
            "reason": "已有一个目录选择窗口在等待，请先完成它",
        }

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict] = loop.create_future()

    def _run() -> None:
        try:
            result = _pick_folder_blocking()
        except BaseException as e:  # noqa: BLE001 —— 线程里漏出的异常必须转成结果
            result = {
                "available": False,
                "path": None,
                "reason": f"picker crashed: {e}",
            }
        try:
            # 线程里不能直接 set_result，必须回到事件循环线程
            loop.call_soon_threadsafe(fut.set_result, result)
        except RuntimeError:
            pass  # 事件循环已关（进程正在退出）
        finally:
            _picker_lock.release()  # 锁覆盖对话框的完整生命周期

    # daemon=True：对话框开着时也不拖住进程退出（见上面的线程模型）
    threading.Thread(target=_run, name="folder-picker", daemon=True).start()
    return await fut
