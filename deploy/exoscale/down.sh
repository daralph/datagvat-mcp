#!/usr/bin/env bash
# Entfernt genau die von up.sh angelegte Exoscale-Instanz und Security Group. Wiederholbar.
set -euo pipefail

NAME="${EXO_INSTANCE_NAME:-datagvat-mcp}"
SG_NAME="${EXO_SG_NAME:-${NAME}-sg}"
ZONE="${EXO_ZONE:-at-vie-1}"

while [ $# -gt 0 ]; do
  case "$1" in
    --zone) ZONE="${2:?--zone braucht eine Zone}"; shift 2 ;;
    --name) NAME="${2:?--name braucht einen Namen}"; SG_NAME="${NAME}-sg"; shift 2 ;;
    -h|--help)
      echo "Aufruf: $0 [--zone at-vie-1] [--name datagvat-mcp]"
      exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 64 ;;
  esac
done

command -v exo >/dev/null 2>&1 || { echo "FEHLER: exo-CLI nicht gefunden." >&2; exit 1; }

if exo compute instance show "$NAME" --zone "$ZONE" >/dev/null 2>&1; then
  echo "Loesche Instanz '$NAME' in $ZONE..."
  exo compute instance delete "$NAME" --zone "$ZONE" --force --quiet
else
  echo "Instanz '$NAME' in $ZONE nicht vorhanden (uebersprungen)."
fi

if exo compute security-group show "$SG_NAME" >/dev/null 2>&1; then
  echo "Loesche Security Group '$SG_NAME'..."
  exo compute security-group delete "$SG_NAME" --force --quiet
else
  echo "Security Group '$SG_NAME' nicht vorhanden (uebersprungen)."
fi
echo "Rueckbau abgeschlossen."
