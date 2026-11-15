#!/usr/bin/env bash
# Prueft die Dateibehandlung von install.sh ohne Docker: Einrichten, Wiederholung, Rueckbau, Sonderfaelle.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail() { echo "FEHLER: $*" >&2; exit 1; }
run() { (cd "$1" && shift && bash "$HERE/install.sh" "$@" >/dev/null); }
fresh() { rm -rf "$WORK/lc" "$WORK/orig"; mkdir -p "$WORK/lc"; printf 'services:\n  api:\n    image: stub\n' > "$WORK/lc/docker-compose.yml"; printf 'HOST=localhost\nPORT=3080\n' > "$WORK/lc/.env"; }
snapshot() { mkdir -p "$WORK/orig"; cp -p "$WORK/lc/.env" "$WORK/orig/"; if [ -f "$WORK/lc/librechat.yaml" ]; then cp -p "$WORK/lc/librechat.yaml" "$WORK/orig/"; fi; }
same_as_before() { cmp -s "$WORK/lc/.env" "$WORK/orig/.env" || fail "$1: .env weicht ab"; if [ -f "$WORK/orig/librechat.yaml" ]; then cmp -s "$WORK/lc/librechat.yaml" "$WORK/orig/librechat.yaml" || fail "$1: librechat.yaml weicht ab"; else [ ! -e "$WORK/lc/librechat.yaml" ] || fail "$1: librechat.yaml blieb liegen"; fi; [ ! -e "$WORK/lc/docker-compose.datagvat.yml" ] || fail "$1: Compose-Datei blieb liegen"; }
valid_yaml() { python3 - "$1" <<'PY'
import sys
try:
    import yaml
except ImportError:
    sys.exit(0)
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
assert doc["mcpServers"]["datagvat"]["url"] == "http://mcp-datagvat:8000/mcp", doc
assert doc["mcpServers"]["datagvat"]["headers"]["Authorization"] == "Bearer ${DATAGVAT_MCP_TOKEN}"
PY
}

# Fall 1: bestehende librechat.yaml mit eigenem Server, Kommentaren und 4 Leerzeichen Einrueckung
fresh
printf 'version: 1.2.1\n# mein Kommentar\nmcpServers:\n    # eigener Server\n    andere:\n        type: sse\n        url: "http://x/sse"\ninterface:\n  privacyPolicy: {}\n' > "$WORK/lc/librechat.yaml"
snapshot
run "$WORK/lc" --dry-run; same_as_before "Probelauf" 2>/dev/null || cmp -s "$WORK/lc/.env" "$WORK/orig/.env" || fail "Probelauf hat geaendert"
run "$WORK/lc" --no-start
grep -qE '^DATAGVAT_MCP_TOKEN=[0-9a-f]{64}$' "$WORK/lc/.env" || fail "Token fehlt"
grep -qx 'COMPOSE_FILE=docker-compose.yml:docker-compose.datagvat.yml' "$WORK/lc/.env" || fail "COMPOSE_FILE fehlt"
grep -q '^    datagvat:$' "$WORK/lc/librechat.yaml" || fail "Eintrag nicht mit der vorhandenen Einrueckung"
grep -q '^    andere:$' "$WORK/lc/librechat.yaml" || fail "fremder Server verloren"
valid_yaml "$WORK/lc/librechat.yaml"
cp "$WORK/lc/.env" "$WORK/env1"; cp "$WORK/lc/librechat.yaml" "$WORK/yaml1"
run "$WORK/lc" --no-start
cmp -s "$WORK/lc/.env" "$WORK/env1" && cmp -s "$WORK/lc/librechat.yaml" "$WORK/yaml1" || fail "zweiter Lauf hat geaendert"
[ "$(ls -d "$WORK"/lc/.datagvat-backup-* | wc -l | tr -d ' ')" = 1 ] || fail "Sicherung fehlt oder doppelt"
run "$WORK/lc" --uninstall --no-start; same_as_before "Rueckbau Fall 1"

# Fall 2: keine librechat.yaml, vorhandene Override-Datei
fresh; : > "$WORK/lc/docker-compose.override.yml"; snapshot
run "$WORK/lc" --no-start
grep -qx 'COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.datagvat.yml' "$WORK/lc/.env" || fail "Override nicht beruecksichtigt"
valid_yaml "$WORK/lc/librechat.yaml"
run "$WORK/lc" --uninstall --no-start; same_as_before "Rueckbau Fall 2"

# Fall 3: librechat.yaml ohne mcpServers
fresh; printf 'version: 1.2.1\ncache: true\n' > "$WORK/lc/librechat.yaml"; snapshot
run "$WORK/lc" --no-start; valid_yaml "$WORK/lc/librechat.yaml"
run "$WORK/lc" --uninstall --no-start; same_as_before "Rueckbau Fall 3"

# Fall 4: nicht sicher zusammenfuehrbar -> Abbruch ohne Aenderung
fresh; printf 'mcpServers: {}\n' > "$WORK/lc/librechat.yaml"; snapshot
if run "$WORK/lc" --no-start 2>/dev/null; then fail "Kurzschreibweise haette abbrechen muessen"; fi
same_as_before "Abbruch Fall 4"
fresh; echo 'COMPOSE_FILE=a.yml:b.yml' >> "$WORK/lc/.env"; snapshot
if run "$WORK/lc" --no-start 2>/dev/null; then fail "fremdes COMPOSE_FILE haette abbrechen muessen"; fi
same_as_before "Abbruch Fall 5"

# Fall 6: Token schon von Hand gesetzt -> bleibt, kein zweites
fresh; echo 'DATAGVAT_MCP_TOKEN=vonhandgesetztvonhandgesetzt' >> "$WORK/lc/.env"
run "$WORK/lc" --no-start
[ "$(grep -c '^DATAGVAT_MCP_TOKEN=' "$WORK/lc/.env")" = 1 ] || fail "zweites Token angelegt"
echo "install.sh: alle Faelle ok"
