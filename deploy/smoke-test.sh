#!/usr/bin/env bash
# Rauchtest fuer das Image mit derselben Haertung wie deploy/exoscale/compose.yml.
# Aufruf: bash deploy/smoke-test.sh [image]   (READ_ONLY=1 prueft zusaetzlich ein schreibgeschuetztes Dateisystem)
set -euo pipefail
IMAGE="${1:-datagvat-mcp}"
PORT="${PORT:-18000}"
TOKEN="$(openssl rand -hex 32)"
NAME="datagvat-smoke-$$"
EXTRA=()
if [ "${READ_ONLY:-0}" = 1 ]; then EXTRA+=(--read-only); fi
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

t0=$(date +%s)
docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:8000" -v /tmp \
  --cap-drop ALL --security-opt no-new-privileges --memory 1500m --pids-limit 256 --init \
  "${EXTRA[@]}" -e MCP_AUTH_TOKEN="$TOKEN" -e MCP_STATELESS=1 "$IMAGE" >/dev/null
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then break; fi
  if [ "$i" = 60 ]; then echo "FEHLER: /healthz antwortet nicht"; docker logs "$NAME" 2>&1 | tail -30; exit 1; fi
  sleep 1
done
echo "bereit nach $(( $(date +%s) - t0 )) s"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/mcp")
[ "$code" = 401 ] || { echo "FEHLER: ohne Token HTTP $code statt 401"; exit 1; }
body=$(curl -sS -X POST "http://127.0.0.1:${PORT}/mcp" \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"rauchtest","version":"0"}}}')
echo "$body" | grep -q 'data-gv-at' || { echo "FEHLER: initialize ohne serverInfo"; echo "$body" | head -c 400; exit 1; }

t1=$(date +%s)
docker stop -t 10 "$NAME" >/dev/null
rc=$(docker inspect -f '{{.State.ExitCode}}' "$NAME")
echo "Stop nach $(( $(date +%s) - t1 )) s, Exit-Code $rc"
[ "$rc" = 0 ] || { echo "FEHLER: kein geordnetes Ende"; exit 1; }
echo "Rauchtest ok"
