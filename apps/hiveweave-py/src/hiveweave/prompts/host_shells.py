"""宿主感知的 shell 工具名（T3.2 三处同步的角色脚本侧）。

Windows pwsh 宿主上 bash / bash_main 不暴露（host_env.tools_filter），
角色脚本里的工具引导必须与实际暴露一致 —— 否则模型对着 pwsh 工具写
bash（计划 T3.2 执行前必须先扫 prompts 的原因）。Linux / 未探测环境
回退 bash 系名字（与历史行为一致）。
"""
from __future__ import annotations


def _bash_hidden() -> bool:
    try:
        from hiveweave.services.host_env import host_hidden_tools

        return "bash" in host_hidden_tools()
    except Exception:
        return False


def worktree_shell_tool() -> str:
    """自己工作区（worktree）的 shell 工具名。"""
    return "pwsh" if _bash_hidden() else "bash"


def main_shell_tool() -> str:
    """MAIN / 项目根（里程碑 VERIFY、QA 取证）的 shell 工具名。"""
    return "pwsh_main" if _bash_hidden() else "bash_main"


def shell_tool_pair() -> str:
    """「worktree shell / MAIN shell」成对文案（如 ``bash / bash_main``）。"""
    return f"{worktree_shell_tool()} / {main_shell_tool()}"


def apply_host_shell_names(text: str) -> str:
    """把提示词文本里的 bash 系工具名整体替换为本宿主实际暴露的名字。

    只在「bash 被宿主隐藏」（Windows pwsh 宿主）时生效；替换分两步：
    先长词 ``bash_main`` → ``pwsh_main``，再 ``bash`` → ``pwsh``（大小写
    敏感 —— 后台任务唤醒标记 ``[BASH DONE]`` / ``[BASH FAILED]`` 是平台
    固定事件名，不受工具名替换影响，保持原样）。语义核对过全部 prompt
    出现点：都是「shell 工具」概念本身，pwsh 宿主上整词成立。
    """
    if not _bash_hidden():
        return text
    return text.replace("bash_main", "pwsh_main").replace("bash", "pwsh")
