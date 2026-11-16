# Exoscale-Betrieb (Weg A)

## Voraussetzungen (einmalig)
- Exoscale-Konto mit eingerichteter [`exo`-CLI](https://community.exoscale.com/documentation/tools/exoscale-command-line-interface/) (`exo config`), `jq` und `curl`
- Mindestens ein bei Exoscale hinterlegter SSH-Schlüssel (`exo compute ssh-key register`)
- Veröffentlichtes Container-Image `ghcr.io/daralph/datagvat-mcp:1.1`

## Start in einem Befehl
```bash
./deploy/exoscale/up.sh --domain mcp.example.org
```
Für einen Schnelltest ohne eigene Domain geht auch `./deploy/exoscale/up.sh` (nutzt `<ip>.sslip.io`); im Produktivbetrieb ist eine eigene Subdomain empfohlen (`--domain`, DNS-A-Record vorher oder direkt nach dem Start auf die Ziel-IP setzen).

Das Skript legt eine Security Group (`80` und `443` offen, `22` nur von der eigenen aktuellen IPv4), eine Instanz in `at-vie-1` (Wien) mit `cloud-init.yaml` an, wartet auf `https://<Host>/healthz` und prüft `initialize`. Das Bearer-Token wird erst auf der Instanz erzeugt (`first-boot.sh`) und steht in `/opt/datagvat-mcp/deploy/exoscale/.env` (Rechte `600`, nicht in den User-Daten oder Logs).

## Wahl des Instanztyps
Standard ist `standard.small` (2 vCPU, 2 GB RAM). Begründung aus den Server-Limits:
- DuckDB darf pro Datei-Aufruf höchstens 512 MB RAM belegen (`DATAGVAT_MAX_PARALLEL=2` in `compose.yml`, also max. 1 GB für DuckDB), der Container selbst ist auf `1500m` begrenzt.
- Für Caddy und das Grundsystem bleiben ca. 500 MB.
- **Für LibreChat mit mehreren gleichzeitigen Nutzerinnen empfohlen:** `--instance-type standard.medium` (4 GB RAM, 2 vCPU) und in `/opt/datagvat-mcp/deploy/exoscale/.env` `DATAGVAT_MAX_PARALLEL=4` (sowie ggf. `MCP_MEM_LIMIT=2800m`) setzen.

## Einbinden in LibreChat (Remote-VM, für alle Nutzerinnen)
Der Server läuft mit `MCP_STATELESS=1` (zustandsloser Streamable-HTTP-Betrieb) und speichert keinerlei Chat- oder Nutzerdaten – nur ein gemeinsamer Lese-Cache für heruntergeladene Open-Data-Dateien liegt bis zu 15 Minuten in `/tmp`.

1. **Token von der Exoscale-VM holen:**
   ```bash
   ssh ubuntu@<VM-IP> 'sudo cat /opt/datagvat-mcp/deploy/exoscale/.env'
   ```
   Den Wert von `MCP_AUTH_TOKEN` als `DATAGVAT_MCP_TOKEN=<Token>` in die `.env` von LibreChat übernehmen.
2. **In `librechat.yaml` einmal zentral eintragen** (gilt dann über `chatMenu: true` für alle Nutzerinnen ohne eigene Anmeldung):
   ```yaml
   mcpServers:
     datagvat:
       type: streamable-http
       url: "https://<MCP_DOMAIN>/mcp"
       timeout: 150000
       initTimeout: 15000
       chatMenu: true
       requiresOAuth: false
       headers:
         Authorization: "Bearer ${DATAGVAT_MCP_TOKEN}"
       serverInstructions: >
         Workflow fuer oesterreichische Open Data (data.gv.at):
         1. Suche Datensaetze mit search_datasets.
         2. Hole Distributionen mit get_dataset_details und nimm recommended_url (Feld 'url' je Distribution).
            Scheitert das Laden (404, HTML statt Datei), get_dataset_details mit check_links=true wiederholen.
         3. Pruefe bei CSV/JSON/Excel/ZIP immer zuerst das Schema via inspect_distribution_schema.
         4. Werte Daten gezielt per SQL ueber query_distribution_sql auf der Tabelle 'dataset' aus.
         5. Codes (z. B. Bezirkscodes) mit den Codelisten aus 'lookup_tables' entschluesseln:
            per extra_tables mitladen und per JOIN verbinden.
   ```
3. **Empfohlene Härtung für exklusiven LibreChat-Betrieb:**
   - Nach erfolgreicher Zertifikatsausstellung die Regel für Port `443` in der Exoscale Security Group auf die Ausgangs-IP bzw. das Netz der LibreChat-Instanz einschränken (Port `80` nur offen lassen, falls HTTP-01-Erneuerungen darüber laufen).
   - Optional `DATAGVAT_ALLOWED_HOSTS` in `.env` setzen, falls nur bestimmte Zielhosts abgerufen werden dürfen (leer = alle öffentlichen Hosts).

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

## Hinweise zur Prüfung
- `docker compose config`, `caddy validate` sowie die Syntax- und Image-Rauchtests laufen automatisiert in der CI.
- Der Live-Lauf gegen Exoscale (`up.sh`), Let's-Encrypt-Zertifizierung und die Einbindung in einer laufenden LibreChat-Instanz sollten beim erstmaligen Ausrollen auf dem Zielsystem per Chat-Test verifiziert werden.
