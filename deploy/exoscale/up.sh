#!/usr/bin/env bash
# datagvat-mcp auf einer frischen Exoscale-Instanz (Wien) in einem Befehl starten.
# Voraussetzungen: exo-CLI eingerichtet ('exo config'), registrierter SSH-Schluessel, jq.
# Aufruf: ./deploy/exoscale/up.sh [--domain mcp.example.org] [--zone at-vie-1] [--name datagvat-mcp] [--ssh-key NAME] [--instance-type standard.small]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAME="${EXO_INSTANCE_NAME:-datagvat-mcp}"
SG_NAME="${EXO_SG_NAME:-${NAME}-sg}"
ZONE="${EXO_ZONE:-at-vie-1}"
INSTANCE_TYPE="${EXO_INSTANCE_TYPE:-standard.small}"
TEMPLATE="${EXO_TEMPLATE:-Linux Ubuntu 24.04 LTS 64-bit}"
DISK_SIZE="${EXO_DISK_SIZE:-20}"
SSH_KEY="${EXO_SSH_KEY:-}"
DOMAIN=""

while [ $# -gt 0 ]; do
  case "$1" in
    --domain)        DOMAIN="${2:?--domain braucht einen Hostnamen}"; shift 2 ;;
    --zone)          ZONE="${2:?--zone braucht eine Zone}"; shift 2 ;;
    --name)          NAME="${2:?--name braucht einen Namen}"; SG_NAME="${NAME}-sg"; shift 2 ;;
    --ssh-key)       SSH_KEY="${2:?--ssh-key braucht einen Schluesselnamen}"; shift 2 ;;
    --instance-type) INSTANCE_TYPE="${2:?--instance-type braucht einen Typ}"; shift 2 ;;
    -h|--help)
      echo "Aufruf: $0 [--domain HOST] [--zone at-vie-1] [--name datagvat-mcp] [--ssh-key NAME] [--instance-type standard.small]"
      exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 64 ;;
  esac
done

command -v exo >/dev/null 2>&1 || { echo "FEHLER: exo-CLI nicht gefunden (https://community.exoscale.com/documentation/tools/exoscale-command-line-interface/)." >&2; exit 1; }
command -v jq  >/dev/null 2>&1 || { echo "FEHLER: jq nicht gefunden." >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "FEHLER: curl nicht gefunden." >&2; exit 1; }

if [ -z "$SSH_KEY" ]; then
  SSH_KEY="$(exo compute ssh-key list -O json | jq -r '.[0].name // empty')"
  [ -n "$SSH_KEY" ] || { echo "FEHLER: Kein SSH-Schluessel bei Exoscale hinterlegt (--ssh-key oder 'exo compute ssh-key register')." >&2; exit 1; }
fi

T0=$(date +%s)
step() { printf '[%3ds] %s\n' "$(( $(date +%s) - T0 ))" "$*"; }

# 1. Security Group anlegen (idempotent)
if ! exo compute security-group show "$SG_NAME" >/dev/null 2>&1; then
  step "Security Group '$SG_NAME' anlegen"
  exo compute security-group create "$SG_NAME" >/dev/null
  MY_IP="$(curl -fsS --max-time 5 https://checkip.amazonaws.com | tr -d '[:space:]')"
  exo compute security-group rule add "$SG_NAME" --protocol tcp --port 22  --network "${MY_IP}/32" --description "SSH nur von eigener IP" >/dev/null
  exo compute security-group rule add "$SG_NAME" --protocol tcp --port 80  --network "0.0.0.0/0"   --description "HTTP fuer ACME/Redirect" >/dev/null
  exo compute security-group rule add "$SG_NAME" --protocol tcp --port 443 --network "0.0.0.0/0"   --description "HTTPS fuer Caddy/MCP" >/dev/null
else
  step "Security Group '$SG_NAME' bereits vorhanden"
fi

# 2. cloud-init vorbereiten (optional eigene Domain mitgeben, niemals Tokens)
CLOUD_INIT="$HERE/cloud-init.yaml"
TMP_CI=""
cleanup() { [ -z "$TMP_CI" ] || rm -f "$TMP_CI"; }
trap cleanup EXIT
if [ -n "$DOMAIN" ]; then
  TMP_CI="$(mktemp)"
  cat "$CLOUD_INIT" > "$TMP_CI"
  printf '\nwrite_files:\n  - path: /etc/datagvat-domain\n    permissions: "0644"\n    content: "%s\\n"\n' "$DOMAIN" >> "$TMP_CI"
  CLOUD_INIT="$TMP_CI"
fi

# 3. Instanz anlegen (idempotent)
if ! exo compute instance show "$NAME" --zone "$ZONE" >/dev/null 2>&1; then
  step "Instanz '$NAME' ($INSTANCE_TYPE in $ZONE) anlegen"
  exo compute instance create "$NAME" \
    --zone "$ZONE" \
    --template "$TEMPLATE" \
    --instance-type "$INSTANCE_TYPE" \
    --disk-size "$DISK_SIZE" \
    --ssh-key "$SSH_KEY" \
    --security-group "$SG_NAME" \
    --cloud-init "$CLOUD_INIT" >/dev/null
else
  step "Instanz '$NAME' bereits vorhanden"
fi

IP="$(exo compute instance show "$NAME" --zone "$ZONE" -O json | jq -r '.ip_address // empty')"
[ -n "$IP" ] || { echo "FEHLER: Konnte IPv4-Adresse der Instanz nicht ermitteln." >&2; exit 1; }
if [ -z "$DOMAIN" ]; then
  DOMAIN="$(echo "$IP" | tr . -).sslip.io"
fi
step "Instanz laeuft unter $IP (Hostname: $DOMAIN); warte auf https://$DOMAIN/healthz"

# 4. Auf /healthz ueber HTTPS warten (Zertifikat + Container-Start)
READY=0
for _ in $(seq 1 90); do
  if curl -fsS --max-time 5 "https://${DOMAIN}/healthz" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 3
done
[ "$READY" = 1 ] || { echo "FEHLER: https://${DOMAIN}/healthz antwortet nicht innerhalb von 270 s. Pruefe per SSH: sudo cat /var/log/cloud-init-output.log" >&2; exit 1; }
step "HTTPS /healthz erreichbar"

# 5. initialize ueber SSH-geholtes Token pruefen (Token bleibt auf dem Zielsystem und wird nicht ausgegeben)
if command -v ssh >/dev/null 2>&1; then
  TOKEN_VAL="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 "ubuntu@${IP}" \
    "sudo sed -n 's/^MCP_AUTH_TOKEN=//p' /opt/datagvat-mcp/deploy/exoscale/.env" 2>/dev/null || true)"
  if [ -n "$TOKEN_VAL" ]; then
    RESP="$(curl -fsS --max-time 15 -X POST "https://${DOMAIN}/mcp" \
      -H "Authorization: Bearer ${TOKEN_VAL}" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"up-sh","version":"0"}}}' || true)"
    unset TOKEN_VAL
    if echo "$RESP" | grep -q 'data-gv-at'; then
      step "MCP initialize ueber https://${DOMAIN}/mcp erfolgreich"
    else
      step "WARNUNG: initialize hat nicht 'data-gv-at' geliefert"
    fi
  else
    step "Hinweis: Token per SSH nicht automatisch abrufbar (Agent/Schluessel); initialize manuell pruefen"
  fi
fi

echo ""
echo "Fertig in $(( $(date +%s) - T0 )) s."
echo "  URL:          https://${DOMAIN}/mcp"
echo "  Healthcheck:  https://${DOMAIN}/healthz"
echo "  Token-Fundort: ssh ubuntu@${IP} 'sudo cat /opt/datagvat-mcp/deploy/exoscale/.env'"
echo "  Rueckbau:     ./deploy/exoscale/down.sh --zone ${ZONE} --name ${NAME}"
