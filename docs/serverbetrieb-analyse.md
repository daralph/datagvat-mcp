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

## Gemessen auf der Test-Docker-Umgebung (DS218+, 2 Kerne Celeron J3355, 6 GB, Docker 24.0.2, 15.11.2026)

| Lücke | Messung | Folge |
|---|---|---|
| L11 PID 1 | Im 1.0.0-Image ist `uv` PID 1. Stoppen dauert 7 s, Exit 0. | Image startet jetzt `python` direkt (PID 1), mit `init: true` im Compose. Stoppen 4 s, Exit 0. |
| L12 Härtung | Mit `--read-only` startet 1.0.0 nicht: `uv` schreibt in `/opt/uv-cache`. | Mit venv-Image läuft `read_only` + `cap_drop ALL` + `no-new-privileges` + `tmpfs /tmp` + `init` + 1 GB Limit. |
| Image-Größe | 289 MB (1.0.0) gegen 231 MB (zweistufig). | siehe `docs/entscheidungen.md` |
| Bereitschaft | `/healthz` nach 9 bis 16 s auf der DS218+ (Python-Import von duckdb/mcp). | Zeitbudget für Weg B anpassen |
| DuckDB 2.0.0 | `SIGILL` auf CPU ohne AVX, Build brach bei `--selftest` mit Exit 132 ab. 1.1.3 bis 1.5.6 laufen. | Hinweis im README, Auftrag-Grundlage für L4/Exoscale unberührt (moderne CPU) |
| MongoDB | Dieselbe CPU hat kein AVX: LibreChat (MongoDB 5+) läuft auf der DS218+ nicht ohne `mongo:4.4`. | Weg B wird auf der DS218+ nur mit `mongo:4.4` testbar |

## Nicht getestet

- Exoscale, Caddy mit echtem Zertifikat, LibreChat, claude.ai. Die Doku-Seiten waren aus der Arbeitsumgebung nicht erreichbar.
- Jede Zeitmessung der beiden Wege. Es gibt noch keine Aussage, ob 5 oder 30 Minuten erreichbar sind.

## Meilenstein 2: geschrieben, in der CI geprüft

`release.yml` (Image nach GHCR), `deploy/smoke-test.sh`, `deploy/exoscale` (Compose, Caddyfile, cloud-init, `first-boot.sh`), `deploy/librechat` (`install.sh`, Test). Die CI baut das Image, fährt den Rauchtest, prüft `compose.yml` und das Caddyfile und spielt sechs Fälle der Dateibehandlung von `install.sh` durch.

Schreibgeschütztes Dateisystem: `uv run --locked --offline` scheiterte am schreibgeschützten Cache („Could not create temporary file … Read-only file system“). Das Image startet deshalb `python` aus einem venv (siehe `docs/entscheidungen.md`), und beide Compose-Dateien setzen `read_only: true`. `/tmp` ist bei Exoscale ein Volume, bei LibreChat ein tmpfs. Der Rauchtest läuft in der CI in beiden Varianten und blockiert bei einem Fehler.

## Offen

- Tag `v1.1.0`, Image veröffentlichen und auf „public“ stellen.
- Echter Lauf auf Exoscale und in LibreChat, Zeitmessung für 5 und 30 Minuten.
- Ein-Befehl-Skripte `up.sh` und `down.sh` mit der `exo`-CLI.
- Zertifikat ohne DNS-Wartezeit belegen (sslip.io oder IP-Zertifikat).
- Abschlussbericht nach Abschnitt 9 des Auftrags.
