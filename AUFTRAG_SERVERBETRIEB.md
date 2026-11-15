# Auftrag: datagvat-mcp in 5 Minuten auf Exoscale oder in LibreChat

Auftragsdatei für Coding-Agenten, Fassung 2. Ins Repo-Root von `daralph/datagvat-mcp` legen und den Agenten starten mit:

> Lies `AUFTRAG_SERVERBETRIEB.md` vollständig, arbeite alle Phasen selbständig ab und melde dich erst mit dem Abschlussbericht.

Stand der Analyse: Commit vom 31.10.2026 (Version 1.0.0). Weicht das Repo davon ab, gilt das Repo, und die Abweichung wird in Phase 0 festgehalten.

---

## 1. Ziel

Der MCP-Server läuft heute lokal am Mac (stdio) und als Zusatzdienst in einem LibreChat-Compose-Verbund, dort mit Handarbeit in sechs Schritten und einem Image-Build vor Ort. Er soll auf zwei Wegen **in höchstens 5 Minuten** laufen:

- **Weg A, Exoscale:** frische Compute-Instanz in Wien, Docker Compose, öffentlich über HTTPS, nutzbar als Custom Connector in claude.ai und in Claude Code.
- **Weg B, LibreChat:** bestehende LibreChat-Installation per Docker Compose, Server im internen Netz, in LibreChat als MCP-Server sichtbar.

### So wird gemessen

- **Start der Uhr:** Enter auf dem einen Befehl des jeweiligen Weges.
- **Ende der Uhr:** Ein MCP-`initialize` mit Token wird über die endgültige Adresse erfolgreich beantwortet. Bei Weg A über `https://` mit gültigem Zertifikat, bei Weg B aus dem LibreChat-Container `api` heraus, und LibreChat hat den Server geladen.
- **Grenze Schnellstart:** 300 Sekunden im schlechtesten von drei Läufen.
- **Handgriffe:** höchstens ein Befehl je Weg, keine Datei von Hand bearbeiten.

Außerhalb dieser Uhr liegen nur Dinge, die man einmalig hat: Exoscale-Konto mit eingerichteter `exo`-CLI und SSH-Schlüssel bei Weg A, eine laufende LibreChat-Installation bei Weg B.

### Neues Projekt von null

Wer noch nichts eingerichtet hat, kommt in **unter 30 Minuten** zum laufenden Dienst. Hier zählen die einmaligen Voraussetzungen mit: CLI installieren und anmelden, SSH-Schlüssel hinterlegen, bei Weg B eine LibreChat-Installation aufsetzen. Die Uhr läuft vom ersten Schritt der Anleitung bis zum erfolgreichen `initialize`.

### Wenn eine Grenze nicht realistisch ist

Beide Grenzen gelten, solange sie realistisch sind. Kommen die Sub-Agenten nach Messung oder begründeter Schätzung zum Ergebnis, dass eine Grenze nicht erreichbar ist, setzt der Agent sie auf den Schätzwert der Sub-Agenten. Alter Wert, neuer Wert und Begründung stehen in `docs/entscheidungen.md`, gemessen wird gegen den neuen Wert. Gelockert wird nur die Zeit, nie Regel 8 (Sicherheit).

## 2. Nicht-Ziele

- Kein Umbau der Tools, der Katalog-Logik oder der DuckDB-Sandbox.
- Kein Kubernetes (Exoscale SKS), kein Load Balancer, kein Mehrinstanz-Betrieb.
- Kein OAuth in diesem Auftrag (siehe Abschnitt 10, geparkt).

## 3. Ausgangslage (aus dem Code gelesen)

Bereits vorhanden und zu erhalten:

- Eine Datei `datagvat_mcp.py` als uv-Script mit Lockfile `datagvat_mcp.py.lock`.
- Transport per `MCP_TRANSPORT`: `stdio` (Standard), `streamable-http`, `sse`.
- `_BearerAuthMiddleware` mit zeitkonstantem Vergleich, Mindestlänge 16 Zeichen, Startverweigerung (Exit 2) ohne Token auf Nicht-Loopback-Adressen.
- `Dockerfile`: Lockfile-treuer Build mit Selftest, Start `--offline`, Lauf als `nobody`, Healthcheck über TCP-Port.
- SSRF-Schutz mit IP-Pinning, `trust_env=False`, Limits für Download, Entpacken, RAM und Laufzeit.
- CI: Offline-Tests gegen mcp 1.x und 2.x, Docker-Build, wöchentliche Live-Tests.

Lücken, geordnet nach Wirkung auf das 5-Minuten-Ziel:

| # | Lücke | Folge |
|---|---|---|
| L1 | Kein veröffentlichtes Image, jeder Server baut selbst | Der Build mit Abhängigkeiten frisst den Großteil des Zeitbudgets |
| L2 | Kein eigenständiges Compose, nur die Override-Datei für LibreChat | Weg A existiert nicht |
| L3 | Kein TLS, das README verweist nur auf „Reverse Proxy davorschalten“ | Token ginge im Klartext übers Netz |
| L4 | Zertifikat braucht einen Hostnamen, ein DNS-Eintrag braucht Wartezeit | DNS liegt außerhalb der Kontrolle und sprengt 5 Minuten |
| L5 | Einrichtung in LibreChat sind sechs Handschritte in drei Dateien | Weg B ist langsam und fehleranfällig |
| L6 | Token wird von Hand erzeugt und eingetragen | Handarbeit, Tippfehler |
| L7 | Die Auth-Middleware schützt jede Route, Healthcheck prüft nur den TCP-Port | Kein HTTP-Signal „bereit“ für Skripte, Proxy und Monitoring |
| L8 | Ein einziges Token, nur über Umgebungsvariable | Kein Wechsel ohne Ausfall, keine Trennung nach Client |
| L9 | Host-Header-Prüfung des MCP-SDK hinter einem Proxy ist ungeklärt | Anfragen mit öffentlichem Hostnamen könnten abgewiesen werden |
| L10 | Sitzungszustand des Streamable-HTTP-Transports ist ungeklärt | Clients könnten nach einem Container-Neustart hängen |
| L11 | `uv run` ist PID 1 im Container | Signalweitergabe und Aufräumen bei `docker stop` ungeprüft |
| L12 | Keine Container-Härtung (Dateisystem, Capabilities, Limits) | Unnötige Angriffsfläche |
| L13 | Der Server lädt jede öffentliche URL | Mit geleaktem Token wird er zum offenen Abruf-Proxy |

L9, L10 und L11 sind **Vermutungen**. Sie werden in Phase 0 zuerst geprüft und erst dann behoben.

## 4. Arbeitsregeln für den Agenten

1. **Erst Ursache, dann Änderung.** Zu jeder Lücke zuerst belegen, dass sie besteht (Code-Stelle, Testlauf oder Doku-Zitat mit Link), dann beheben. Was sich nicht belegen lässt, wird nicht „vorsorglich“ umgebaut.
2. **Der Agent trifft alle Entscheidungen.** Ralph hat allen hypothetischen und künftig echten Anfragen des Agenten und seiner Sub-Agenten vorab zugestimmt. Daraus folgt:
   - Keine Rückfrage an Ralph, kein Warten auf Freigabe, kein Zwischenbericht.
   - Sub-Agenten richten ihre Fragen an den Hauptagenten. Der entscheidet sofort und gibt die Entscheidung weiter. Diese Datei gilt für jeden Sub-Agenten vollständig.
   - Die Vorgaben in Abschnitt 8 sind Empfehlungen. Der Agent darf abweichen, wenn Messung oder Beleg dagegen sprechen.
   - Jede Entscheidung kommt mit einer Zeile in `docs/entscheidungen.md`: Frage, Entscheidung, Grund, wie sie sich umkehren lässt.
3. **Wirkungsbereich.** Die Zustimmung gilt für dieses Repo samt `main`, Tags und Image-Veröffentlichung sowie für Testumgebungen, die der Agent selbst anlegt: lokal, und auf Exoscale, sofern die `exo`-CLI in seiner Umgebung eingerichtet ist. Selbst angelegte Ressourcen entfernt er am Ende wieder und nennt sie im Bericht mit Laufzeit und Kosten. Bestehende Server, andere Repos und die produktive LibreChat-Instanz der Organisation fasst er nicht an. Weg B testet er an einer eigenen, frisch aufgesetzten LibreChat-Installation.
4. **Vollständiger, lauffähiger Code.** Keine Platzhalter, keine „hier ergänzen“-Stellen. Jede neue Datei funktioniert so, wie sie ist.
5. **Quellen offenlegen.** Aussagen über Exoscale, das MCP-SDK, Caddy, LibreChat und claude.ai stammen aus der aktuellen offiziellen Doku oder aus dem installierten Paketcode, mit Link oder Fundstelle. Im README die bestehende Form übernehmen: „Getestet (Datum, Umgebung)“ und „Nicht getestet“.
6. **Zeiten werden gemessen, nicht geschätzt.** Jede Zeitangabe im Bericht stammt aus einem protokollierten Lauf. Wird das Ziel verfehlt, steht dort die gemessene Ursache.
7. **Standardverhalten bleibt.** Ohne neue Variablen verhält sich der Server wie Version 1.0.0. stdio am Mac ändert sich nicht.
8. **Sicherheit wird nicht gelockert.** SSRF-Prüfung, IP-Pinning, Sandbox-Limits und Startschutz bleiben. `MCP_ALLOW_NO_AUTH` kommt in keinem Beispiel und keinem Skript vor. Tempo ist kein Grund, TLS oder Token wegzulassen.
9. **Keine Geheimnisse im Repo, im Log oder im Bericht.** Tokens entstehen auf dem Zielsystem und bleiben dort. Im Repo steht nur `.env.example` mit Leerwerten.
10. **Eine Datei bleibt eine Datei.** `datagvat_mcp.py` wird nicht in ein Paket zerlegt. Neue Abhängigkeiten nur mit Begründung, danach `uv lock --script datagvat_mcp.py`.
11. **Beide SDK-Hauptversionen.** Jede Code-Änderung läuft mit mcp 1.x und 2.x, wie die CI-Matrix es verlangt.
12. **Stil übernehmen.** Code-Kommentare deutsch in ASCII-Umschrift (ae, ue), README und Changelog mit echten Umlauten, knappe Sätze.
13. **Arbeitsweise.** Branch `feature/serverbetrieb`, kleine Commits je Lücke, Pull Request. Nach grüner CI führt der Agent selbst zusammen und setzt den Tag `v1.1.0`, weil das veröffentlichte Image Voraussetzung für die Zeitmessung ist.

## 5. Phasen

### Phase 0: Analyse und Ausgangsmessung (keine Code-Änderung)

Ergebnis ist `docs/serverbetrieb-analyse.md` mit:

- Abgleich der Tabelle in Abschnitt 3 mit dem aktuellen Repo.
- **Ausgangszeit:** Wie lange dauert Weg B heute nach README, Schritt für Schritt gestoppt? Wie groß ist das Image, wie lange dauern Build und Pull?
- **L9:** Wie prüfen mcp 1.x und 2.x den Host-Header? Test: Server mit `MCP_HOST=0.0.0.0` starten, Anfrage mit `Host: mcp.example.org` senden, Statuscode festhalten, für beide SDK-Versionen.
- **L10:** Hält der Streamable-HTTP-Transport Sitzungen im Speicher? Gibt es in beiden SDK-Versionen einen zustandslosen Modus, und was kostet er?
- **L11:** `docker stop` messen. Endet der Container in der Frist, verschwindet das Temp-Verzeichnis `datagvat-<PID>-…`?
- **L4:** Welche Wege zu einem gültigen Zertifikat ohne DNS-Wartezeit gibt es heute? Zu prüfen sind mindestens ein Hostname aus der IP über einen Wildcard-DNS-Dienst samt Ausstellungslimits der Zertifizierungsstelle, ein Zertifikat direkt auf die IP-Adresse, und eine vorab reservierte feste IP mit bestehendem DNS-Eintrag.
- Aktuelle Exoscale-Angaben mit Link: Zonen in Wien, passender Instanztyp, Vorlagen mit vorinstalliertem Docker, Security Groups, cloud-init, IPv6, Startzeit einer Instanz.
- Aktuelle LibreChat-Angaben mit Link: Wie werden MCP-Server in `librechat.yaml` eingetragen, welcher Dienst muss neu starten, wie lässt sich das Laden prüfen?
- Aktuelle claude.ai-Angaben mit Link: Custom Connector mit festem Request-Header, erwarteter URL-Pfad, Quell-IP-Bereiche von Anthropic.
- **Zeitbudget** je Weg als Tabelle mit Soll je Schritt. Die Richtwerte unten sind ungeprüfte Annahmen und werden durch Messwerte ersetzt.

| Schritt Weg A | Richtwert | Schritt Weg B | Richtwert |
|---|---|---|---|
| Instanz anlegen bis cloud-init läuft | 60 s | Voraussetzungen prüfen, Token, Dateien | 10 s |
| Docker bereitstellen | 75 s | Image ziehen | 45 s |
| Images ziehen | 45 s | MCP-Container starten bis gesund | 30 s |
| Start und Zertifikat | 40 s | LibreChat-Dienst neu laden | 60 s |
| Prüfung `initialize` | 10 s | Prüfung `initialize` und Ladebeleg | 15 s |
| **Summe** | **230 s** | **Summe** | **160 s** |

### Phase 1: Server-Code (`datagvat_mcp.py`)

- **Healthcheck (L7).** `GET /healthz` ohne Authentifizierung, Antwort `200` mit `{"status":"ok","version":"…"}` und sonst nichts. Alle anderen Pfade bleiben geschützt. Die Ausnahme greift exakt für Methode und Pfad, nicht per Präfix.
- **Tokens (L8).** `MCP_AUTH_TOKEN` nimmt mehrere, durch Komma getrennte Tokens. Zusätzlich `MCP_AUTH_TOKEN_FILE` (eine Zeile je Token, Leerzeilen und `#`-Kommentare erlaubt). Jedes Token mindestens 16 Zeichen, Vergleich zeitkonstant gegen alle Tokens ohne vorzeitigen Abbruch. Tokens erscheinen nie im Log.
- **Host-Header (L9),** nur falls bestätigt. `MCP_ALLOWED_HOSTS` (Komma-Liste) reicht die erlaubten Hostnamen an das SDK durch. Der Schutz wird erweitert, nicht abgeschaltet.
- **Sitzungen (L10),** nur falls empfohlen. `MCP_STATELESS=1` schaltet den zustandslosen Betrieb ein, Standard bleibt wie bisher.
- **Abruf-Ziele (L13).** Optional `DATAGVAT_ALLOWED_HOSTS` (Komma-Liste mit Suffix-Abgleich, z. B. `gv.at,statistik.at`). Leer bedeutet wie bisher alle öffentlichen Hosts. Die Prüfung gilt für jede Weiterleitung und läuft zusätzlich zur SSRF-Prüfung. Die Katalog-API ist immer erlaubt.
- **Signale (L11),** nur falls bestätigt. Sauberes Beenden bei SIGTERM mit Aufräumen.
- **Tests** zu jedem Punkt in `tests/test_server.py`: Healthcheck ohne Token 200, geschützter Pfad ohne Token 401, zweites Token gültig, zu kurzes Token in Liste oder Datei verweigert den Start, gesperrter Abruf-Host liefert eine klare Fehlermeldung.

### Phase 2: Image und CI (L1)

- Workflow `release.yml`: bei Tag `v*` Image für `linux/amd64` und `linux/arm64` bauen und öffentlich nach `ghcr.io/daralph/datagvat-mcp` schieben, Marken `X.Y.Z` und `X.Y`. In Betriebsdateien nie `latest`.
- Image-Größe und Pull-Zeit messen. Liegt der Pull über dem Budget, die Ursache benennen (Schicht für Schicht) und gezielt verkleinern.
- Docker-Job in `tests.yml` um einen Rauchtest erweitern: Container mit Test-Token starten, dann Healthcheck, 401 und `initialize` wie in Abschnitt 6.
- Die Compose-Dateien aus Phase 3 und 4 werden in der CI mit `docker compose config` geprüft.

### Phase 3: Weg A, Exoscale (`deploy/exoscale/`)

- `compose.yml` mit zwei Diensten:
  - `mcp`: Image aus der Registry mit fester Versionsmarke. Kein veröffentlichter Port, nur internes Netz. Härtung (L12): `read_only: true`, begrenztes `tmpfs` oder Volume für das Temp-Verzeichnis (Auslagerung bis 1 GB plus Datei-Cache 200 MB beachten), `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `mem_limit`, `pids_limit`, `init: true`, `restart: unless-stopped`, Log-Rotation. Healthcheck über `/healthz`. Prüfen, ob `uv run --offline` mit schreibgeschütztem Dateisystem startet, sonst die Ursache nennen und gezielt lösen.
  - `caddy`: Ports 80 und 443, automatisches Zertifikat für den Hostnamen aus Phase 0, Weiterleitung an `mcp:8000`. Antworten nicht puffern, Zeitlimits über 150 s, Größenlimit für Anfragen, Zugriffslog ohne `Authorization`-Header, Zertifikate in einem Volume.
- `cloud-init.yaml`: bringt die Instanz ohne Login in den Betrieb. Docker bereitstellen, Dateien nach `/opt/datagvat-mcp` schreiben, Token beim ersten Start **auf der Instanz** erzeugen und mit Rechten 600 ablegen, Dienste starten. Paket-Vollaktualisierung nicht vor dem Dienststart, sondern danach, dazu unbeaufsichtigte Sicherheitsupdates. Das Token steht weder in den User-Daten noch im cloud-init-Log.
- `up.sh`: der eine Befehl. Legt Security Group (443 und 80 offen, 22 nur von der eigenen IP) und Instanz in Wien an, wartet auf `/healthz`, prüft `initialize`, gibt am Ende URL, Token-Fundort und die gestoppte Zeit je Schritt aus. Wiederholbar ohne Doppelanlage. Optionaler Parameter für eine eigene Domain mit bestehendem DNS-Eintrag.
- `down.sh`: entfernt alles, was `up.sh` angelegt hat, und nur das.
- `README.md`: Voraussetzungen, der eine Befehl, Einbindung in claude.ai (URL `https://<Host>/mcp`, Request-Header `Authorization` mit Wert `Bearer <Token>`) und Claude Code, Update, Token-Wechsel ohne Ausfall, Rückbau, begründete Wahl des Instanztyps aus den Limits des Servers.

### Phase 4: Weg B, LibreChat (`deploy/librechat/`)

- `install.sh`: der eine Befehl, ausgeführt im LibreChat-Ordner. Prüft Voraussetzungen, erzeugt das Token in `.env` falls es fehlt, bindet den Dienst `mcp-datagvat` mit dem veröffentlichten Image ein, ergänzt den Eintrag in `librechat.yaml`, startet, lädt LibreChat neu, prüft `initialize` und den Ladebeleg, gibt die gestoppte Zeit je Schritt aus.
- Anforderungen an das Skript: wiederholbar ohne Schaden, bestehende `docker-compose.override.yml` und bestehende `mcpServers`-Einträge bleiben erhalten (Kommentare und Reihenfolge inklusive), vor jeder Änderung eine Sicherungskopie mit Zeitstempel, `--dry-run` zeigt die Änderungen ohne sie auszuführen, `--uninstall` nimmt genau die eigenen Änderungen zurück. Bei einem Zustand, den es nicht sicher zusammenführen kann, bricht es ohne Änderung ab und nennt die eine Zeile, die fehlt.
- Die bestehenden Dateien `docker-compose.librechat.yml` und `librechat.mcp.example.yaml` bleiben als Handweg nutzbar und verweisen auf das Skript.

### Phase 5: Dokumentation und Abschluss

- README: Abschnitt „In 5 Minuten“ ganz oben mit je einem Befehl für Weg A und Weg B, darunter der bisherige Inhalt. Variablen-Tabelle ergänzt, Abschnitt „Sicherheit“ um das Bedrohungsbild aus Abschnitt 7 erweitert.
- `CHANGELOG.md` mit Version 1.1.0, `__version__` angepasst.
- Abschlussbericht nach Abschnitt 9.

## 6. Abnahme

Alles Folgende ist erfüllt und im Bericht mit Ausgabe belegt:

```bash
# 1. Tests, beide SDK-Versionen
uv run --no-project --with pytest --with pytest-asyncio --with "mcp[cli]>=1.30,<2" --with httpx --with duckdb --with python-calamine pytest
uv run --no-project --with pytest --with pytest-asyncio --with "mcp[cli]>=2,<3"   --with httpx --with duckdb --with python-calamine pytest

# 2. Rauchtest gegen den Container aus dem veroeffentlichten Image
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/healthz          # 200
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/mcp              # 401
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"rauchtest","version":"0"}}}'
# Antwort enthaelt serverInfo mit dem Namen data-gv-at

# 3. Zeitmessung, je Weg drei Laeufe von null
time ./deploy/exoscale/up.sh          # jeder Lauf hoechstens 300 s, danach ./deploy/exoscale/down.sh
time ./deploy/librechat/install.sh    # jeder Lauf hoechstens 300 s, auf frischer LibreChat-Installation

# 3b. Neues Projekt von null: Anleitung einmal vollstaendig durchspielen und stoppen,
#     einmalige Voraussetzungen eingerechnet. Grenze 1800 s oder der angepasste Wert.

# 4. Wiederholung und Rueckbau
./deploy/librechat/install.sh         # zweiter Aufruf aendert nichts
./deploy/librechat/install.sh --uninstall && git -C . status   # LibreChat-Dateien wie vorher (Vergleich mit Sicherung)
```

Zusätzlich:

- stdio-Start am Mac unverändert (`claude mcp add …` aus dem README funktioniert ohne neue Variablen).
- Kein Token, keine echte Domain und keine IP-Adresse im Repo (`git grep` belegt).
- Ist die `exo`-CLI in der Umgebung des Agenten nicht eingerichtet, misst er Weg A ersatzweise auf einer frischen lokalen VM mit demselben `cloud-init.yaml`, kennzeichnet das klar als Ersatzmessung und führt den echten Exoscale-Lauf unter „Nicht getestet“.

## 7. Bedrohungsbild für den öffentlichen Betrieb

Der Agent prüft seine Änderungen gegen diese Fälle und beschreibt die Gegenmaßnahme im README:

1. **Token-Leak.** Der Angreifer nutzt den Server als Abruf-Proxy und Lastverstärker. Gegenmittel: mehrere Tokens mit Wechsel, optionale Ziel-Liste (L13), Parallelitätslimit, Größenlimits, Ratenbegrenzung am Proxy.
2. **Mitlesen.** Gegenmittel: TLS zwingend, kein Klartext-Port nach außen.
3. **Ausbruch aus dem Container.** Gegenmittel: Härtung aus Phase 3, Lauf ohne Root.
4. **Ressourcen-Erschöpfung.** Gegenmittel: Speicher- und Prozesslimits, begrenztes Temp-Verzeichnis, bestehende Sandbox-Limits.
5. **Abruf interner Adressen.** Gegenmittel besteht (SSRF-Prüfung mit IP-Pinning). Zusätzlich mit eigenem Test belegen, dass der Metadaten-Dienst der Cloud-Instanz (Link-Local-Adresse) gesperrt ist.
6. **Zugriff auf die VM.** Gegenmittel: SSH nur per Schlüssel und nur von bekannter IP, automatische Updates.
7. **Token auf dem Transportweg der Einrichtung.** Gegenmittel: Erzeugung auf dem Zielsystem, nicht in User-Daten, nicht im Log, nicht im Bericht.

## 8. Empfehlungen (der Agent darf abweichen)

| Thema | Empfehlung |
|---|---|
| Betriebsform Weg A | Eine VM mit Docker Compose, Start vollständig über cloud-init |
| Reverse Proxy | Caddy, wegen automatischer Zertifikate mit wenig Konfiguration |
| Image-Quelle | GHCR, öffentlich. Lokaler Build bleibt als Alternative dokumentiert |
| Hostname ohne DNS-Wartezeit | Der schnellste in Phase 0 belegte Weg. Eigene Domain als Parameter |
| Authentifizierung | Feste Bearer-Tokens, je Client ein eigenes |
| Ziel-Liste (L13) | Eingebaut, standardmäßig aus, im Exoscale-Beispiel auskommentiert vorgeschlagen |
| Zugriffsbeschränkung per Quell-IP | Dokumentiert, nicht voreingestellt |
| Einbindung in LibreChat | Eigene Compose-Datei statt Eingriff in fremde, wenn LibreChat das sauber zulässt |

## 9. Abschlussbericht

Der Agent meldet sich genau einmal, mit `docs/abschlussbericht.md` und demselben Inhalt im Text des Pull Requests:

1. **Ergebnis in einem Satz:** Schnellstart (5 Minuten) und neues Projekt (30 Minuten) je Weg erreicht, verfehlt oder angepasst, mit Median und schlechtestem Lauf. Bei angepasster Grenze der Schätzwert der Sub-Agenten und sein Grund.
2. **Die zwei Befehle** für Ralph als Kopiervorlage, samt Voraussetzungen.
3. **Zeit je Schritt** aus den Messläufen, verglichen mit der Ausgangszeit aus Phase 0.
4. **Entscheidungen** mit Verweis auf `docs/entscheidungen.md`, die drei folgenreichsten ausgeschrieben.
5. **Lücken L1 bis L13:** je „behoben“, „nicht bestätigt“ oder „bewusst offen“, mit Beleg.
6. **Getestet und nicht getestet,** mit Datum und Umgebung.
7. **Angelegte und entfernte Ressourcen** mit Laufzeit und Kosten.
8. **Falls verfehlt:** die gemessene Ursache und der nächste Hebel mit erwartetem Gewinn in Sekunden.

## 10. Geparkt

- OAuth statt fester Tokens, sobald mehrere Personen oder Organisationen den Dienst nutzen.
- Eigene Exoscale-Vorlage mit fertig installiertem Docker und Image, falls die Messung zeigt, dass die Bereitstellung der Engpass bleibt.
- Terraform-Modul für Exoscale.
- Betrieb ohne Docker als systemd-Dienst.
- Privates Netz statt öffentlicher Adresse, falls der einzige Client eine LibreChat-Instanz im selben Exoscale-Netz ist.
- Unterstützung für Kartendienste (WMS/WFS als Datenquelle). Das ist ein eigenes Thema und kein Teil dieses Auftrags.
