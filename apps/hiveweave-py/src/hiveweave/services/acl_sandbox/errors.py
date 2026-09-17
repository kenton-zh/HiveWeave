"""ACL 沙箱异常（spec docs/spec/windows-acl-sandbox.md §5.6 异常纪律）。"""

from __future__ import annotations


def is_platform_side(exc: BaseException) -> bool:
    """这条异常是否**确属平台侧**（⇒ 归因可落 ``runner_failed``）。

    ## 为什么需要这个判别（2026-09-17 第二轮审计必修）

    ``SandboxUnavailableError`` 是**一切异常的容器** ——
    ``service.py`` 把意外异常（含 ``TypeError`` / ``AttributeError`` 这类真代码
    bug）都包成 ``SandboxUnavailableError(...) from e``。故「归因是平台侧」
    **不能由类型推出**；用它硬编码 ``fact="runner_failed"`` 会把真缺陷说成
    "平台故障"，agent 收到「不是你的 bug」而放弃自查。

    ⚠ 本函数放在 `errors.py` 而**不是**各工具里 —— 判据全平台只有一份；
    每处各写一份正是本仓反复栽的那个坑（「每处各列一份清单」形态的第 3 次复发：
    `dev_server_tools` 与 `bash` 的这条出口结构完全相同）。

    ## 判据（按证据归属）

    看**异常链**（``__cause__`` / ``__context__``）里有没有平台侧的特征打标：

      1. ``platform_side=True`` —— **构造点亲笔声明**。该字段由
         `raise SandboxUnavailableError(..., platform_side=True)` 显式给出，
         用于那些**本身就是平台故障**、但不是某一次具体 Win32 调用失败的
         情形（pywin32 不可用 / 令牌缺 logon SID / ACL 前置条件未满足 /
         seal read-back 失败 / pwsh 缺失）；
      2. ``api_name`` 非空 —— 该字段只在 `grant.py` / `service.py` /
         `spawn.py` / `token.py` 一族**真的调了 Win32 API 并拿到错误码**时
         构造，是"平台设施故障"的另一种**亲笔签名**；
      3. 链里出现 ``PwshUnavailableError``（`integration.py`）—— 受限 shell
         缺失，同属平台侧。

    ⚠⚠ **为什么需要 ①（2026-09-17 第三轮审计 NEW-1）**：只有 ②③ 时，
    30 处构造点里 19 处不传 `api_name`，其中至少 7 处**是真实平台故障**
    （见下列清单）⇒ 它们会被**静默降级**为 `outcome_unknown`。
    这是过度矫正：修复前它们全报 `runner_failed`（过度归因），
    只加 ②③ 后它们全不报（**欠归因**），而"平台加固失败"恰恰应当让 agent
    知道不是自己的问题。

    已按此标注的构造点（平台侧，无 api_name）：
      · `token.py` `no logon SID in token groups`
      · `grant.py` / `spawn.py` / `token.py` `pywin32 unavailable`
      · `service.py` `workspace 根无真实主体写 ACE`（**部署前提**，见下）
      · `service.py` `seal read-back failed`（两处：git 引导文件 / 配置载体
        —— 平台自己刚 `created`/写过该文件，有信息优势）
      · `integration.py` `pwsh not found`（经 `PwshUnavailableError`）

    ⚠⚠ **标注标准（2026-09-17 第四轮审计 HIGH 定案）** —— 只有构造点对该故障
    **确有信息优势**时才标，具体是这两种之一：

      (a) 它**自己真的在调** Win32 API（那次的失败就是平台设施故障）；
      (b) 它在**装配平台自己的目录/设施**（`workspace 根`、git 引导文件 ——
          这些是平台/部署流程给定的，不在 agent 执行期的自建面内）。

    ⚠ **"观察到某个状态不满足"不足以标注** —— 必须追问"这个状态会不会是
    agent 自己造出来的"。反例（第四轮审计已摘掉标注的两处）：
      · `service.py` `附加可写目录 {d} 无真实主体写 ACE` —— 判据是该目录 ACL
        的**实际状态**，而同段注释明写"**不自动创建**" ⇒ agent 自建目录后
        触发本分支是**已知可达路径**；
      · `service.py` `seal read-back failed: {git_dir}`（`.git` **根**）——
        同上，`.git` 可由 agent `git init` 自建。
      标了这两处 = **替 agent 卸责**（agent 收到「不是你的 bug」而放弃自查）。
      它们现在走默认 `False` ⇒ `outcome_unknown`（"这次失败没有归因"，不是
      "是 agent 的错"，也不是"平台故障"）。

    其余一律**不表态**（连 ``SandboxUnavailableError`` 本身都不够 —— 它会
    把真 bug 包进来）。不表态不等于说"不是平台问题"，只是不替上游做它自己
    会做的归因。

    ⚠ 取舍方向：**误判为平台侧 ⇒ agent 放弃自查真 bug**（代价更大），
    **漏判 ⇒ 上游按既有阶梯归因**（无害）。故「未标注」是安全的默认，
    而**标注必须是构造点主动做的动作**（判据来自状态/显式字段，不来自文案）。
    """
    from hiveweave.services.acl_sandbox.integration import PwshUnavailableError

    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, SandboxUnavailableError) and (
            cur.platform_side or cur.api_name
        ):
            return True
        if isinstance(cur, PwshUnavailableError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class SandboxUnavailableError(RuntimeError):
    """沙箱初始化/执行失败 —— fail-closed 的唯一出口。

    `service.spawn_confined` 只在「非 Windows / 配置关 / 项目级逃生门」三种情形
    返回 None（判定由 `policy.resolve_spawn_decision` 给出具名理由），
    其余一切异常（含意外 bug）都必须以本异常向上抛，绝不降级 native。
    """

    def __init__(
        self,
        message: str,
        *,
        api_name: str = "",
        win32_code: int | None = None,
        platform_side: bool = False,
    ):
        super().__init__(message)
        self.api_name = api_name
        self.win32_code = win32_code
        # ⚠ 2026-09-17 第三轮审计 NEW-1：**构造点亲笔声明**"这确属平台故障"。
        #
        # 为什么需要它（而不是让 `is_platform_side` 去猜）：本异常是**一切异常
        # 的容器** —— 真代码 bug 也被包进来。故"平台侧"这个判断**只有构造点
        # 知道得最清楚**（它要么真的调了 Win32 API、要么真的在检查平台前置
        # 条件）。默认 `False`（不表态）是安全侧：漏判 ⇒ 上游按既有阶梯归因；
        # 误判 ⇒ agent 收到「不是你的 bug」而放弃自查（代价更大）。
        #
        # 与 `api_name` 的分工：`api_name` 是"某一次具体 Win32 调用失败"的
        # 签名（带得出，就顺手带上）；本字段给"本身就是平台故障但不是某次
        # 具体调用"的那些（pywin32 不可用 / 令牌缺 SID / ACL 前置未满足 /
        # seal read-back 失败）。
        self.platform_side = platform_side

    def _detail(self) -> str:
        detail = self.win32_code if self.win32_code is not None else "n/a"
        return (
            f"沙箱不可用，已拒绝执行（fail-closed）：{self}"
            f"{f' [API={self.api_name}, Win32Err={detail}]' if self.api_name else ''}"
        )

    def to_tool_dict(self, *, platform_side: bool = True, **extra) -> dict:
        """对齐 DSH Win32Error：工具层把错误对象转换为对 agent 的提示。

        L6（2026-09-11）：沙箱自身不可用 = 命令从未执行 = runner 故障（平台侧），
        不是模型参数错，故显式携带 ``fact="runner_failed"``，并经
        ``finalize_fact_dict`` 展开派生键（否则下游读 ``runner_failed`` KeyError）。

        ⚠ 返回**裸 dict** —— 只给"函数契约本来就是 dict"的调用方（`bash.py` 的
        `_run_registered_dev_server` 一族）。返回 `ToolResult` 的工具请用
        :meth:`to_tool_result`：混用会让调用方（与测试）拿到 dict 却按
        `ToolResult` 用（实测 `AttributeError: 'dict' object has no attribute
        'success'`）。

        ``**extra``（F5，2026-09-17）：给调用方带上**执行面事实**的口子 ——
        典型是 ``executed=False``（命令从未启动）。本方法**不自己推断**它
        （有没有进程是 `entry.spawn_agent_command` 才知道的事），只负责透传。

        ``platform_side``（2026-09-17 第二轮审计必修）：本异常是**一切异常的
        容器** —— `service.py:1105-1107` 把意外异常（含真代码 bug）都包成
        ``SandboxUnavailableError(...) from e``。故「归因是平台侧」
        **不能由类型推出**，须由调用方按证据判（`dev_server_tools` 用
        `_is_platform_side` 查异常链里的 Win32/pwsh 亲笔签名）。
        传 ``False`` ⇒ 归因落 ``outcome_unknown``（blocked 允许的第三格）：
        「这次调用没有结果」是事实，但**不声称**是平台故障，让上游按既有阶梯
        归因，也避免给 agent 发「不是你的 bug」而让它放弃自查真缺陷。
        """
        from hiveweave.tools.result import finalize_fact_dict

        return finalize_fact_dict({
            "success": False,
            "blocked": True,
            "fact": "runner_failed" if platform_side else "outcome_unknown",
            "error": self._detail(),
            **extra,
        })

    def to_tool_result(self, *, platform_side: bool = True, **extra):
        """同一错误，**类型化**出口（`ToolResult`）—— 给返回 ToolResult 的工具用。

        ``**extra`` 同 :meth:`to_tool_dict`（F5：透传 ``executed`` 等执行面事实）；
        ``platform_side`` 同 :meth:`to_tool_dict`（2026-09-17 审计必修：本异常
        是"一切异常的容器"，归因不得由类型推出）。
        """
        from hiveweave.tools.result import ToolResult

        return ToolResult(
            success=False,
            output="",
            error=self._detail(),
            blocked=True,
            fact="runner_failed" if platform_side else "outcome_unknown",
            extra=extra,
        )
