# LibreChat-Betrieb (Weg B): datagvat-mcp als Mit-Container

Diese Anleitung richtet sich an den Betreiber einer bestehenden LibreChat-Installation per Docker Compose. Sie beschreibt Einbau, Betrieb und Rückbau des MCP-Servers `mcp-datagvat` als zusätzlichen Container auf demselben Host. Der alternative Weg A (eigene VM mit HTTPS) ist in `deploy/exoscale/README.md` beschrieben.

## Voraussetzungen

- Eine LibreChat-Installation per Docker Compose: ein Ordner mit `docker-compose.yml`, `.env` und `librechat.yaml`.
- Zugriff auf `ghcr.io` für das öffentliche Image `ghcr.io/daralph/datagvat-mcp:1.2` (bereitgestellt über `.github/workflows/release.yml`).
- `docker compose` und `openssl` auf dem Host.
- Dieses Repo auf dem Host verfügbar (geklont oder als Kopie), Aufrufe erfolgen aus dem LibreChat-Ordner heraus.

## Einbau

### Mit install.sh (empfohlen)

Im LibreChat-Ordner:

```bash
bash <Pfad zum Repo>/deploy/librechat/install.sh
```

Das Skript erzeugt das Bearer-Token, kopiert `deploy/librechat/compose.datagvat.yml` als `docker-compose.datagvat.yml` in den LibreChat-Ordner, ergänzt `librechat.yaml` um den MCP-Block, startet die Dienste und prüft das Ergebnis. Vor jeder Dateiänderung legt es eine Sicherungskopie an.

Flags:

- `--dry-run`: zeigt nur den Plan, ändert nichts.
- `--no-start`: richtet nur die Dateien ein, startet nicht.
- `--no-token`: Betrieb ohne Bearer-Token (nur in einem abgeschotteten Netz, siehe Abschnitt Netz).
- `--uninstall`: nimmt die eigenen Änderungen zurück (siehe Abschnitt Betrieb).

Änderungen im Einzelnen:

- `.env`: ein neuer Eintrag `DATAGVAT_MCP_TOKEN=<Zufallswert>` (bei `--no-token` stattdessen `DATAGVAT_MCP_ALLOW_NO_AUTH=1`).
- `docker-compose.datagvat.yml`: neue Datei mit dem Dienst `mcp-datagvat` und `depends_on` beim Dienst `api`.
- `librechat.yaml`: neuer Eintrag `datagvat` unter `mcpServers` (Inhalt wie in `librechat.mcp.example.yaml`).

Die Dateibehandlung des Skripts ist mit `deploy/librechat/test-install.sh` geprüft, der Teil mit Docker nicht.

### Einbau von Hand

1. Token erzeugen und in die LibreChat-`.env` eintragen:

   ```bash
   echo "DATAGVAT_MCP_TOKEN=$(openssl rand -hex 32)" >> .env
   ```

2. Compose-Erweiterung: `deploy/librechat/compose.datagvat.yml` als `docker-compose.datagvat.yml` in den LibreChat-Ordner kopieren und beim Start mit `-f docker-compose.yml -f docker-compose.datagvat.yml` einbinden. Kern des Dienstes (Auszug):

   ```yaml
   services:
     api:
       volumes:
         - type: bind
           source: ./librechat.yaml
           target: /app/librechat.yaml
       depends_on:
         - mcp-datagvat

     mcp-datagvat:
       image: ghcr.io/daralph/datagvat-mcp:${DATAGVAT_MCP_IMAGE_TAG:-1.2}
       container_name: mcp-datagvat
       restart: unless-stopped
       environment:
         - MCP_TRANSPORT=streamable-http
         - MCP_HOST=0.0.0.0
         - MCP_PORT=8000
         - MCP_STATELESS=1
         - MCP_AUTH_TOKEN=${DATAGVAT_MCP_TOKEN:-}
         - MCP_ALLOW_NO_AUTH=${DATAGVAT_MCP_ALLOW_NO_AUTH:-0}
   ```

   Maßgeblich ist die Datei `deploy/librechat/compose.datagvat.yml` im Repo; sie enthält zusätzlich die Härtung (schreibgeschütztes Wurzeldateisystem, `/tmp` als tmpfs, keine Capabilities).

3. `librechat.yaml`: den Eintrag `datagvat` unter `mcpServers` aus `librechat.mcp.example.yaml` übernehmen (gibt es `mcpServers:` schon, nur den Eintrag ergänzen). Variante mit Token:

   ```yaml
   mcpServers:
     datagvat:
       type: streamable-http
       url: "http://mcp-datagvat:8000/mcp"
       timeout: 150000
       initTimeout: 15000
       chatMenu: true
       requiresOAuth: false
       headers:
         Authorization: "Bearer ${DATAGVAT_MCP_TOKEN}"
   ```

   Der Block `serverInstructions` aus `librechat.mcp.example.yaml` wird empfohlen (Arbeitsablauf für die Tools). Variante ohne Token: in `.env` `DATAGVAT_MCP_ALLOW_NO_AUTH=1` setzen und die beiden `headers`-Zeilen löschen. Ohne Token und ohne `MCP_ALLOW_NO_AUTH=1` verweigert der Server auf einer Nicht-Loopback-Adresse den Start (Exit-Code 2).

4. Starten und prüfen:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.datagvat.yml up -d
   docker compose ps                    # mcp-datagvat muss "healthy" werden
   ```

## Ressourcen

Aus `deploy/librechat/compose.datagvat.yml` (Dienst `mcp-datagvat`):

- Speicher: `mem_limit: 2g`.
- Prozesse: `pids_limit: 256`.
- `/tmp` ist ein tmpfs mit `size=1300m`. Es zählt zum Speicherlimit und nimmt die DuckDB-Auslagerung (bis 1 GB) und den Datei-Cache auf.
- CPU und Plattenplatz: keine Vorgabe im Repo, vom Betreiber zu prüfen. Das Wurzeldateisystem ist schreibgeschützt, der Container belegt außer dem Image keinen Plattenplatz.

Server-interne Grenzen (zur Einordnung, aus README.md, Abschnitt Sicherheit): DuckDB höchstens 512 MB RAM und 1 GB Auslagerung, 30 s je Abfrage (Laden einer Datei: 60 s); Downloads höchstens 120 s Gesamtzeit, höchstens 4 gleichzeitige Aufrufe mit Datei, 50 MB pro Download (entpackt 200 MB).

## Netz

- Der Dienst veröffentlicht keinen Port auf dem Host; er ist nur im Compose-Netz unter `mcp-datagvat:8000` erreichbar.
- Ausgehend braucht der Container HTTPS zu `data.gv.at` (Katalog-API) UND zu den Hosts der Datenquellen: die Distributionen liegen auf beliebigen öffentlichen Hosts der jeweiligen Publisher.
- Einschränkung der Abrufziele über `DATAGVAT_ALLOWED_HOSTS` (Suffix-Liste, z. B. `gv.at,statistik.at`; leer = alle öffentlichen Hosts).
- SSRF-Schutz des Servers: Jede URL und jede Weiterleitung (max. 5) wird per DNS auf öffentliche IPs geprüft (`ipaddress.is_global`, zusätzlich NAT64 gesperrt). Die verbindliche Prüfung läuft beim Verbindungsaufbau: der Server löst den Host selbst auf, prüft alle Adressen und verbindet auf genau diese IP (TLS/SNI weiter über den Hostnamen).

## LibreChat-Seite

- `timeout: 150000` (Millisekunden): Downloads dürfen serverseitig bis 120 s dauern, der Client-Timeout muss darüber liegen.
- `initTimeout: 15000` (Millisekunden) für den Verbindungsaufbau.
- `chatMenu: true`: der Server erscheint im Chat-Menü und ist für alle Nutzerinnen der LibreChat ohne eigene Anmeldung nutzbar.
- Hinweis: Blockiert der SSRF-Schutz der LibreChat den internen Dienstnamen `mcp-datagvat:8000`, den Eintrag über `mcpSettings.allowedAddresses` in `librechat.yaml` freigeben. Dieser Schritt ist noch nicht in einer laufenden LibreChat bestätigt und vom Betreiber zu prüfen.

## Betrieb

- Logs: `docker compose logs mcp-datagvat` (im LibreChat-Ordner, ggf. mit den `-f`-Dateien wie beim Start).
- Healthcheck: `GET /healthz` antwortet ohne Token mit Status und Version; der Docker-Healthcheck prüft den TCP-Port, `docker compose ps` zeigt `healthy`.
- Update: in `.env` `DATAGVAT_MCP_IMAGE_TAG=<neuer Tag>` setzen, dann `docker compose -f docker-compose.yml -f docker-compose.datagvat.yml pull && docker compose -f docker-compose.yml -f docker-compose.datagvat.yml up -d`. Rückfall: den vorigen Tag in `DATAGVAT_MCP_IMAGE_TAG` eintragen und erneut `pull`/`up -d` ausführen.
- Rückbau: `bash <Pfad zum Repo>/deploy/librechat/install.sh --uninstall` entfernt die eigenen Änderungen an `.env`, `librechat.yaml` und `docker-compose.datagvat.yml`.

## Datenhaltung

- Der Server speichert keine Nutzer- oder Chatdaten. Es fallen nur Zwischendateien an: heruntergeladene Dateien liegen in einem eigenen temporären Verzeichnis `datagvat-<PID>-…` unter `/tmp` (im Container ein tmpfs), das beim Beenden entfernt wird; Reste abgestürzter Prozesse räumt der nächste Start weg.
- Ein gemeinsamer Lese-Cache hält heruntergeladene Dateien bis zu 900 s (`DATAGVAT_CACHE_TTL`) und höchstens 200 MB (`DATAGVAT_CACHE_MAX_BYTES`) vor, ohne Bezug zu einzelnen Nutzerinnen.
- Nach außen gehen: die Suchbegriffe und Filter an die Katalog-API `data.gv.at` sowie die Abruf-URLs (HTTP-GET) an die Hosts der Datenquellen. Was Nutzerinnen sonst eingeben, verlässt den Server nicht.
