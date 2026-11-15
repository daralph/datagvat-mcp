#!/usr/bin/env bash
# datagvat-mcp in eine bestehende LibreChat-Installation (Docker Compose) einbinden.
# Aufruf im LibreChat-Ordner:  bash <Pfad>/deploy/librechat/install.sh [--dry-run] [--no-start] [--uninstall]
# Eigene Aenderungen stehen zwischen Markierungen und lassen sich mit --uninstall rueckstandsfrei entfernen.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE=install
START=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE=dry-run ;;
    --uninstall) MODE=uninstall ;;
    --no-start) START=0 ;;
    *) echo "Unbekannte Option: $arg" >&2; exit 64 ;;
  esac
done
BEGIN="# >>> datagvat-mcp (von install.sh, nicht von Hand aendern)"
END="# <<< datagvat-mcp"
COMPOSE_NAME="docker-compose.datagvat.yml"
T0=$(date +%s)
step() { printf '[%3ds] %s\n' "$(( $(date +%s) - T0 ))" "$*"; }
die() { echo "FEHLER: $*" >&2; exit 1; }
dc() { docker compose "$@"; }

[ -f docker-compose.yml ] || die "Hier liegt keine docker-compose.yml. Bitte im LibreChat-Ordner ausfuehren."
[ -f .env ] || die "Hier liegt keine .env (LibreChat-Konfiguration)."

strip_block() { # entfernt den eigenen Block, der Rest der Datei bleibt Byte fuer Byte gleich
  local f="$1" tmp
  [ -f "$f" ] || return 0
  tmp="$(mktemp)"
  awk -v b="$BEGIN" -v e="$END" 'index($0, b) { skip = 1 } !skip { print } index($0, e) { skip = 0 }' "$f" > "$tmp"
  cat "$tmp" > "$f"
  rm -f "$tmp"
}

if [ "$MODE" = uninstall ]; then
  if [ "$START" = 1 ] && [ -f "$COMPOSE_NAME" ]; then
    step "Container mcp-datagvat entfernen"
    dc rm -sf mcp-datagvat >/dev/null 2>&1 || true
  fi
  strip_block .env
  strip_block librechat.yaml
  if [ -f librechat.yaml ] && [ ! -s librechat.yaml ]; then rm -f librechat.yaml; fi
  rm -f "$COMPOSE_NAME"
  step "Entfernt. LibreChat mit 'docker compose up -d' neu laden."
  exit 0
fi

# ---------- Pruefen, was zu tun ist (noch keine Aenderung) ----------
ENV_HAS_BLOCK=0; grep -qF "$BEGIN" .env && ENV_HAS_BLOCK=1
if [ "$ENV_HAS_BLOCK" = 0 ] && grep -qE '^[[:space:]]*COMPOSE_FILE=' .env; then
  grep -E '^[[:space:]]*COMPOSE_FILE=' .env | grep -qF "$COMPOSE_NAME" \
    || die "In .env ist COMPOSE_FILE schon gesetzt. Bitte dort ':$COMPOSE_NAME' anhaengen und erneut starten."
fi
YAML_ACTION=none
if [ ! -f librechat.yaml ]; then
  YAML_ACTION=create
elif grep -qF "$BEGIN" librechat.yaml; then
  YAML_ACTION=none
elif grep -qE '^mcpServers:[[:space:]]*(#.*)?$' librechat.yaml; then
  if awk '/^mcpServers:/ { f = 1; next } f && /^[^[:space:]#]/ { f = 0 } f && /^[[:space:]]+datagvat:/ { found = 1 } END { exit !found }' librechat.yaml; then
    YAML_ACTION=none # schon von Hand eingetragen
  else
    YAML_ACTION=insert
  fi
elif grep -qE '^mcpServers:' librechat.yaml; then
  die "librechat.yaml: 'mcpServers:' steht in Kurzschreibweise. Bitte als Block schreiben (eigene Zeile 'mcpServers:') und erneut starten."
else
  YAML_ACTION=append
fi
step "Plan: .env $([ "$ENV_HAS_BLOCK" = 1 ] && echo unveraendert || echo ergaenzen), librechat.yaml: $YAML_ACTION, $COMPOSE_NAME schreiben"
if [ "$MODE" = dry-run ]; then step "Probelauf: nichts geaendert."; exit 0; fi

# ---------- Sicherung, dann aendern ----------
BACKUP=".datagvat-backup-$(date +%Y%m%d-%H%M%S)"
if [ "$ENV_HAS_BLOCK" = 0 ] || [ "$YAML_ACTION" != none ]; then
  mkdir -p "$BACKUP"
  cp -p .env "$BACKUP/"
  if [ -f librechat.yaml ]; then cp -p librechat.yaml "$BACKUP/"; fi
  chmod -R go-rwx "$BACKUP"
  step "Sicherung in $BACKUP"
fi

if [ "$ENV_HAS_BLOCK" = 0 ]; then
  files="docker-compose.yml"
  if [ -f docker-compose.override.yml ]; then files="$files:docker-compose.override.yml"; fi
  {
    if [ -s .env ] && [ -n "$(tail -c 1 .env)" ]; then echo; fi
    echo "$BEGIN"
    grep -qE '^[[:space:]]*DATAGVAT_MCP_TOKEN=.' .env || echo "DATAGVAT_MCP_TOKEN=$(openssl rand -hex 32)"
    grep -qE '^[[:space:]]*COMPOSE_FILE=' .env || echo "COMPOSE_FILE=$files:$COMPOSE_NAME"
    echo "$END"
  } >> .env
fi

entry() { # $1 = Einrueckung der Server-Namen unter mcpServers
  sed "s/^/$1/" <<ENTRY
$BEGIN
datagvat:
  type: streamable-http
  url: "http://mcp-datagvat:8000/mcp"
  timeout: 150000
  initTimeout: 15000
  chatMenu: true
  requiresOAuth: false
  headers:
    Authorization: "Bearer \${DATAGVAT_MCP_TOKEN}"
  serverInstructions: >
    Workflow fuer oesterreichische Open Data (data.gv.at):
    1. Suche Datensaetze mit search_datasets.
    2. Hole Distributionen mit get_dataset_details und nimm recommended_url.
    3. Pruefe das Schema via inspect_distribution_schema.
    4. Werte Daten per SQL ueber query_distribution_sql auf der Tabelle 'dataset' aus.
    5. Codes mit den Codelisten aus 'lookup_tables' per extra_tables und JOIN entschluesseln.
$END
ENTRY
}
case "$YAML_ACTION" in
  create)
    { echo "$BEGIN"; echo "version: 1.2.1"; echo "cache: true"; echo "mcpServers:"; entry "  " | sed '1d;$d'; echo "$END"; } > librechat.yaml ;;
  append)
    { if [ -n "$(tail -c 1 librechat.yaml)" ]; then echo; fi; echo "$BEGIN"; echo "mcpServers:"; entry "  " | sed '1d;$d'; echo "$END"; } >> librechat.yaml ;;
  insert)
    IND="$(awk '/^mcpServers:/ { f = 1; next } f && /^[^[:space:]#]/ { exit } f && /^[[:space:]]+[^#[:space:]]/ { match($0, /^[[:space:]]+/); print substr($0, 1, RLENGTH); exit }' librechat.yaml)"
    [ -n "$IND" ] || IND="  "
    blk="$(mktemp)"; tmp="$(mktemp)"
    entry "$IND" > "$blk"
    awk -v blockfile="$blk" '{ print } /^mcpServers:[[:space:]]*(#.*)?$/ && !done { while ((getline line < blockfile) > 0) print line; done = 1 }' librechat.yaml > "$tmp"
    cat "$tmp" > librechat.yaml
    rm -f "$blk" "$tmp" ;;
esac
cp "$HERE/compose.datagvat.yml" "$COMPOSE_NAME"
step "Dateien eingerichtet"
if [ "$START" = 0 ]; then step "Ohne Start beendet (--no-start)."; exit 0; fi

# ---------- Starten und pruefen ----------
step "Image holen"
dc pull -q mcp-datagvat
step "MCP-Server starten"
dc up -d mcp-datagvat
for i in $(seq 1 60); do
  if dc exec -T mcp-datagvat python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" >/dev/null 2>&1; then break; fi
  [ "$i" != 60 ] || die "mcp-datagvat antwortet nicht. Siehe: docker compose logs mcp-datagvat"
  sleep 1
done
step "MCP-Server bereit, initialize pruefen"
dc exec -T mcp-datagvat python3 - <<'PY' || die "initialize fehlgeschlagen. Siehe: docker compose logs mcp-datagvat"
import json, os, urllib.request
body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "install", "version": "0"}}}
req = urllib.request.Request("http://127.0.0.1:8000/mcp", data=json.dumps(body).encode(), headers={
    "Authorization": "Bearer " + os.environ["MCP_AUTH_TOKEN"].split(",")[0].strip(),
    "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
assert "data-gv-at" in urllib.request.urlopen(req, timeout=15).read().decode()
PY
step "LibreChat neu laden"
dc up -d --force-recreate api
for i in $(seq 1 90); do
  if dc exec -T api node -e "fetch('http://mcp-datagvat:8000/healthz').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))" >/dev/null 2>&1; then break; fi
  [ "$i" != 90 ] || die "LibreChat (api) erreicht mcp-datagvat nicht. Siehe: docker compose logs api"
  sleep 1
done
step "Fertig. In LibreChat im Chat-Menue den MCP-Server 'datagvat' waehlen."
echo "Hinweis: COMPOSE_FILE steht jetzt in .env. Eine spaeter angelegte docker-compose.override.yml dort ergaenzen."
