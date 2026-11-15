#!/bin/sh
# Rauchtest fuer das Container-Image: gehaertet starten, /healthz, 401 ohne Token, initialize mit Token, sauberes Stoppen.
# Aufruf: deploy/smoketest.sh [IMAGE] [PORT]     Standard: datagvat-mcp, 18000
# Das Token entsteht hier und wird nie ausgegeben.
set -u
IMG=${1:-datagvat-mcp}
PORT=${2:-18000}
NAME=datagvat-smoketest-$$
TOKEN=$(openssl rand -hex 32)
FAIL=0

check() { # Beschreibung, erwartet, tatsaechlich
  if [ "$2" = "$3" ]; then echo "ok    $1"; else echo "FEHLER $1 (erwartet $2, war $3)"; FAIL=1; fi
}

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT

docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8000" \
  -e MCP_AUTH_TOKEN="$TOKEN" \
  --read-only --tmpfs /tmp:rw,size=1300m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges:true --init --memory 1g \
  "$IMG" >/dev/null || { echo "FEHLER Container startet nicht"; exit 1; }

start=$(date +%s)
code=000
for _ in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/healthz")
  [ "$code" = 200 ] && break
  sleep 1
done
echo "bereit nach $(( $(date +%s) - start )) s"
check "GET /healthz ohne Token" 200 "$code"
check "GET /mcp ohne Token" 401 "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/mcp")"

body=$(curl -s -X POST "http://127.0.0.1:$PORT/mcp" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"rauchtest","version":"0"}}}')
case "$body" in *'"name":"data-gv-at"'*) check "initialize mit Token" ok ok ;; *) check "initialize mit Token" ok "keine serverInfo" ;; esac

t=$(date +%s)
docker stop "$NAME" >/dev/null
check "Exit-Code nach docker stop" 0 "$(docker inspect -f '{{.State.ExitCode}}' "$NAME")"
echo "docker stop dauerte $(( $(date +%s) - t )) s"
exit $FAIL
