FROM python:3.12-slim

# System deps: git for repo ops, curl for the kiro-cli installer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install kiro-cli (headless coding agent).
RUN curl -fsSL https://cli.kiro.dev/install | bash
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

ENTRYPOINT ["python", "agent.py"]
