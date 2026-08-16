"""Harbor-comparable eval seal — hard-deny agent internet during a trial.

Official SWE-Marathon recognition still requires a Harbor run. This module
only makes a HiveWeave workspace *comparable*: instruction-only, no web
tools, no bash egress to the public internet.

Activation: ``<project_root>/.hiveweave/eval_sealed.json`` with
``{"sealed": true}``. The project root is inferred from worktrees
(``.hiveweave/worktrees/<id>``). A closer worktree-local seal file is
ignored so agents cannot unseal themselves. Unreadable/invalid JSON at
the project seal path fail-closed (treated as sealed).

Not a network namespace — argv heuristics plus tool-name denies.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

SEAL_FILENAME = "eval_sealed.json"

SEALED_NET_TOOLS = frozenset({
    "websearch",
    "webfetch",
    "browse",
})

_SEAL_DENY = (
    "Eval sealed: '{tool}' is disabled for a Harbor-comparable trial. "
    "Use instruction.md and local files only — no web search, fetch, or browser."
)

_BASH_DENY = (
    "Eval sealed: outbound network from bash is disabled "
    "(Harbor agent network is deny-all except the model gateway). "
    "Localhost health checks and local git commits are allowed. "
    "Preinstall deps before sealing — pip/npm/git clone are blocked."
)

_LOOPBACK_HOST = re.compile(
    r"(?i)^(localhost|127\.0\.0\.1|\[::1\]|::1|0\.0\.0\.0)$"
)

_GIT_EGRESS_VERBS = frozenset({"clone", "fetch", "pull", "push", "ls-remote"})
_GIT_FLAG_TAKES_ARG = frozenset({
    "-c", "-C", "--git-dir", "--work-tree", "--namespace",
})

_PIP_INSTALL_VERBS = frozenset({"install", "download"})
_NPM_INSTALL_VERBS = frozenset({"install", "i", "ci", "add"})
_CURL_BINS = frozenset({"curl", "curl.exe", "wget", "wget.exe", "httpie", "http"})
_CURL_TAKES_ARG = frozenset({
    "-o", "--output", "-O", "--remote-name",
    "-H", "--header", "-d", "--data", "--data-raw", "--data-binary",
    "-A", "--user-agent", "-e", "--referer",
    "-m", "--max-time", "-w", "--write-out",
    "-b", "--cookie", "-c", "--cookie-jar",
    "-u", "--user", "-x", "--proxy", "-K", "--config",
    "-T", "--upload-file", "-F", "--form",
    "--connect-timeout", "--retry", "--resolve",
})
_PS_FETCH = frozenset({
    "invoke-webrequest", "invoke-restmethod", "iwr", "irm",
})

_SKIP_LEAK_PARTS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache",
})


def _workspace_hint(agent: dict[str, Any] | None) -> str:
    if not agent:
        return ""
    for key in ("workspace_path", "workspacePath", "workspace"):
        raw = agent.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def project_root_from_workspace(start: str | Path | None) -> Path | None:
    """Project root, stripping ``.hiveweave/worktrees/<id>`` if present."""
    if not start:
        return None
    try:
        p = Path(start).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    parts = p.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".hiveweave" and parts[i + 1] == "worktrees":
            if i == 0:
                return p
            return Path(*parts[:i])
    return p


def find_seal_file(start: str | Path | None) -> Path | None:
    """Return the project-root seal path if that file exists.

    Worktree-local ``.hiveweave/eval_sealed.json`` is ignored.
    """
    root = project_root_from_workspace(start)
    if root is None:
        return None
    seal = root / ".hiveweave" / SEAL_FILENAME
    try:
        if seal.is_file():
            return seal
    except OSError:
        return None
    return None


def is_eval_sealed(start: str | Path | None) -> bool:
    """True when the project-root seal file says sealed, or is unreadable."""
    path = find_seal_file(start)
    if path is None:
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        log.warning("eval_seal.read_failed_fail_closed", path=str(path))
        return True
    if not isinstance(data, dict):
        return True
    return bool(data.get("sealed"))


def is_agent_eval_sealed(agent: dict[str, Any] | None) -> bool:
    return is_eval_sealed(_workspace_hint(agent))


def sealed_tool_deny(agent: dict[str, Any], tool_name: str) -> str | None:
    if tool_name not in SEALED_NET_TOOLS:
        return None
    if not is_agent_eval_sealed(agent):
        return None
    return _SEAL_DENY.format(tool=tool_name)


def _host_is_loopback(host: str) -> bool:
    return bool(_LOOPBACK_HOST.match(host.strip("[]")))


def _strip_hash_comments(command: str) -> str:
    lines: list[str] = []
    for line in command.splitlines():
        in_s = in_d = False
        buf: list[str] = []
        for c in line:
            if c == "'" and not in_d:
                in_s = not in_s
            elif c == '"' and not in_s:
                in_d = not in_d
            elif c == "#" and not in_s and not in_d:
                break
            buf.append(c)
        lines.append("".join(buf))
    return "\n".join(lines)


def _tokenize(command: str) -> list[str]:
    cleaned = _strip_hash_comments(command)
    try:
        return shlex.split(cleaned, posix=True)
    except ValueError:
        return cleaned.split()


def _is_local_path(arg: str) -> bool:
    if arg in (".", "..") or arg.startswith(".["):
        return True
    if arg.startswith(("./", ".\\", "../", "..\\", "/", "file:")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", arg):
        return True
    return False


def _lower_name(token: str) -> str:
    return Path(token.replace("\\", "/")).name.lower()


def _skip_flag(tokens: list[str], i: int, takes_arg: frozenset[str]) -> int:
    t = tokens[i]
    name = t.split("=", 1)[0]
    if name in takes_arg and "=" not in t and i + 1 < len(tokens):
        return i + 2
    return i + 1


def _git_verb(tokens: list[str]) -> str | None:
    if not tokens or _lower_name(tokens[0]) not in {"git", "git.exe"}:
        return None
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            i = _skip_flag(tokens, i, _GIT_FLAG_TAKES_ARG)
            continue
        return t.lower()
    return None


def _pip_targets_are_local(tokens: list[str], start: int) -> bool:
    """*start* is the index of install/download. Allow only local/offline."""
    rest = tokens[start + 1:]
    if any(t == "--no-index" or t.startswith("--no-index=") for t in rest):
        return True
    saw_pkg = False
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in ("-e", "--editable"):
            if i + 1 >= len(rest) or not _is_local_path(rest[i + 1]):
                return False
            saw_pkg = True
            i += 2
            continue
        if t.startswith("-"):
            if t.startswith(("-e", "--editable=")):
                path = t.split("=", 1)[-1]
                if not _is_local_path(path):
                    return False
                saw_pkg = True
            i += 1
            continue
        if not _is_local_path(t):
            return False
        saw_pkg = True
        i += 1
    return saw_pkg


def _package_egress(tokens: list[str]) -> bool:
    """True when tokens look like a public package-index fetch."""
    n = len(tokens)
    i = 0
    while i < n:
        name = _lower_name(tokens[i])
        nxt = _lower_name(tokens[i + 1]) if i + 1 < n else ""
        nxt2 = _lower_name(tokens[i + 2]) if i + 2 < n else ""
        nxt3 = _lower_name(tokens[i + 3]) if i + 3 < n else ""

        if name in {"pip", "pip3", "pip.exe"} and nxt in _PIP_INSTALL_VERBS:
            return not _pip_targets_are_local(tokens, i + 1)
        if name in {"python", "python3", "python.exe"} and nxt == "-m" and nxt2 in {
            "pip", "pip3",
        } and nxt3 in _PIP_INSTALL_VERBS:
            return not _pip_targets_are_local(tokens, i + 3)
        if name == "uv":
            if nxt in _PIP_INSTALL_VERBS:
                return not _pip_targets_are_local(tokens, i + 1)
            if nxt == "pip" and nxt2 in _PIP_INSTALL_VERBS:
                return not _pip_targets_are_local(tokens, i + 2)
            if nxt in {"add", "sync", "remove"}:
                rest = tokens[i + 2:]
                if any(t in {"--offline", "--no-index"} for t in rest):
                    i += 1
                    continue
                return True
        if name in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd", "bun", "bun.exe"}:
            if nxt in _NPM_INSTALL_VERBS:
                rest = tokens[i + 2:]
                if any(t == "--offline" or t.startswith("--offline=") for t in rest):
                    i += 1
                    continue
                return True
            if nxt == "dlx":
                return True
        if name in {"npx", "npx.cmd", "bunx"}:
            rest = tokens[i + 1:]
            if any(t == "--offline" or t.startswith("--offline=") for t in rest):
                i += 1
                continue
            return True
        if name in {"apt-get", "apt", "apk", "yum", "dnf", "brew"} and nxt in {
            "install", "add", "upgrade", "update",
        }:
            return True
        i += 1
    return False


def _host_from_url(token: str) -> str | None:
    m = re.match(r"(?i)^https?://([^/:\s]+)", token)
    if m:
        return m.group(1)
    if re.match(r"(?i)^[a-z0-9.-]+\.[a-z]{2,}(?::\d+)?(/|$)", token):
        return token.split("/", 1)[0].split(":", 1)[0]
    if re.match(r"(?i)^(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(/|$)", token):
        return token.split("/", 1)[0].split(":", 1)[0]
    return None


def _curl_hosts(tokens: list[str]) -> list[str]:
    if not tokens or _lower_name(tokens[0]) not in _CURL_BINS:
        return []
    hosts: list[str] = []
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            i = _skip_flag(tokens, i, _CURL_TAKES_ARG)
            continue
        host = _host_from_url(t)
        if host:
            hosts.append(host)
        i += 1
    return hosts


def bash_egress_reason(command: str) -> str | None:
    """Return a deny reason if *command* looks like public-network egress."""
    if not command or not command.strip():
        return None
    if re.search(r"(?i)eval_sealed\.json", command):
        return "Eval sealed: cannot read or modify eval_sealed.json from bash."
    tokens = _tokenize(command)
    if not tokens:
        return None
    names = {_lower_name(t) for t in tokens}
    if names & _PS_FETCH:
        return _BASH_DENY
    verb = _git_verb(tokens)
    if verb in _GIT_EGRESS_VERBS:
        return _BASH_DENY
    if _package_egress(tokens):
        return _BASH_DENY
    if _lower_name(tokens[0]) in _CURL_BINS:
        hosts = _curl_hosts(tokens)
        if any(not _host_is_loopback(h) for h in hosts):
            return _BASH_DENY
        # curl with no host (e.g. --help) is fine
    elif re.search(
        r"(?i)https?://(?!localhost\b|127\.0\.0\.1\b|\[::1\]|0\.0\.0\.0\b)",
        command,
    ):
        return _BASH_DENY
    return None


def sealed_bash_deny_for_workspace(
    workspace_path: str | None, command: str
) -> str | None:
    if not is_eval_sealed(workspace_path):
        return None
    return bash_egress_reason(command)


def sealed_bash_deny(agent: dict[str, Any], command: str) -> str | None:
    return sealed_bash_deny_for_workspace(_workspace_hint(agent), command)


def _skip_leak_path(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    parts = rel.parts
    if any(p in _SKIP_LEAK_PARTS for p in parts):
        return True
    # Operator-only evidence dir is not agent-readable; don't block freeze.
    if len(parts) >= 2 and parts[0] == ".hiveweave" and parts[1] == "evidence":
        return True
    return False


def scan_task_root_leaks(task_root: str | Path) -> list[str]:
    """Instruction-only preflight: official tests / GAP must not sit in tree."""
    root = Path(task_root)
    leaks: list[str] = []
    if not root.is_dir():
        return [f"TASK_ROOT does not exist: {root}"]

    tests_dir = root / "tests"
    if tests_dir.is_dir():
        officialish = [
            name
            for name in ("test.sh", "cua_blend.sh", "cua_stage.sh", "cua_config.json")
            if (tests_dir / name).exists()
        ]
        py_tests = [
            p for p in tests_dir.rglob("test_*.py") if not _skip_leak_path(p, root)
        ]
        py_tests += [
            p for p in tests_dir.rglob("*_test.py") if not _skip_leak_path(p, root)
        ]
        if officialish or py_tests:
            sample = officialish or [p.name for p in py_tests[:5]]
            leaks.append(
                "tests/ looks like the hidden verifier — remove it from TASK_ROOT "
                f"(found {sample})"
            )

    for marker in ("test.sh", "cua_blend.sh", "solve.sh"):
        if (root / marker).is_file():
            leaks.append(f"official harness file in TASK_ROOT: {marker}")

    solution = root / "solution"
    if solution.is_dir() and any(solution.iterdir()):
        leaks.append("solution/ present — Harbor oracle, not agent input")

    gap_hits: list[str] = []
    for p in root.rglob("GAP_REPORT*"):
        if p.is_file() and not _skip_leak_path(p, root):
            gap_hits.append(str(p.relative_to(root)))
            if len(gap_hits) >= 5:
                break
    shared = root / ".hiveweave" / "shared"
    if shared.is_dir():
        gap_hits.extend(
            str(p.relative_to(root))
            for p in shared.glob("*gap*")
            if p.is_file() and str(p.relative_to(root)) not in gap_hits
        )
    if gap_hits:
        leaks.append(
            "GAP_REPORT in TASK_ROOT — agents can read it. "
            f"Move off the workspace (found {gap_hits[:5]})."
        )
    return leaks
