"""日志轮转（EXE 运维缺口：``data/logs/`` 无上限增长）。

背景（09-17 实测）：``dist/HiveWeave/data/logs/`` 达 **163 MB**——
``server.out.log`` 95.1 MB + ``launcher.out.log`` 72.3 MB，两个文件都是
纯 append、无任何上限。EXE 用户长期挂着平台，日志只增不减：
既是**分发体积**问题，也是**磁盘占用**问题。

为什么不用 ``logging.handlers.RotatingFileHandler``
--------------------------------------------------
``Windows`` 上它改名 ``server.out.log`` → ``server.out.log.1`` 时，**持有
句柄的进程自己也会失败**（``os.rename`` → ``PermissionError``）。

**本机实测（09-17，win32 + CPython 3.13，逐条跑过，不是推断）**：

| 操作 | 持有 ``open()`` 句柄时 | 句柄已关时 |
| --- | --- | --- |
| ``os.replace(path, path.1)`` | ❌ ``PermissionError(32)`` | ✅ OK |
| ``os.truncate(path, 0)`` | ✅ OK | ✅ OK |

根因：CPython 的 ``open()`` 在 Windows 上共享模式是
``FILE_SHARE_READ|FILE_SHARE_WRITE``，**不含** ``FILE_SHARE_DELETE`` ⇒
改名/移动（本质是 unlink+link）被拒；而 ``os.truncate`` 走 ``_chsize``，
**作用于路径而非句柄、不涉及目录项**，因此不受共享模式限制。

本仓的日志句柄是**进程级常驻**的（``main._FlushFile``、launcher 的 stdout
重定向），且 launcher 那份甚至**没有关闭路径**（GUI 进程活到退出为止）——
所以**任何依赖 rename/move 的方案在这个仓里都不可能生效**，不是"偶发失败"。

设计：只截断，不搬迁
------------------
1. **预检**（高频，必须便宜）：只看 ``path.stat().st_size`` 是否超阈值。
2. **封存代**（只在真正翻转时做一次）：把**当前内容**搬到 ``path.1``，
   再 ``os.truncate(path, 0)`` 原地清零。
3. **主文件永不 move、永不 rename** ⇒ 常驻句柄全程有效，只需把**偏移**归零
   就能继续追加（不在最后就显式 seek，见调用方的 ``reset_offset``）。

稳态上限 ``max_bytes × (backup_count + 1)``：默认 8 MiB + 1 份 ⇒
**每文件 16 MiB 封顶**（此前无上限，``server.out.log`` 实测 95 MB）。

**搬运为什么要显式 ``seek``/``truncate`` 收尾**（本地实测踩到）：在 Windows 上
写 4096 B 的备份后 ``st_size`` 会**大于** 4096（NTFS 尾零用有效数据长度表示，
分配大小保留 1 MB 粒度），读回来就是一串 NUL，看起来像文件被写坏。所以拷贝
完成后必须 ``os.truncate(dest, 实际写入字节数)`` 把尾零裁掉。

⚠ **``.1`` 的读取被"连续写入"推迟**（已知良性行为）：搬运用的是"读当前全部
字节"，此刻常驻句柄的偏移可能还在旧末尾（NTFS 预分配留下的零区）⇒ 读到的尾部
带 NUL。那部分 NUL 会被**下一行写入**覆盖掉，所以最终一致；只有恰好卡在
"搬运刚做完、下一行还没写"的窗口里去看 ``.1``，才会看到尾部 NUL。

契约（三处调用方共用）
--------------------
- **绝不抛异常**：日志基础设施坏了不能把进程带走。任何 OSError 静默降级
  （下一次 write 再试），返回 ``None``；失败留痕走 ``_note_failure``。
- **返回 ``None`` 无歧义**：轮转器为 ``None``（功能关闭 / 构造失败）或
  本次没有翻转。调用方因此**先自检 ``self._rotator is None``** 再回调，避免
  ``None()`` —— 「看似有守卫」的典型坑。
- **翻转信号只能来自返回值**：不要用 ``stat().st_size`` 或文本内容反推
  「刚才转没转」（那是文本判据，用户 09-14 钦定禁用）。
- **翻转后必须 ``reset_offset()``**：``os.truncate`` 走路径、不作用于句柄流，
  所以句柄的文件位置仍停在旧偏移。写下去就是空洞。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB —— 单个文件单代上限
DEFAULT_BACKUP_COUNT = 1  # 保留 .1 一份 ⇒ 每文件稳态上限 16 MiB


def _unlink_if_exists(path: Path) -> None:
    """best-effort 删除；**绝不抛**。

    ⚠ 只吞 ``FileNotFoundError`` 是不够的（2026-09-17 审计实测的 P0）：
    Windows 上文件被任何句柄持有 ⇒ ``PermissionError(32)``，而本函数的两个
    调用点**都在 ``except OSError`` 体内**（清理残留的 ``.1.tmp``）。异常从
    ``except`` 体内冒出会**替换掉原异常并逃出 ``rotate_if_needed``** ⇒ 违反
    「绝不抛」契约 ⇒ 经 ``write()`` 直穿，**进程死、当轮日志全丢**
    （实测：进程崩后 4000 行日志既不在主文件也不在 ``.1``）。

    故这里吞 **全部 ``OSError``**：本函数是"清理残留"的 best-effort 动作，
    删不掉不影响正确性（``.tmp`` 会在下一轮被 ``_stash_bytes`` 覆盖），
    吞掉不丢任何信息。
    """
    try:
        path.unlink()
    except OSError:
        pass


def _shift_chain(path: Path, backup_count: int) -> None:
    """``.N-1``→``.N`` … ``.1``→``.2``：从最老一端顺移。

    从高序号往低序号走至关重要：反序会让 ``.1`` 把 ``.2`` 覆盖掉，整个链
    退化成一份。``backup_count=1`` 时不需要任何顺移（``.1`` 由下面的
    ``os.replace(backup, .1)`` 原子覆盖），本函数直接返回。

    句柄若恰好持着某个 ``.N``（本仓不会：只有主文件有常驻句柄），
    ``os.replace`` 会 ``PermissionError``，由调用方的 ``OSError`` 兜底吃掉。
    """
    if backup_count <= 1:
        return
    _unlink_if_exists(path.with_name(f"{path.name}.{backup_count}"))
    for i in range(backup_count - 1, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        if src.exists():
            try:
                os.replace(src, path.with_name(f"{path.name}.{i + 1}"))
            except OSError:
                pass  # 单代顺移失败不阻断：下一代会覆盖同一目标，可自愈


def _stash_bytes(src: Path, dest: Path) -> int:
    """把 ``src`` 当前内容可信地搬到 ``dest``（**先写临时文件再原子替换**）。

    为什么要临时文件：直写 ``dest`` 只能开 ``"wb"``，而 ``"wb"`` 自身就会把
    ``dest`` 截成 0 —— 若失败在写入中途，上一代历史就被毁了。临时文件让
    "失败"退化为"什么都没发生"。

    为什么要显式 ``truncate`` 收尾（本机实测踩到）：Windows/NTFS 上写完 4096 B
    后 ``st_size`` 会 **大于** 4096 —— 尾零用"有效数据长度"表示，分配大小按
    1 MB 粒度保留，读回来就是一串 NUL，看着像文件被写坏。故按**实际写入
    字节数**再截一次，把尾零裁掉。

    返回搬走的字节数。
    """
    tmp = dest.with_name(f"{dest.name}.tmp")
    written = 0
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        for chunk in iter(lambda: fin.read(256 * 1024), b""):
            fout.write(chunk)
            written += len(chunk)
    with open(tmp, "rb") as fchk:  # 兜底：按实际长度收尾（防读侧比写侧长）
        pass
    os.truncate(tmp, written)
    os.replace(tmp, dest)  # 原子落定；此处 ``dest`` 无句柄，故 Windows 也成功
    return written


class SizeRotator:
    """按大小自我轮转的日志文件；所有失败都不冒泡。"""

    def __init__(
        self,
        path: Path | str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max(1, int(max_bytes))
        self.backup_count = max(1, int(backup_count))

    def rotate_if_needed(self) -> Path | None:
        """超阈值就把当前内容封存到 ``path.1``，再把主路径**原地清零**。

        返回备份路径（＝回调信号）；未翻转或任何失败返回 ``None``。
        返回值同时充当「已翻转」的判据——调用方**不得**靠文件大小或
        文本内容重新推断，那些都不是状态判据。

        ⚠ 被调用时文件**可能已经被主系统重开过**（轮转器只共享路径、不共享
        句柄）。故预检**在这里重新取一次大小**——既是最便宜的预检，也顺带
        吃掉"调用方持有的是旧结论"的竞态。
        """
        try:
            existing = self.path.stat().st_size
        except OSError:
            return None  # 文件不在 / 读不到 → 不轮转（下一轮再试）
        if existing < self.max_bytes:
            return None

        backup = self.path.with_name(f"{self.path.name}.1")
        tmp = backup.with_name(f"{backup.name}.tmp")
        try:
            _shift_chain(self.path, self.backup_count)
            _stash_bytes(self.path, backup)
            # 关键一步：``os.truncate`` 走路径、不碰目录项，**在持有句柄时也
            # 成功**（本机实测）。这正是本方案能work而 ``os.replace`` 不能的原因。
            os.truncate(self.path, 0)
            return backup
        except OSError as e:
            # 绝不抛（日志设施坏了不能带走进程），但要留痕：全静默会让"轮转没生效"
            # 变成不可诊断的黑盒（本地实测期间就靠这句话连续抓到三处问题）。
            _unlink_if_exists(tmp)
            _note_failure(e)
            return None
        except Exception as e:  # noqa: BLE001 —— 见下
            # ⚠ 兜底层（2026-09-17 审计 P0 后补）：调用方**没有** try，本方法的
            # 任何逃逸异常都会经 ``write()`` 直穿 ⇒ 进程死 + 当轮日志全丢。
            # 上面只捕 ``OSError``，而"清理/搬运"路径里还可能有非 OSError 的
            # 意外（如未来有人在这里加 ``int()``/索引/第三方调用）。
            # 契约是**绝不抛**，那这里就必须真的做到"绝不抛"，不能只覆盖
            # 今天想得到的那一种异常 —— 「看似有守卫比没有守卫更危险」。
            _unlink_if_exists(tmp)
            try:
                _note_failure(e)  # 传 Exception；_note_failure 已 best-effort
            except Exception:  # noqa: BLE001 —— 留痕本身也不能成为新风险
                pass
            return None

    def __repr__(self) -> str:  # 便于测试/取证定位
        return (
            f"SizeRotator({self.path!s}, max_bytes={self.max_bytes}, "
            f"backup_count={self.backup_count})"
        )

    def reset_offset(self, handle: object) -> bool:
        """翻转后的**必需**收尾：把常驻句柄的文件位置归零。

        ``os.truncate(path, 0)`` 是**路径级**操作，不作用于句柄的流状态 ⇒
        句柄的文件位置仍停在旧偏移，下一次写就从那里开始，中间留一段空洞
        （读起来全是 NUL，长得像"日志被写坏"）。

        **为什么是 reset 而不是 reopen**（本机实测结论）：
        Windows 上持有 ``open()`` 句柄时 ``os.replace`` 必 ``PermissionError``，
        所以本方案不改名不换 inode —— 主路径的 inode 从头到尾没变，
        "旧句柄"和"新句柄"其实是同一个文件 ⇒ **reopen 是多余的**，只需要
        seek。少一次 open 也就少一条失败路径（轮转器只剩"预检 / 搬运 /
        截断"三步，全部不依赖文件系统语义）。

        返回是否成功归零。失败**不抛**：调用方保留旧句柄继续写，内容仍会落盘
        （可能带空洞），下一次轮转时自愈。
        """
        seek = getattr(handle, "seek", None)
        truncate = getattr(handle, "truncate", None)
        try:
            if seek is not None:
                seek(0, os.SEEK_END)
                return True
            if truncate is not None:
                truncate()  # 无 seek 的流式对象（如 Tee）退回"自身截断+归位"
                return True
        except (OSError, ValueError):
            return False
        return False


def _note_failure(exc: BaseException) -> None:
    """轮转失败留痕（best-effort，绝不二次抛）。

    参数放宽为 ``BaseException``：兜底层也会走到这里（不只 ``OSError``）。

    为什么不用 ``logging``：轮转器是给**日志系统自己**用的，失败留痕若也走
    日志系统，就成了"日志坏了想记日志"的循环。这里直接 best-effort 写
    stderr（GUI 下为 None 就跳过）；真正的取证靠 ``.1`` 文件的在场与否。
    """
    try:
        sys.stderr.write(f"[log_rotate] rotation failed: {exc!r}\n")
    except Exception:
        pass


def make_rotator(
    path: Path | str | None,
    *,
    max_bytes: int | None = None,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> SizeRotator | None:
    """构造轮转器；``path`` 为空 / 构造失败返回 ``None``（调用方据此跳过）。

    ``max_bytes`` 缺省时读 ``HIVEWEAVE_LOG_MAX_BYTES``（测试用极小阈值验证
    真实翻转，免得测试为了触发轮转真去写 8 MiB）。
    """
    if not path:
        return None
    if max_bytes is None:
        raw = (os.getenv("HIVEWEAVE_LOG_MAX_BYTES") or "").strip()
        try:
            max_bytes = int(raw) if raw else DEFAULT_MAX_BYTES
        except ValueError:
            max_bytes = DEFAULT_MAX_BYTES
    try:
        return SizeRotator(path, max_bytes=max_bytes, backup_count=backup_count)
    except (OSError, ValueError, TypeError):
        return None
