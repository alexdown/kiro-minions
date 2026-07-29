# kiro-minions runtime — ARM64 container for AgentCore Runtime.
#
# Pre-installs: kiro-cli, git, gh (GitHub CLI), AWS CLI v2, common test runners.
# The workflow script (agent.py) drives a deterministic fix pipeline:
#   clone → branch → kiro-cli fix → test → commit → push → PR
#
# Build: docker build --platform linux/arm64 -t kiro-minions-runtime .

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# --- System dependencies ---
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        curl unzip git ca-certificates jq && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# --- Kiro CLI (ARM64) ---
RUN curl -fsSL https://cli.kiro.dev/install | KIRO_CLI_SKIP_SETUP=1 bash && \
    cp /root/.local/bin/kiro-cli* /usr/local/bin/

# --- GitHub CLI (ARM64) ---
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=arm64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends gh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# --- AWS CLI v2 (ARM64) ---
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/aws /tmp/awscliv2.zip

# --- Node.js (for npm test in target repos) ---
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y -qq --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# --- Python dependencies for the workflow script ---
COPY requirements.txt requirements.txt
RUN uv pip install -r requirements.txt
RUN uv pip install "aws-opentelemetry-distro>=0.10.1"

# --- Environment ---
ENV AWS_REGION=us-east-1 \
    AWS_DEFAULT_REGION=us-east-1 \
    DOCKER_CONTAINER=1

EXPOSE 8080
EXPOSE 8000

# --- Application code ---
COPY . .

CMD ["opentelemetry-instrument", "python", "-m", "agent"]
