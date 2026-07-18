# agentmandate — hosted MCP server image (the org deployment)
#
# Build:  docker build -t agentmandate .
# Run:    docker run -p 8000:8000 \
#           -v $(pwd)/policy.yaml:/app/policy.yaml \
#           -v $(pwd)/data:/app/data \
#           -e TRANSPORT=streamable-http -e HOST=0.0.0.0 \
#           -e KEYSTORE_PATH=/app/data/wallet.key \
#           -e AUDIT_DB_PATH=/app/data/audit.db \
#           agentmandate
#
# Clients connect with: {"transport": "streamable_http", "url": "http://<host>:8000/mcp"}

FROM python:3.12-slim

WORKDIR /app

# install the package first so layers cache well
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# default config: hosted transport, listen on all interfaces inside the container
ENV TRANSPORT=streamable-http HOST=0.0.0.0 PORT=8000

# never run the wallet-holding process as root: a container escape from a
# non-root user is a much taller order, and nothing here needs privileges.
# /app/data is where the mounted keystore + audit db live.
RUN useradd --create-home --uid 10001 agentmandate \
    && mkdir -p /app/data && chown -R agentmandate /app/data
USER agentmandate

EXPOSE 8000
CMD ["agentmandate"]
