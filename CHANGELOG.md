# Changelog

## 1.0.0 (2026-10-30)
- `query_distribution_sql`: `extra_tables` lädt bis zu 3 weitere Dateien als eigene Tabellen, z. B. Codelisten zum JOIN mit `dataset`. Alle Tabellen werden vor dem Sperren der Sandbox geladen.
- `get_dataset_details`: `lookup_tables` (Codelisten als Name → URL); `check_links=True` prüft die Links vorab per Range-Anfrage (SSRF-Prüfung je Weiterleitung), tote Links und Weiterleitungen auf HTML-Seiten werden nicht mehr empfohlen.
- Fehlermeldungen zeigen URLs wieder vollständig (vorher als `https:/<Datei>` maskiert); lokale Pfade bleiben verborgen.
- Empfehlungstext unterscheidet zwischen anderen Jahrgängen und anderen Varianten einer Datei.
- Abhängigkeiten per `datagvat_mcp.py.lock` festgeschrieben; Docker baut mit `--locked`, startet `--offline`.
- CI über GitHub Actions (mcp 1.x und 2.x, Docker-Build, wöchentlich Live-Tests).
- 199 Offline-Tests, 4 Live-Tests.

## 0.4.0 (2026-10-14)
- Erste Version: Suche, Details, Schema, SQL; CSV/JSON/Parquet/Excel/ZIP; SSRF-Schutz mit IP-Pinning; Bearer-Token für HTTP.
