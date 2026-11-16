# Stufe 1: Abhaengigkeiten exakt nach Lockfile in ein venv installieren (uv nur hier, nicht zur Laufzeit).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY datagvat_mcp.py datagvat_mcp.py.lock /app/
RUN uv venv /opt/venv \
 && uv export --script /app/datagvat_mcp.py --locked --no-hashes -o /tmp/requirements.txt \
 && uv pip install --python /opt/venv/bin/python --no-deps -r /tmp/requirements.txt \
 && /opt/venv/bin/python /app/datagvat_mcp.py --selftest

# Stufe 2: schlankes Laufzeit-Image ohne uv und ohne Schreibbedarf ausserhalb von /tmp.
FROM python:3.15-slim-bookworm
COPY --from=build /opt/venv /opt/venv
COPY datagvat_mcp.py /app/datagvat_mcp.py
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
WORKDIR /app
# Der Server startet im Container nur mit MCP_AUTH_TOKEN oder MCP_AUTH_TOKEN_FILE (siehe README, Abschnitt Betrieb).
USER nobody
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('MCP_PORT', '8000'), timeout=3)" || exit 1
# Direkt Python als PID 1: SIGTERM erreicht den Server ohne Zwischenprozess.
CMD ["python", "/app/datagvat_mcp.py"]
