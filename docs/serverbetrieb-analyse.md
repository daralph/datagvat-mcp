# Serverbetrieb: Analyse und Stand (Meilenstein 1)

Gemessen am 15.11.2026 in einer Linux-Umgebung ohne Docker-Daemon, mit mcp 1.30.0 und 2.3.0, Python 3.12.

## Belegt und behoben

| Lücke | Beleg | Änderung |
|---|---|---|
| Host-Header | Bind `127.0.0.1`: fremder Host-Header ergibt 421, fremder Origin 403. Bind `0.0.0.0`: beides 200, keine Prüfung. Beide SDK-Versionen gleich. | `MCP_ALLOWED_HOSTS`, `MCP_ALLOWED_ORIGINS` |
| SIGTERM | Prozess endet mit Exit-Code -15, das Temp-Verzeichnis bleibt liegen. Ursache: uvicorn löst das Signal nach dem geordneten Herunterfahren erneut aus, `atexit` läuft nicht. | Eigener SIGTERM-Handler, Exit-Code 0, Verzeichnis wird entfernt |
| Temp-Reste | In frischen PID-Namensräumen wiederholt sich die PID (dreimal PID 1, mit Shell davor zweimal PID 3). Reste mit der eigenen PID galten als „lebt noch“ und blieben für immer. | Reste mit eigener PID werden beim Start entfernt |
| Healthcheck | Die Token-Prüfung umschloss jede Route. | `GET`/`HEAD /healthz` ohne Token, exakter Pfad |
| Ein Token | Code-Stelle `_BearerAuthMiddleware`. | Komma-Liste in `MCP_AUTH_TOKEN`, dazu `MCP_AUTH_TOKEN_FILE` |
| Sitzungen | Sitzungen liegen im Speicher des Prozesses (SDK-Code). Der Server nutzt weder Fortschrittsmeldungen noch Abos. | `MCP_STATELESS=1`, Standard unverändert |
| Offener Abruf-Proxy | Jede öffentliche URL wird geladen (Code-Stelle `_validate_public_url`). | `DATAGVAT_ALLOWED_HOSTS`, Standard unverändert |

## Nicht getestet

- Alles mit Docker: Image-Build, Compose, Verhalten von `uv run` als PID 1, schreibgeschütztes Dateisystem.
- Exoscale, Caddy, LibreChat, claude.ai. Die Doku-Seiten waren aus der Arbeitsumgebung nicht erreichbar.
- Jede Zeitmessung. Es gibt noch keine Aussage, ob 5 oder 30 Minuten erreichbar sind.

## Meilenstein 2: geschrieben, statisch geprüft

`release.yml` (Image nach GHCR), `deploy/smoke-test.sh`, `deploy/exoscale` (Compose, Caddyfile, cloud-init, `first-boot.sh`), `deploy/librechat` (`install.sh`, Test). Geprüft: Shell-Syntax, `docker compose config` für beide Compose-Dateien, YAML der Workflows und von cloud-init, sechs Fälle der Dateibehandlung von `install.sh`.

Befund zum schreibgeschützten Dateisystem: `uv run --locked --offline` scheitert mit schreibgeschütztem Cache („Could not create temporary file … Read-only file system“). Deshalb steht in `compose.yml` kein `read_only`. Die CI prüft den Fall im echten Container als eigenen, nicht blockierenden Schritt.

## Offen

- Erster CI-Lauf mit Docker (Build, Rauchtest, Caddy-Prüfung).
- Tag `v1.1.0`, Image veröffentlichen und auf „public“ stellen.
- Echter Lauf auf Exoscale und in LibreChat, Zeitmessung für 5 und 30 Minuten.
- Ein-Befehl-Skripte `up.sh` und `down.sh` mit der `exo`-CLI.
- Zertifikat ohne DNS-Wartezeit belegen (sslip.io oder IP-Zertifikat).
