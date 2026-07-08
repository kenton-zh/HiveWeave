"""Sandbox service — per-project Docker container lifecycle.

Each project gets an isolated Docker container with its own filesystem,
dev tools, and dependencies. Agent bash commands run inside the container
via ``docker exec``, preventing host system pollution.

Requires Docker Engine. Disabled by default (sandbox_enabled=False).
"""

from __future__ import annotations

import asyncio
import re
import shutil

import structlog

log = structlog.get_logger(__name__)

SANDBOX_IMAGE = "hiveweave/sandbox:latest"
SANDBOX_CONTAINER_PREFIX = "hw-sandbox-"
SANDBOX_WORKSPACE_MOUNT = "/workspace"

# Commands that must run on the host even when sandbox is enabled
# (e.g., git worktree operations that modify .hiveweave/worktrees/)
_HOST_ONLY_COMMANDS: frozenset[str] = frozenset({
    "git worktree",
    # Docker commands run on the HOST, not inside the sandbox container.
    # This lets agents manage the project's own Docker setup (docker build,
    # docker compose up, etc.) while still being sandboxed for everything else.
    "docker",
    "docker-compose",
})


def _container_name(project_id: str) -> str:
    """Derive Docker container name from project ID (safe for Docker)."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", project_id)[:40]
    return f"{SANDBOX_CONTAINER_PREFIX}{safe}"


async def _run_docker(*args: str, timeout_s: float = 120.0) -> tuple[int, str]:
    """Run a docker CLI command and return (exit_code, output)."""
    cmd = ["docker"] + list(args)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, f"docker command timed out after {timeout_s}s"
    output = stdout.decode("utf-8", errors="replace") if stdout else ""
    return proc.returncode or 0, output


class SandboxService:
    """Manage per-project Docker sandbox containers."""

    def __init__(self, enabled: bool = False) -> None:
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled and self._docker_available()

    def _docker_available(self) -> bool:
        """Check if Docker is installed and reachable."""
        if not shutil.which("docker"):
            return False
        return True

    async def ensure_image(self) -> bool:
        """Ensure the sandbox image is built or pulled.

        Returns True if image is ready, False if Docker unavailable.
        """
        if not self._docker_available():
            return False
        rc, _out = await _run_docker("image", "inspect", SANDBOX_IMAGE)
        if rc == 0:
            return True
        # Image not found — build from local Dockerfile
        log.info("sandbox.image_build", image=SANDBOX_IMAGE)
        rc, out = await _run_docker(
            "build", "-t", SANDBOX_IMAGE,
            "./docker/sandbox",
            timeout_s=600.0,
        )
        if rc != 0:
            log.error("sandbox.image_build_failed", error=out[-500:])
            return False
        return True

    async def create(self, project_id: str, workspace_path: str) -> str | None:
        """Create a sandbox container for a project.

        Returns container name, or None on failure.
        """
        if not self.enabled:
            return None

        name = _container_name(project_id)
        # Remove existing container with same name (e.g., recreated project)
        await _run_docker("rm", "-f", name)

        # Resolve workspace to absolute path for volume mount
        import os
        ws_abs = os.path.abspath(workspace_path)

        log.info("sandbox.container_create", name=name, workspace=ws_abs)
        rc, out = await _run_docker(
            "run", "-d",
            "--name", name,
            "-v", f"{ws_abs}:{SANDBOX_WORKSPACE_MOUNT}",
            "--network", "none",  # No network access by default
            "--restart", "no",
            SANDBOX_IMAGE,
            timeout_s=30.0,
        )
        if rc != 0:
            log.error("sandbox.container_create_failed", name=name, error=out[-500:])
            return None
        log.info("sandbox.container_created", name=name)
        return name

    async def destroy(self, project_id: str) -> None:
        """Destroy the sandbox container for a project."""
        name = _container_name(project_id)
        log.info("sandbox.container_destroy", name=name)
        await _run_docker("rm", "-f", name)

    async def exec(
        self,
        project_id: str,
        command: str,
        timeout_s: float = 120.0,
    ) -> dict:
        """Execute a command inside the project's sandbox container.

        Returns the same format as bash._run_native:
            {"output": str, "exit_code": int | None, "timed_out": bool, "error": str | None}
        """
        name = _container_name(project_id)

        # Check if container is running
        rc, _ = await _run_docker("inspect", "-f", "{{.State.Running}}", name)
        if rc != 0:
            return {
                "output": "",
                "exit_code": None,
                "timed_out": False,
                "error": f"Sandbox container '{name}' is not running. "
                         "The project may not have sandbox enabled, or the "
                         "container may have been removed.",
            }

        # Execute command via docker exec
        rc, output = await _run_docker(
            "exec",
            "-w", SANDBOX_WORKSPACE_MOUNT,
            name,
            "sh", "-c", command,
            timeout_s=timeout_s,
        )

        timed_out = rc == -1
        return {
            "output": output,
            "exit_code": rc if not timed_out else None,
            "timed_out": timed_out,
            "error": None,
        }

    def should_use_host(self, command: str) -> bool:
        """Check if a command should run on the host even with sandbox enabled."""
        cmd_lower = command.lower().strip()
        for prefix in _HOST_ONLY_COMMANDS:
            if cmd_lower.startswith(prefix):
                return True
        return False
