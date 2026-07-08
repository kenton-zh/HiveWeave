#!/bin/bash
# HiveWeave Sandbox entrypoint — keeps the container alive so agents
# can docker exec commands into it.

echo "HiveWeave Sandbox ready. Workspace: /workspace"
# Idle loop: sleep indefinitely, responding to docker exec calls
exec tail -f /dev/null
