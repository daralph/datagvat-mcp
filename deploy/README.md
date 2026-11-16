# Betrieb auf einem Server

Voraussetzung für beide Wege ist das veröffentlichte Container-Image `ghcr.io/daralph/datagvat-mcp:1.1` (öffentlich über `.github/workflows/release.yml` bereitgestellt).

Pro LibreChat-Installation wählt man **entweder** Weg A (eigene VM per HTTPS) **oder** Weg B (Mit-Container direkt auf dem LibreChat-Host).

## Weg A: Exoscale (oder jede Ubuntu-VM mit öffentlicher IPv4)

Mit eingerichteter `exo`-CLI in einem Befehl:

```bash
./deploy/exoscale/up.sh --domain mcp.example.org
```

Details (Voraussetzungen, LibreChat-Remote-Einbindung für alle Nutzerinnen, Security-Group-Härtung, Token-Rotation, Rückbau): `deploy/exoscale/README.md`.

Manuell ohne `exo`-CLI:
1. Instanz mit Ubuntu 24.04 anlegen, Security Group mit 80 und 443 offen (Port 443 optional auf die IP des LibreChat-Servers einschränken) und 22 nur von der eigenen Admin-IP.
2. Den Inhalt von `deploy/exoscale/cloud-init.yaml` als „User Data“ angeben (bei eigener Domain vorab `/etc/datagvat-domain` schreiben oder `MCP_DOMAIN` setzen).
3. Nach dem Start per SSH: `sudo cat /opt/datagvat-mcp/deploy/exoscale/.env` zeigt Adresse und Token.

Ohne eigene Domain bildet `first-boot.sh` den Hostnamen aus der IP über `sslip.io` (für produktive Umgebungen ist eine eigene Subdomain mit `--domain` vorzuziehen).

Einbinden:
- **LibreChat (Remote-VM für alle Nutzerinnen):** In `librechat.yaml` `type: streamable-http`, `url: "https://<MCP_DOMAIN>/mcp"`, `requiresOAuth: false`, `chatMenu: true` und `Authorization: "Bearer ${DATAGVAT_MCP_TOKEN}"` eintragen (Details in `deploy/exoscale/README.md`).
- **claude.ai / Custom Connector:** URL `https://<MCP_DOMAIN>/mcp`, Request-Header `Authorization` mit Wert `Bearer <Token>`.

Update: `docker compose --env-file .env pull && docker compose --env-file .env up -d` im Ordner `deploy/exoscale`. Zweites Token für den Wechsel: in `.env` mit Komma anhängen, neu starten, altes später entfernen.

## Weg B: LibreChat auf demselben Host (Docker Compose)

Im LibreChat-Ordner:

```
bash <Pfad zum Repo>/deploy/librechat/install.sh
```

Das Skript erzeugt das Token, bindet den Dienst über eine eigene Compose-Datei ein, ergänzt `librechat.yaml`, startet und prüft. `--dry-run` zeigt nur den Plan, `--uninstall` nimmt die eigenen Änderungen zurück, `--no-start` richtet nur die Dateien ein. Die Dateibehandlung ist mit `deploy/librechat/test-install.sh` geprüft (sechs Fälle), der Teil mit Docker nicht.

## Prüfen

`bash deploy/smoke-test.sh <image>` startet das Image mit der Härtung aus den Compose-Dateien (schreibgeschützt, `/tmp` als Volume, mit `TMP_ART=tmpfs` als tmpfs) und prüft Healthcheck, 401, `initialize` und den geordneten Stop. Die CI führt das bei jedem Push aus.
