# Betrieb auf einem Server

Stand 15.11.2026: Dateien geschrieben und statisch geprüft, **noch nicht auf Exoscale, mit Caddy oder mit einer echten LibreChat-Instanz gelaufen**. Zeiten sind nicht gemessen. Details: `docs/serverbetrieb-analyse.md`.

Voraussetzung für beide Wege ist das veröffentlichte Image `ghcr.io/daralph/datagvat-mcp`. Es entsteht über `.github/workflows/release.yml` beim Tag `v1.1.0` und muss in GitHub einmal auf „public“ gestellt werden.

## Weg A: Exoscale (oder jede Ubuntu-VM mit öffentlicher IPv4)

1. Instanz mit Ubuntu 24.04 anlegen, Security Group mit 80 und 443 offen und 22 nur von der eigenen IP.
2. Den Inhalt von `deploy/exoscale/cloud-init.yaml` als „User Data“ angeben.
3. Nach dem Start per SSH: `sudo cat /opt/datagvat-mcp/deploy/exoscale/.env` zeigt Adresse und Token.

Ohne eigene Domain bildet `first-boot.sh` den Hostnamen aus der IP über sslip.io. Ob die Zertifizierungsstelle dafür zuverlässig ausstellt, ist ungeprüft. Mit eigener Domain: DNS-Eintrag vorher setzen und `MCP_DOMAIN` vor `first-boot.sh` exportieren.

Einbinden in claude.ai als Custom Connector: URL `https://<MCP_DOMAIN>/mcp`, Request-Header `Authorization` mit Wert `Bearer <Token>`.

Update: `docker compose --env-file .env pull && docker compose --env-file .env up -d` im Ordner `deploy/exoscale`. Zweites Token für den Wechsel: in `.env` mit Komma anhängen, neu starten, altes später entfernen.

Ein Ein-Befehl-Skript mit der `exo`-CLI fehlt noch, weil die CLI-Syntax nicht gegen die Doku geprüft werden konnte.

## Weg B: LibreChat

Im LibreChat-Ordner:

```
bash <Pfad zum Repo>/deploy/librechat/install.sh
```

Das Skript erzeugt das Token, bindet den Dienst über eine eigene Compose-Datei ein, ergänzt `librechat.yaml`, startet und prüft. `--dry-run` zeigt nur den Plan, `--uninstall` nimmt die eigenen Änderungen zurück, `--no-start` richtet nur die Dateien ein. Die Dateibehandlung ist mit `deploy/librechat/test-install.sh` geprüft (sechs Fälle), der Teil mit Docker nicht.

## Prüfen

`bash deploy/smoke-test.sh <image>` startet das Image mit der Härtung aus `compose.yml` und prüft Healthcheck, 401, `initialize` und den geordneten Stop. Die CI führt das bei jedem Push aus.
