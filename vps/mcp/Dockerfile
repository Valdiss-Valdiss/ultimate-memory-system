FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

ENV VAULT_PATH=/vault
ENV INBOX_PREFIX=Inbox/AI/
ENV AUDIT_LOG=/var/log/memory-mcp/audit.log
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8765

RUN mkdir -p /var/log/memory-mcp

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8765/health || exit 1

CMD ["python", "-m", "src.server"]
