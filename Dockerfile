FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
ENV UV_CACHE_DIR=/opt/uv-cache \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY datagvat_mcp.py datagvat_mcp.py.lock /app/
# Abhaengigkeiten exakt nach Lockfile in den Cache ziehen; schlaegt der Selftest fehl, bricht der Build ab.
RUN uv run --locked --script /app/datagvat_mcp.py --selftest \
 && chmod -R a+rX /app \
 && chmod -R a+rwX /opt/uv-cache
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
# Der Server startet im Container nur mit MCP_AUTH_TOKEN (siehe README, Abschnitt Betrieb).
USER nobody
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python3 -c "import os, socket; socket.create_connection(('127.0.0.1', int(os.getenv('MCP_PORT', '8000'))), 3)" || exit 1
CMD ["uv", "run", "--locked", "--offline", "--script", "/app/datagvat_mcp.py"]
