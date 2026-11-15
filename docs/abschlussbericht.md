# Abschlussbericht Serverbetrieb (Version 1.1.0)

## 1. Ergebnis in einem Satz
Server-Code, zweistufiges gehärtetes Container-Image, Release-Workflow, Ein-Befehl-Skripte (`deploy/exoscale/up.sh` und `deploy/librechat/install.sh`) sowie alle Offline-, Rauch- und Dateitests sind umgesetzt; die Live-Zeitmessungen auf Exoscale und in einer echten LibreChat-Instanz (300 s / 1800 s) sowie Tag `v1.1.0` stehen noch aus, weil `exo`-CLI, laufender Docker-Daemon lokal und Exoscale-/LibreChat-Zielsysteme in der Arbeitsumgebung fehlten.

## 2. Die zwei Befehle (Kopiervorlage)
Voraussetzung: Nach Merge in `main` Tag `v1.1.0` setzen (`release.yml` baut `ghcr.io/daralph/datagvat-mcp:1.1`), Paket in GitHub auf *public* stellen.

- **Weg A (Exoscale, Wien):** Vorausgesetzt `exo` (`exo config`, SSH-Schlüssel registriert), `jq`, `curl`:
  ```bash
  ./deploy/exoscale/up.sh
  ```
  Rückbau: `./deploy/exoscale/down.sh`
- **Weg B (bestehende LibreChat-Installation, im LibreChat-Ordner):**
  ```bash
  bash <Pfad zum Repo>/deploy/librechat/install.sh
  ```
  Prüfen ohne Änderung: `--dry-run`; Rückbau: `--uninstall`

## 3. Zeit je Schritt
- **Gemessene Teilwerte (DS218+, Celeron J3355, Docker 24.0.2, 15.11.2026):** Image-Größe 231 MB (statt 289 MB in 1.0.0), `/healthz`-Bereitschaft nach 9–16 s, geordneter Stop nach 4 s (Exit 0, vorher 7 s unter `uv`).
- **Offline-Tests (Mac, 15.11.2026):** `mcp 1.x` 240/240 in 3,93 s, `mcp 2.x` 240/240 in 4,15 s, `deploy/librechat/test-install.sh` bestanden.
- **Gesamtzeiten Weg A und Weg B (3×300 s / 1800 s):** noch nicht gemessen (siehe Abschnitt 6 und 8).

## 4. Entscheidungen
Vollständige Tabelle: `docs/entscheidungen.md` (6 Einträge). Die drei folgenreichsten:
1. **Start direkt über `python` im venv statt `uv run` (`#1`):** `uv run --offline` schreibt beim Start nach `/opt/uv-cache` und scheitert bei `read_only: true`. Mit venv + `python` als PID 1 funktioniert `read_only: true`, und `SIGTERM` räumt sofort mit Exit 0 auf.
2. **Zweistufiges Image `python:3.12-slim-bookworm` (`#2`):** 231 MB statt 289 MB ohne `uv` zur Laufzeit.
3. **DuckDB 2.0.0 im Lockfile belassen (`#4`):** `duckdb 2.0.0` löst auf CPUs ohne AVX (Celeron J3355 / DS218+) `SIGILL` aus; Exoscale-VMs und GitHub-Runner haben AVX, die Einschränkung steht im README.

## 5. Lücken L1 bis L13
| # | Lücke | Status | Beleg |
|---|---|---|---|
| L1 | Veröffentlichtes Image | umgesetzt, Tag offen | `.github/workflows/release.yml`, `Dockerfile` (CI `docker` grün) |
| L2 | Eigenständiges Compose | behoben | `deploy/exoscale/compose.yml` (`docker compose config` grün) |
| L3 | TLS | umgesetzt, live ungetestet | `deploy/exoscale/Caddyfile` (`caddy validate` grün) |
| L4 | Hostname ohne DNS-Wartezeit | umgesetzt, CA-Limit ungetestet | `deploy/exoscale/first-boot.sh` (`<ip>.sslip.io`), eigener `--domain`-Parameter |
| L5 | LibreChat-Einrichtung | behoben (Dateien), live ungetestet | `deploy/librechat/install.sh`, `test-install.sh` (6 Fälle grün) |
| L6 | Token-Erzeugung | behoben | `first-boot.sh` / `install.sh` erzeugen 32-Byte-Hex-Token erst am Ziel |
| L7 | Healthcheck ohne Token | behoben | `datagvat_mcp.py` (`GET`/`HEAD /healthz`), `tests/test_server.py` |
| L8 | Mehrere Tokens | behoben | `MCP_AUTH_TOKEN` (Komma-Liste) + `MCP_AUTH_TOKEN_FILE`, `tests/test_server.py` |
| L9 | Host-Header hinter Proxy | bestätigt & behoben | Bind `127.0.0.1` ergab 421/403; `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` |
| L10 | Streamable-HTTP-Zustand | bestätigt & behoben | Sitzungen lagen im RAM; `MCP_STATELESS=1` ergänzt |
| L11 | PID 1 / SIGTERM-Aufräumen | bestätigt & behoben | `uvicorn` schluckte `atexit`; eigener SIGTERM-Handler + `python` als PID 1 |
| L12 | Container-Härtung | behoben | `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, `init`, Limits (`smoke-test.sh`) |
| L13 | Offener Abruf-Proxy | behoben | `DATAGVAT_ALLOWED_HOSTS` (Suffix-Prüfung je Weiterleitung) + Cloud-Metadaten-SSRF-Tests |

## 6. Getestet und nicht getestet
- **Getestet (15.11.2026, macOS + GitHub Actions `ubuntu-latest`):**
  - 240 Offline-Tests unter `mcp 1.x` und `mcp 2.x` inkl. Healthcheck, Multi-Token, Host-/Origin-Schutz, `MCP_STATELESS=1`, `DATAGVAT_ALLOWED_HOSTS` und Cloud-Metadaten-SSRF (`169.254.169.254`, `fe80::1`, `fd00:ec2::254`).
  - `deploy/librechat/test-install.sh` (6 Fälle: Einrueckung/Kommentare, Wiederholung, `--dry-run`, `--uninstall`, Abbruch bei Sonderfällen, vorhandenes Token).
  - CI-Job `docker` (auf `dad6744`): `docker build`, `deploy/smoke-test.sh` mit `/tmp`-Volume und `tmpfs`, `docker compose config`, `caddy validate`.
  - Hardware-Teillauf (15.11.2026, Synology DS218+): Image-Größe, `/healthz`-Bereitschaft, `SIGTERM`, `read_only: true`, DuckDB-AVX-Befund (`docs/serverbetrieb-analyse.md`).
- **Nicht getestet:**
  - `deploy/exoscale/up.sh` und `down.sh` gegen Exoscale (`exo`-CLI in der Arbeitsumgebung nicht installiert; bisher nur `bash -n`).
  - Echtes Let's-Encrypt-Zertifikat durch Caddy (insbesondere `sslip.io`-Ausstellungslimits) und Anbindung aus `claude.ai`.
  - Echte LibreChat-Instanz (`docker compose up` mit `api` + `mcp-datagvat`, Reload und Chat-Aufruf).
  - Stoppuhr-Läufe (3×300 s, 1800 s von null) und veröffentlichtes GHCR-Image via Tag `v1.1.0`.

## 7. Angelegte und entfernte Ressourcen
- In diesem Abschlussstand lokal keine Exoscale- oder Docker-Ressourcen angelegt.
- Hinweis aus dem Vormesslauf (15.11.2026, DS218+): Test-Reste (`~/dgvtest` und Images `dgvtest/datagvat-mcp:*`) noch manuell bzw. nach Freigabe auf der DS218+ bereinigen.

## 8. Nächste Schritte zum Abschluss der Messungen
1. PR `feature/serverbetrieb-m2` → `main` erstellen, CI abwarten, mergen und Tag `v1.1.0` pushen (veröffentlicht `ghcr.io/daralph/datagvat-mcp:1.1`, danach in GitHub auf *public* setzen).
2. Auf einer Umgebung mit `exo`-CLI `./deploy/exoscale/up.sh` 3× mit Stoppuhr messen und je Lauf mit `./deploy/exoscale/down.sh` aufräumen.
3. Auf einer frischen LibreChat-Compose-Testumgebung `bash <Pfad>/deploy/librechat/install.sh` 3× messen und `--uninstall` gegen `git status` prüfen.
