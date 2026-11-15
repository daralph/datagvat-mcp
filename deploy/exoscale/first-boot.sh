#!/bin/sh
# Erster Start auf der Instanz: Hostname bestimmen, Token erzeugen, Dienste starten. Wiederholbar.
# Eigene Domain: vorher MCP_DOMAIN setzen (DNS-Eintrag muss schon auf die Instanz zeigen).
set -eu
cd "$(dirname "$0")"
umask 077
if [ ! -f .env ]; then
  DOMAIN="${MCP_DOMAIN:-}"
  if [ -z "$DOMAIN" ]; then
    # Ohne Domain: Hostname aus der IPv4-Adresse ueber einen Wildcard-DNS-Dienst (keine DNS-Wartezeit).
    IP=$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
    [ -n "$IP" ] || { echo "Keine IPv4-Adresse gefunden. MCP_DOMAIN setzen und erneut starten." >&2; exit 1; }
    DOMAIN="$(echo "$IP" | tr . -).sslip.io"
  fi
  { echo "MCP_DOMAIN=$DOMAIN"; echo "MCP_AUTH_TOKEN=$(openssl rand -hex 32)"; } > .env
fi
docker compose -f compose.yml --env-file .env up -d
echo "Gestartet. Adresse und Token stehen in $(pwd)/.env (nur fuer root lesbar)."
