# Exoscale-Betrieb (Weg A)

## Voraussetzungen (einmalig)
- Exoscale-Konto mit eingerichteter [`exo`-CLI](https://community.exoscale.com/documentation/tools/exoscale-command-line-interface/) (`exo config`), `jq` und `curl`
- Mindestens ein bei Exoscale hinterlegter SSH-Schlüssel (`exo compute ssh-key register`)
- Veröffentlichtes Container-Image `ghcr.io/daralph/datagvat-mcp:1.1` (über Tag `v1.1.0` und `release.yml`, auf GitHub auf *public* gestellt)

## Start in einem Befehl
```bash
./deploy/exoscale/up.sh
```
Mit eigener Domain (DNS-A-Record vorher auf die Ziel-IP setzen, oder nach dem ersten Lauf nachtragen):
```bash
./deploy/exoscale/up.sh --domain mcp.example.org
```
Das Skript legt eine Security Group (`80` und `443` offen, `22` nur von der eigenen aktuellen IPv4), eine Instanz in `at-vie-1` (Wien) mit `cloud-init.yaml` an, wartet auf `https://<Host>/healthz` und prüft `initialize`. Das Bearer-Token wird erst auf der Instanz erzeugt (`first-boot.sh`) und steht in `/opt/datagvat-mcp/deploy/exoscale/.env` (Rechte `600`, nicht in den User-Daten oder Logs).

## Wahl des Instanztyps
Standard ist `standard.small` (2 vCPU, 2 GB RAM). Begründung aus den Server-Limits:
- DuckDB darf pro Datei-Aufruf höchstens 512 MB RAM belegen (`DATAGVAT_MAX_PARALLEL=2` in `compose.yml`, also max. 1 GB für DuckDB), der Container selbst ist auf `1500m` begrenzt.
- Für Caddy und das Grundsystem bleiben ca. 500 MB. Für höhere Parallelität (`DATAGVAT_MAX_PARALLEL=4`) `--instance-type standard.medium` (4 GB RAM, 2 vCPU) wählen.

## Einbinden in Claude Code und claude.ai
**Claude Code:**
```bash
claude mcp add --transport http data-gv-at "https://<MCP_DOMAIN>/mcp" --header "Authorization: Bearer <TOKEN>"
```

**claude.ai (Custom Connector):**
- URL: `https://<MCP_DOMAIN>/mcp`
- Request-Header: `Authorization` mit Wert `Bearer <TOKEN>` (Hinweis: claude.ai-Web-Connectors nutzen je nach Plan/Ausrollung primär OAuth 2.1 oder feste Header und verbinden ausschließlich über IPv4; eine reine AAAA-Domain funktioniert dort nicht).
- Bei zusätzlicher IP-Filterung am Proxy müssen die dokumentierten Anthropic-Ausgangsbereiche (`160.79.104.0/21`, `153.61.208.0/20`) erlaubt sein.

## Update und Token-Wechsel ohne Ausfall
Per SSH auf der Instanz unter `/opt/datagvat-mcp/deploy/exoscale`:
- **Image aktualisieren:** `docker compose --env-file .env pull && docker compose --env-file .env up -d`
- **Token rotieren:** neues Token mit Komma in `MCP_AUTH_TOKEN=<alt>,<neu>` in `.env` anhängen, `docker compose --env-file .env up -d`, Clients umstellen, danach das alte Token aus `.env` entfernen und erneut `up -d` ausführen.

## Rückbau
```bash
./deploy/exoscale/down.sh
```
Entfernt genau die von `up.sh` angelegte Instanz und Security Group.

## Status
- **Statisch geprüft (15.11.2026):** `docker compose config`, `caddy validate` (in CI), `bash -n up.sh down.sh first-boot.sh` (lokal).
- **Nicht getestet (Stand 15.11.2026):** echter Lauf gegen Exoscale (`exo`-CLI in der Arbeitsumgebung nicht installiert), echtes Let's-Encrypt-Zertifikat über `sslip.io` und Zeitmessung.
