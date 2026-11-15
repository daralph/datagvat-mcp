#!/usr/bin/env bash
# Rauchtest fuer das Image mit derselben Haertung wie die Compose-Dateien: schreibgeschuetztes Dateisystem,
# keine Capabilities, Limits, init. Das Token entsteht hier und wird nie ausgegeben.
# Aufruf: bash deploy/smoke-test.sh [image]
#   TMP_ART=volume (Standard, wie deploy/exoscale) oder TMP_ART=tmpfs (wie deploy/librechat) fuer /tmp
set -euo pipefail
IMAGE="${1:-datagvat-mcp}"
PORT="${PORT:-18000}"
TOKEN="$(openssl rand -hex 32)"
NAME="datagvat-smoke-$$"
case "${TMP_ART:-volume}" in
  volume) TMP_OPT=(-v /tmp) ;;
  tmpfs)  TMP_OPT=(--tmpfs /tmp:rw,size=1300m,mode=1777) ;;
  *) echo "FEHLER: TMP_ART muss volume oder tmpfs sein"; exit 2 ;;
esac
cleanup() { docker rm -f -v "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

t0=$(date +%s)
docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:8000" --read-only "${TMP_OPT[@]}" \
  --cap-drop ALL --security-opt no-new-privileges --memory 2g --pids-limit 256 --init \
  -e MCP_AUTH_TOKEN="$TOKEN" -e MCP_STATELESS=1 "$IMAGE" >/dev/null
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
