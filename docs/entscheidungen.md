# Entscheidungen Serverbetrieb

Je Zeile: Frage, Entscheidung, Grund, Umkehr.

| # | Frage | Entscheidung | Grund | Umkehr |
|---|---|---|---|---|
| 1 | Wie startet das Image? | Abhängigkeiten beim Build ins venv, Start mit `python` statt `uv run` | Gemessen (DS218+): `uv run` schreibt beim Start in `/opt/uv-cache`, mit `read_only` startet der Container nicht (`Read-only file system`). `uv` war außerdem PID 1. | Dockerfile auf `uv run --locked --offline` zurückstellen, dann ohne `read_only` |
| 2 | Image-Aufbau | Zwei Stufen: Build mit uv, Laufzeit `python:3.12-slim-bookworm` ohne uv | 231 MB statt 289 MB, kein Werkzeug zur Laufzeit | Eine Stufe wie in 1.0.0 |
| 3 | Healthcheck im Image | HTTP auf `/healthz` statt TCP-Port | Zeigt, dass die Anwendung antwortet, nicht nur der Port offen ist | Alten TCP-Check einsetzen |
| 4 | DuckDB 2.0.0 auf alten x86-CPUs | Lockfile bleibt bei 2.0.0, Einschränkung im README dokumentiert | Gemessen: `duckdb 2.0.0` stürzt auf einem Intel Celeron J3355 (ohne AVX) mit `SIGILL` ab, 1.1.3 bis 1.5.6 laufen. Exoscale-CPUs und GitHub-Runner sind nicht betroffen. Auf der DS218+ wurde mit `duckdb<2` getestet (nur Testkontext, nicht im Repo). | Pin `duckdb>=1.1.0,<2` im Skript-Header und `uv lock --script` |
| 5 | `pids_limit` | In Compose gesetzt, auf der DS218+ wirkungslos | Der DSM-Kernel kennt die PID-Begrenzung nicht (Docker meldet "PIDs limit discarded"), nur Warnung | Zeile entfernen |
| 6 | Instanztyp & Paketupdates (Exoscale) | `standard.small` (2 GB RAM) als Standard in `up.sh`; `apt-get upgrade` + `unattended-upgrades` erst nach `first-boot.sh` | Reicht für `mem_limit: 1500m` (`DATAGVAT_MAX_PARALLEL=2` × 512 MB DuckDB) plus Caddy; Vollaktualisierung nach dem Start blockiert `/healthz` nicht | `--instance-type standard.medium` übergeben bzw. Reihenfolge in `cloud-init.yaml` ändern |
