# datagvat-mcp

MCP-Server (Version 1.0.0) für den österreichischen Open-Data-Katalog [data.gv.at](https://www.data.gv.at/) (Piveau hub-search).
Lädt CSV-/JSON-/Parquet-Distributionen sowie ZIP-Archive und Excel-Dateien (`.xlsx`/`.xls`) und wertet sie mit DuckDB aus (Tabelle `dataset`, In-Memory, ohne Datei-/Netzzugriff).

## Tools
| Tool | Zweck |
|---|---|
| `search_datasets` | Katalogsuche mit Filtern `publisher`, `format`, `category`, `loadable_only`, `sort`; liefert Titel, Publisher, Formate, `has_loadable_data` und eine Facetten-Übersicht |
| `get_dataset_details` | Distributionen mit `url`, `format`, `license`, `byte_size`, sortiert nach Eignung (`rank`, `role`, `usable`, `note`) plus `recommended_url`; `lookup_tables` (Codelisten, Name → URL) bei Statistik-Austria-Daten; mit `check_links=True` zusätzlich `link_status` je Distribution und `links_checked` (Links werden per Range-GET geprüft, tote Links sind nicht `usable`) |
| `inspect_distribution_schema` | Spalten, Typen, Zeilenzahl, 5 Beispielzeilen (höchstens 20 Spalten, Texte auf 80 Zeichen gekürzt, Hinweis `sample_note`); bei ZIP/Excel zusätzlich `source` (Archivdatei, Blätter) |
| `query_distribution_sql` | Genau ein SELECT/WITH/DESCRIBE/SUMMARIZE, max. 200 Zeilen, 30 s; mit `extra_tables` (Name → URL, höchstens 3) weitere Dateien als eigene Tabellen laden und per JOIN verbinden, z. B. Codelisten aus `lookup_tables` |

Ressource: `datagvat://dataset/{id}` (DCAT-AP JSON-LD).

## Lokal (Claude Code, stdio)
```
claude mcp add data-gv-at --scope user -- /opt/homebrew/bin/uv run --script "<Pfad>/datagvat_mcp.py"
```
Absoluter `uv`-Pfad nötig, Claude Code startet ohne Login-Shell-PATH.

## Betrieb über HTTP (Docker, LibreChat)
- Standard ist `stdio` (lokal, ohne Netzwerk). HTTP nur mit `MCP_TRANSPORT=streamable-http` (oder `sse`).
- **Authentifizierung:** Jede HTTP-Anfrage braucht `Authorization: Bearer <MCP_AUTH_TOKEN>` (mindestens 16 Zeichen, z. B. `openssl rand -hex 32`). Der Vergleich ist zeitkonstant, ohne oder mit falschem Token antwortet der Server mit 401.
- **Start-Schutz:** Auf einer Nicht-Loopback-Adresse (z. B. `MCP_HOST=0.0.0.0` im Container) startet der Server ohne Token nicht (Exit-Code 2). `MCP_ALLOW_NO_AUTH=1` hebt das bewusst auf, nur für abgeschottete Netze. Auf `127.0.0.1` ist kein Token nötig.
- Das Token ist ein einfaches gemeinsames Geheimnis, kein OAuth. Es schützt nicht vor einem Angreifer, der den Netzverkehr mitlesen kann: bei Zugriff über fremde Netze TLS davorschalten (Reverse Proxy).
- Docker: `DATAGVAT_MCP_TOKEN` in die `.env`, `docker-compose.librechat.yml` reicht es als `MCP_AUTH_TOKEN` durch, `librechat.mcp.example.yaml` setzt den Header. Der Healthcheck prüft nur den TCP-Port. Getestet (2026-10-14, Docker 29.8 auf macOS): Image-Build samt Selftest, Lauf als `nobody`, Healthcheck `healthy`, Start ohne Token verweigert (Exit 2), HTTP 401 ohne/mit falschem Token, Tool-Aufruf mit Token gegen data.gv.at, Compose-Datei mit Stub-Basis und Pflicht-Token. Nicht getestet: echte LibreChat-Instanz (`headers: Authorization`).
- **Wiederholungen:** Katalog-Anfragen und Downloads werden bei 429/502/503/504 und Netzwerkfehlern bis zu `DATAGVAT_RETRIES` (Standard 2) Mal wiederholt, mit Wartezeit 0,5 s, 1 s, … (höchstens 10 s). Ein `Retry-After` in Sekunden hat Vorrang. Andere Fehler (404, 403, 500) werden sofort gemeldet. Bei abgebrochenen Downloads beginnt der Versuch von vorn.

## LibreChat einrichten (Schritt für Schritt)
Voraussetzung: eine LibreChat-Installation per Docker Compose (Ordner mit `docker-compose.yml`, `.env` und `librechat.yaml`).

1. **Server-Ordner ablegen:** dieses Repo als Unterordner `datagvat-mcp` in den LibreChat-Ordner klonen:
   ```
   cd <LibreChat-Ordner>
   git clone https://github.com/daralph/datagvat-mcp.git datagvat-mcp
   ```
2. **Token erzeugen** und in die LibreChat-`.env` eintragen:
   ```
   echo "DATAGVAT_MCP_TOKEN=$(openssl rand -hex 32)" >> .env
   ```
   LibreChat (Dienst `api`) liest die `.env` ohnehin ein, der MCP-Container bekommt das Token über die Compose-Datei.
3. **Compose-Erweiterung:** `datagvat-mcp/docker-compose.librechat.yml` als `docker-compose.override.yml` in den LibreChat-Ordner kopieren. Gibt es dort schon eine Override-Datei, den Dienst `mcp-datagvat` und `depends_on` von Hand übernehmen.
   ```
   cp datagvat-mcp/docker-compose.librechat.yml docker-compose.override.yml
   ```
4. **MCP-Server anmelden:** den Block `mcpServers:` aus `datagvat-mcp/librechat.mcp.example.yaml` in die eigene `librechat.yaml` übernehmen (gibt es `mcpServers:` schon, nur den Eintrag `datagvat:` ergänzen). Die URL `http://mcp-datagvat:8000/mcp` passt zum Dienstnamen aus Schritt 3.
5. **Starten:**
   ```
   docker compose up -d --build
   docker compose ps                     # mcp-datagvat muss "healthy" sein
   docker compose logs mcp-datagvat      # bei Problemen
   ```
6. **Prüfen:** In LibreChat einen Chat öffnen, im Chat-Menü den MCP-Server `datagvat` aktivieren und z. B. fragen: „Welche Vornamen waren zuletzt in Wien am häufigsten?“

**Aktualisieren:** `cd datagvat-mcp && git pull && cd .. && docker compose up -d --build mcp-datagvat`.

**Typische Fehler:**
- `DATAGVAT_MCP_TOKEN in .env setzen`: Schritt 2 fehlt.
- Container startet nicht, Exit-Code 2: Token fehlt oder ist kürzer als 16 Zeichen.
- LibreChat meldet 401: In `librechat.yaml` fehlt der `headers`-Eintrag, oder `api` kennt die Variable nicht (`.env` im Dienst `api` eingebunden?).
- Zeitüberschreitung bei großen Dateien: `timeout` in `librechat.yaml` erhöhen (Vorgabe im Beispiel 150 s).

## Tests
```
# offline (mit mcp 2.x, wie der Server per uv aufgelöst wird)
uv run --no-project --with pytest --with pytest-asyncio --with "mcp[cli]>=1.30,<3" --with httpx --with duckdb --with python-calamine pytest
# gegen mcp 1.x
uv run --no-project --with pytest --with pytest-asyncio --with "mcp[cli]>=1.30,<2" --with httpx --with duckdb --with python-calamine pytest
# gegen die echte API
... pytest -m live
```
Die Tests laufen mit beiden mcp-Hauptversionen (201 offline, 4 live). Mit 2.x sind Attribute der Python-Clients snake_case (`is_error`), auf dem Draht bleibt es camelCase (`isError`).

## Sicherheit
- SSRF: Jede URL und jede Weiterleitung (max. 5) wird per DNS auf öffentliche IPs geprüft (`ipaddress.is_global`, zusätzlich NAT64 gesperrt). Die verbindliche Prüfung läuft beim Verbindungsaufbau: der Server löst den Host selbst auf, prüft alle Adressen und verbindet auf genau diese IP (TLS/SNI weiter über den Hostnamen). Damit ist DNS-Rebinding ausgeschlossen. Proxy-/netrc-Umgebung wird ignoriert (`trust_env=False`), Unix-Sockets sind gesperrt.
  **Restrisiko:** Das Pinning setzt auf httpx/httpcore-Interna (`_pool._network_backend`); bei einer inkompatiblen httpx-Version schlägt der erste Abruf mit einer Fehlermeldung fehl, statt ungeschützt zu laufen. Die Prüfung beim Verbinden gilt für alle Anfragen, auch für die Katalog-API und deren Weiterleitungen.
- DuckDB: Daten werden vor dem Sperren geladen, danach `enable_external_access=false` und `lock_configuration=true`. Nur eine Anweisung, nur lesend, 512 MB RAM, höchstens 1 GB Auslagerung auf Platte, 30 s Zeitlimit je Abfrage (Laden der Datei: 60 s). Ergebnis: höchstens 200 Zeilen, Zellen auf 2000 Zeichen gekürzt (`cells_shortened`), insgesamt höchstens 1 Mio. Zeichen.
- Downloads: Gesamtzeit höchstens 120 s (`DATAGVAT_DOWNLOAD_TIMEOUT`), höchstens 4 gleichzeitige Aufrufe mit Datei (`DATAGVAT_MAX_PARALLEL`). Excel: höchstens 5 Mio. Zellen im Blattbereich; bei `.xlsx` werden die Archivmitglieder mit eigenem Zähler neu gepackt, gefälschte Größenangaben im ZIP-Header fallen auf (Zip-Bomb-Schutz). ZIP-Archive mit mehr als 10.000 Einträgen werden abgelehnt, bevor das Inhaltsverzeichnis geladen wird. Temporäre Dateien liegen in einem eigenen Verzeichnis `datagvat-<PID>-…`, das beim Beenden entfernt wird; Reste abgestürzter Prozesse räumt der nächste Start weg. Fehlermeldungen enthalten keine lokalen Pfade.
- Download-Limit 50 MB (`DATAGVAT_MAX_BYTES`), entpackt 200 MB (`DATAGVAT_MAX_UNPACKED_BYTES`). PDF/HTML/XML werden mit klarer Meldung abgelehnt; ZIP (mit Auswahl per `archive_member`) und Excel (`.xlsx`/`.xls`, per `sheet`/`skip_rows`) werden geladen. `.xlsb`/`.ods` sind nicht unterstützt.
- HTTP-Betrieb: Bearer-Token ist Pflicht (siehe oben). Bei Zugriff über fremde Netze zusätzlich TLS davorschalten.

## Konfiguration (Umgebungsvariablen)
| Variable | Standard | Bedeutung |
|---|---|---|
| `DATAGVAT_TIMEOUT` | 20 | Timeout je HTTP-Anfrage (s) |
| `DATAGVAT_MAX_BYTES` | 52428800 (50 MB) | Download-Limit |
| `DATAGVAT_MAX_UNPACKED_BYTES` | 209715200 (200 MB) | Limit für entpackte Daten (ZIP/Excel) |
| `DATAGVAT_QUERY_TIMEOUT` | 30 | Zeitlimit je SQL-Abfrage (s) |
| `DATAGVAT_DOWNLOAD_TIMEOUT` | 120 | Gesamtzeitlimit eines Downloads (s) |
| `DATAGVAT_MAX_PARALLEL` | 4 | Gleichzeitige Aufrufe mit Datei |
| `DATAGVAT_CACHE_TTL` | 900 | Cache-Lebensdauer je URL (s), 0 = aus |
| `DATAGVAT_CACHE_MAX_BYTES` | 209715200 (200 MB) | Größe des Datei-Caches |
| `DATAGVAT_RETRIES` | 2 | Zusätzliche Versuche bei 429/502/503/504 und Netzfehlern |
| `DATAGVAT_LINK_CHECK_TIMEOUT` | 8 | Timeout je Link-Prüfung bei `check_links` (s); Gesamtlimit je Link das Doppelte |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `streamable-http` oder `sse` |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | Bind-Adresse (nur HTTP) |
| `MCP_AUTH_TOKEN` | – | Bearer-Token (HTTP, mind. 16 Zeichen) |
| `MCP_ALLOW_NO_AUTH` | – | `1` erlaubt Nicht-Loopback ohne Token |

Abhängigkeiten sind in `datagvat_mcp.py.lock` festgeschrieben (`uv lock --script datagvat_mcp.py`). Das Docker-Image baut mit `--locked` und startet `--offline`, also genau mit diesen Versionen. Aktualisieren: `uv lock --script --upgrade datagvat_mcp.py`, danach Tests laufen lassen (wegen des IP-Pinnings besonders bei httpx/httpcore). CI (`.github/workflows/tests.yml`) testet bei jedem Push gegen mcp 1.x und 2.x und baut das Image, wöchentlich zusätzlich gegen die echte API.

## Bekannte Eigenheiten der Daten
- Statistik Austria liefert pro Datensatz `*_HEADER.csv` (Metadaten) neben der Datendatei und Dimensionsdateien `*_C-…csv`. Die Datendatei ist `…_1.csv` ohne Zusatz.
- Das Feld `license` fehlt bei manchen Publishern im Katalog (z. B. Statistik Austria). Dann nicht raten, sondern „im Katalog nicht angegeben“ nennen.
- **mcp-Versionen:** Der Server läuft mit mcp 1.x (`FastMCP`) und 2.x (`MCPServer`). `uv` löst zurzeit auf 2.3 auf, getestet auch mit Claude Code (stdio) und per HTTP. Untergrenze 1.30, weil Annotations, strukturierte Ausgaben und `CallToolResult` dort getestet sind. Die Obergrenze `<3` verhindert eine ungeprüfte nächste Hauptversion.
- Im Katalog gibt es praktisch kein Parquet (0 von 74.166 Datensätzen), Excel ist selten (173): CSV und ZIP sind die Praxisformate.

## Lizenz
Der Code steht unter der [MIT-Lizenz](LICENSE). Die Lizenz der Daten gilt unabhängig davon je Datensatz (Feld `license` in `get_dataset_details`, oft CC BY 4.0; bei manchen Publishern im Katalog nicht angegeben).
