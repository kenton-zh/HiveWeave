# HiveWeave Sandbox — per-project agent execution environment
# Provides a standardized, isolated dev environment for agents to:
# - Run bash commands safely (no host pollution)
# - Install dependencies (npm, pip, etc.)
# - Use common dev tools (git, grep, curl, etc.)
#
# Usage:
#   docker build -t hiveweave/sandbox ./docker/sandbox
#   docker run -d --name hw-sandbox-{project} -v {project_path}:/workspace hiveweave/sandbox

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

# ── System packages ─────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Essential build tools
    build-essential \
    ca-certificates \
    curl \
    git \
    jq \
    unzip \
    xz-utils \
    zip \
    # Shell & scripting
    bash \
    grep \
    sed \
    awk \
    # Python 3
    python3 \
    python3-pip \
    python3-venv \
    # Node.js (via nodesource)
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 22 ──────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── pnpm ────────────────────────────────────────────────────
RUN npm install -g pnpm@latest

# ── ripgrep (fast grep) ─────────────────────────────────────
RUN curl -LO https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep_14.1.1-1_amd64.deb \
    && dpkg -i ripgrep_14.1.1-1_amd64.deb \
    && rm ripgrep_14.1.1-1_amd64.deb

# ── python3 alias (agents use python3, ubuntu ships python3 binary) ──
RUN ln -sf /usr/bin/python3 /usr/local/bin/python3 2>/dev/null; \
    ln -sf /usr/bin/python3 /usr/local/bin/python 2>/dev/null; \
    true

# ── Environment ─────────────────────────────────────────────
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONIOENCODING=utf-8
ENV PYTHONUTF8=1
ENV NODE_ENV=development

# ── Workspace ───────────────────────────────────────────────
WORKDIR /workspace

# ── Entrypoint (keeps container alive) ──────────────────────
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
