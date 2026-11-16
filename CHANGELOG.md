# Changelog

## 1.1.0 (2026-11-16)
- HTTP-Betrieb: `GET /healthz` ohne Token (nur Status und Version), alle anderen Pfade bleiben geschützt.
- Mehrere Tokens: `MCP_AUTH_TOKEN` als Komma-Liste, zusätzlich `MCP_AUTH_TOKEN_FILE` (eine Zeile je Token). Ein Komma im Token trennt jetzt.
- `MCP_ALLOWED_HOSTS` und `MCP_ALLOWED_ORIGINS`: Host-/Origin-Prüfung mit eigenen Hostnamen, z. B. hinter einem Reverse Proxy.
- `MCP_STATELESS=1`: zustandsloser Streamable-HTTP-Betrieb, übersteht Neustarts ohne Sitzungsverlust.
- `DATAGVAT_ALLOWED_HOSTS`: optionale Liste erlaubter Abruf-Ziele (Suffix-Abgleich), gilt für jede Weiterleitung.
- SIGTERM beendet den HTTP-Server mit Exit-Code 0 und räumt Temp-Verzeichnis und Cache auf (vorher Exit -15 ohne Aufräumen).
- Temp-Reste eines Vorgängers mit derselben PID werden beim Start entfernt (Container-Neustart).
- Serverbetrieb: `deploy/` mit Compose und Caddy für eine VM, cloud-init, `up.sh`/`down.sh` für Exoscale, LibreChat-Installer, Rauchtest; `release.yml` veröffentlicht das Image nach GHCR.
- Docker-Image zweistufig: Abhängigkeiten beim Build nach Lockfile in ein venv, Start mit `python` als PID 1 statt `uv run`. Läuft damit auch mit schreibgeschütztem Dateisystem (`read_only`), das in beiden Compose-Dateien gesetzt ist.
- Docker-Healthcheck prüft `/healthz` statt nur den TCP-Port.
- 240 Offline-Tests (inkl. Cloud-Metadaten-SSRF).

## 1.0.0 (2026-10-30)
- `query_distribution_sql`: `extra_tables` lädt bis zu 3 weitere Dateien als eigene Tabellen, z. B. Codelisten zum JOIN mit `dataset`. Alle Tabellen werden vor dem Sperren der Sandbox geladen.
- `get_dataset_details`: `lookup_tables` (Codelisten als Name → URL); `check_links=True` prüft die Links vorab per Range-Anfrage (SSRF-Prüfung je Weiterleitung), tote Links und Weiterleitungen auf HTML-Seiten werden nicht mehr empfohlen.
- Fehlermeldungen zeigen URLs wieder vollständig (vorher als `https:/<Datei>` maskiert); lokale Pfade bleiben verborgen.
- Empfehlungstext unterscheidet zwischen anderen Jahrgängen und anderen Varianten einer Datei.
- Abhängigkeiten per `datagvat_mcp.py.lock` festgeschrieben; Docker baut mit `--locked`, startet `--offline`.
- CI über GitHub Actions (mcp 1.x und 2.x, Docker-Build, wöchentlich Live-Tests).
- 201 Offline-Tests, 4 Live-Tests.

## 0.4.0 (2026-10-14)
- Erste Version: Suche, Details, Schema, SQL; CSV/JSON/Parquet/Excel/ZIP; SSRF-Schutz mit IP-Pinning; Bearer-Token für HTTP.
