#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp[cli]>=1.30,<3",
#   "httpx>=0.27.0",
#   "pydantic>=2.7.0",
#   "duckdb>=1.1.0",
#   "python-calamine>=0.3.0",
# ]
# ///
# Laeuft mit mcp 1.x (FastMCP) und 2.x (MCPServer); beide Versionen werden getestet.

import asyncio
import atexit
import codecs
import csv
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import zipfile
import zlib
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlparse

import duckdb
import httpcore
import httpx
try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _ServerClass

    MCP_V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _ServerClass

    MCP_V2 = False
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("datagvat-mcp")

SEARCH_API_BASE = "https://www.data.gv.at/api/hub/search"
REPO_API_BASE = "https://www.data.gv.at/api/hub/repo"
HTTP_TIMEOUT = float(os.getenv("DATAGVAT_TIMEOUT", "20.0"))
MAX_DOWNLOAD_BYTES = int(os.getenv("DATAGVAT_MAX_BYTES", str(50 * 1024 * 1024)))
MAX_UNPACKED_BYTES = int(os.getenv("DATAGVAT_MAX_UNPACKED_BYTES", str(200 * 1024 * 1024)))
MAX_SQL_ROWS = 200
MAX_REDIRECTS = 5
DOWNLOAD_DEADLINE = float(os.getenv("DATAGVAT_DOWNLOAD_TIMEOUT", "120"))  # Gesamtdauer eines Downloads
MAX_PARALLEL = max(1, int(os.getenv("DATAGVAT_MAX_PARALLEL", "4")))  # gleichzeitige Tool-Aufrufe mit Datei
MAX_ZIP_ENTRIES = 10_000  # mehr Eintraege im Archiv: ZipInfo-Objekte wuerden Hunderte MB belegen
MAX_EXCEL_CELLS = 5_000_000  # Zellbereich eines Blatts (calamine baut ihn dicht auf)
MAX_CELL_CHARS = 2000  # Zellen im SQL-Ergebnis werden darauf gekuerzt
MAX_RESULT_CHARS = 1_000_000  # Gesamtgroesse des SQL-Ergebnisses (Text)
QUERY_TIMEOUT_SECONDS = float(os.getenv("DATAGVAT_QUERY_TIMEOUT", "30"))
CACHE_TTL_SECONDS = float(os.getenv("DATAGVAT_CACHE_TTL", "900"))  # 0 = Cache aus
CACHE_MAX_BYTES = int(os.getenv("DATAGVAT_CACHE_MAX_BYTES", str(200 * 1024 * 1024)))
RETRY_ATTEMPTS = int(os.getenv("DATAGVAT_RETRIES", "2"))  # zusaetzliche Versuche nach dem ersten
RETRY_STATUSES = frozenset({429, 502, 503, 504})
RETRY_MAX_DELAY = 10.0
LINK_CHECK_TIMEOUT = float(os.getenv("DATAGVAT_LINK_CHECK_TIMEOUT", "8"))  # je Link: HTTP-Timeout, Gesamtlimit das Doppelte
LINK_CHECK_MAX = 20  # hoechstens so viele Distributions-Links pro Datensatz pruefen
LINK_CHECK_PARALLEL = 6  # gleichzeitige Link-Pruefungen
__version__ = "1.0.0"
USER_AGENT = f"datagvat-mcp/{__version__} (+https://www.data.gv.at/)"

# Nur fuer Tests: ersetzt den HTTP-Transport (httpx.MockTransport).
_TRANSPORT: httpx.AsyncBaseTransport | None = None

# 1.x nimmt host/port im Konstruktor (steuert u. a. den DNS-Rebinding-Schutz), 2.x erst beim Bauen der HTTP-App.
mcp = (
    _ServerClass("data-gv-at")
    if MCP_V2
    else _ServerClass("data-gv-at", host=os.getenv("MCP_HOST", "127.0.0.1"), port=int(os.getenv("MCP_PORT", "8000")))
)


def _client() -> httpx.AsyncClient:
    # Redirects werden bewusst manuell verfolgt (siehe _download_to_temp), damit jeder Hop geprueft wird.
    # trust_env=False: keine Proxy-/netrc-Umgebung, sonst ginge die Anfrage an ein anderes Ziel als das geprueftete.
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": USER_AGENT},
        transport=_TRANSPORT or _pinned_transport(),
    )


def _extract_localized(field: Any, lang: str = "de") -> str:
    if isinstance(field, str):
        return field
    if isinstance(field, dict):
        if lang in field and field[lang]:
            return str(field[lang])
        for val in field.values():
            if val:
                return str(val)
    return ""


def _label(obj: Any) -> str | None:
    """Piveau liefert Format, Lizenz usw. als {id,label,resource}-Objekt oder als String."""
    if isinstance(obj, dict):
        value = obj.get("label") or obj.get("id") or obj.get("resource")
        return str(value) if value else None
    if isinstance(obj, str) and obj:
        return obj
    return None


def _license_name(obj: Any) -> tuple[str | None, str | None]:
    """Liefert (lesbarer Name, URL). Die API gibt oft nur {"resource": "https://creativecommons.org/licenses/by/4.0/"}."""
    if isinstance(obj, dict):
        url = obj.get("resource") if isinstance(obj.get("resource"), str) else None
        name = obj.get("label") or obj.get("id")
    else:
        url, name = None, _label(obj)
    if not name and url:
        match = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z\-]+)/?([0-9.]+)?", url)
        if match:
            kind = match.group(2)
            name = "CC0 1.0" if kind in ("zero", "mark") else f"CC {kind.upper()} {match.group(3) or ''}".strip()
        else:
            name = url
    return name, url


def _first_url(value: Any) -> str | None:
    """access_url / download_url sind in der API Listen, in aelteren Daten Strings."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item:
                return item
    return None


def _all_urls(*values: Any) -> list[str]:
    urls: list[str] = []
    for value in values:
        items = [value] if isinstance(value, str) else value if isinstance(value, (list, tuple)) else []
        for item in items:
            if isinstance(item, str) and item and item not in urls:
                urls.append(item)
    return urls


def _distribution(d: dict[str, Any]) -> dict[str, Any]:
    download = d.get("download_url") or d.get("downloadUrl")
    access = d.get("access_url") or d.get("accessUrl")
    license_name, license_url = _license_name(d.get("license") or d.get("licence"))
    return {
        "id": d.get("id"),
        "title": _extract_localized(d.get("title")),
        "format": _label(d.get("format")),
        "media_type": d.get("media_type"),
        "byte_size": d.get("byte_size"),
        "license": license_name,
        "license_url": license_url,
        "url": _first_url(download) or _first_url(access),
        "all_urls": _all_urls(download, access),
    }


# Formate, die der Server laden kann, nach Eignung (kleiner = besser). Excel und ZIP gehen, sind aber Notloesungen.
_LOADABLE_FORMATS = {"PARQUET": 0, "CSV": 1, "JSON": 2, "GEOJSON": 2, "XLSX": 3, "XLS": 3, "ZIP": 4}
_FORMAT_NOTES = {
    "ZIP": "Archiv wird entpackt; bei mehreren Dateien darin archive_member angeben (erst ohne Angabe inspizieren)",
    "XLSX": "Excel wird nach CSV umgewandelt; bei Bedarf sheet und skip_rows angeben",
    "XLS": "Excel wird nach CSV umgewandelt; bei Bedarf sheet und skip_rows angeben",
}
_UNLOADABLE_HINTS = {
    "PDF": "PDF enthaelt keine abfragbaren Tabellen",
    "HTML": "Webseite, keine Datei",
    "XML": "XML wird nicht unterstuetzt",
    "WMS": "Kartendienst, keine Tabelle",
    "WFS": "Kartendienst, keine Tabelle",
}
_URL_SUFFIX_FORMATS = {
    ".parquet": "PARQUET", ".csv": "CSV", ".json": "JSON", ".geojson": "GEOJSON", ".zip": "ZIP",
    ".xlsx": "XLSX", ".xlsm": "XLSX", ".xls": "XLS", ".pdf": "PDF", ".html": "HTML", ".htm": "HTML", ".xml": "XML",
}
_ROLE_RANK = {"data": 0, "other": 1, "dimension": 2, "header": 3}


def _effective_format(dist: dict[str, Any]) -> str | None:
    """Katalog-Label (oft 'external' oder leer) durch die Dateiendung der URL absichern."""
    label = (dist.get("format") or "").upper()
    if label in _LOADABLE_FORMATS or label in _UNLOADABLE_HINTS:
        return label
    suffix = Path(urlparse(dist.get("url") or "").path.lower()).suffix
    return _URL_SUFFIX_FORMATS.get(suffix) or label or None


def _file_role(url: str | None) -> str:
    """Statistik Austria legt neben die Datendatei '<name>_HEADER.csv' (Spaltenbeschreibung) und
    '<name>_C-<Dimension>.csv' (Codelisten). Heuristik am Dateinamen."""
    stem = Path(urlparse(url or "").path).stem
    if re.search(r"_header$", stem, re.I):
        return "header"
    if re.search(r"_C-[^/]+$", stem, re.I):
        return "dimension"
    return "data"


def _year_hint(dist: dict[str, Any]) -> int | None:
    """Hoechste Jahreszahl in Titel oder Dateiname (z. B. 'luftguete-2022.zip'), um gleichartige Dateien je Jahr zu ordnen."""
    stem = Path(urlparse(dist.get("url") or "").path).stem
    years = re.findall(r"(?<![A-Za-z0-9])((?:19|20)\d\d)(?![A-Za-z0-9])", f"{dist.get('title') or ''} {stem}")
    return max(map(int, years)) if years else None


def _lookup_table_name(url: str, used: set[str]) -> str:
    """Tabellenname fuer eine Codelisten-URL: Statistik-Austria-Muster '..._C-<NAME>-<n>.csv' -> '<name>'."""
    stem = Path(urlparse(url or "").path).stem
    m = re.search(r"_C-(.+)-\d+$", stem, re.I)
    name = re.sub(r"[^a-z0-9_]", "_", m.group(1).lower())[:30] if m else ""
    if not name:
        i = 1
        while f"lookup{i}" in used:
            i += 1
        name = f"lookup{i}"
    elif not name[0].isalpha():
        name = ("t_" + name)[:30]
    if name in used:
        i = 2
        while f"{name}_{i}" in used:
            i += 1
        name = f"{name}_{i}"
    used.add(name)
    return name


def _assess_distribution(dist: dict[str, Any]) -> dict[str, Any]:
    """Ergaenzt role, usable, note und rank; reiner Datenabgleich, kein Netzzugriff."""
    fmt = _effective_format(dist)
    role = _file_role(dist.get("url")) if fmt in _LOADABLE_FORMATS else "other"
    note = None
    usable = fmt in _LOADABLE_FORMATS and bool(dist.get("url"))
    if not dist.get("url"):
        note = "Keine URL im Katalog"
    elif fmt in _UNLOADABLE_HINTS:
        note = _UNLOADABLE_HINTS[fmt]
    elif fmt not in _LOADABLE_FORMATS:
        note = f"Format '{fmt or 'unbekannt'}' nicht ladbar"
    elif role == "header":
        note = "Nur Spaltenbeschreibung (Metadaten), keine Daten"
        usable = False
    elif role == "dimension":
        note = "Codeliste einer Dimension, keine Hauptdaten; nur zum Entschluesseln von Codes"
    elif fmt in _FORMAT_NOTES:
        note = _FORMAT_NOTES[fmt]
    size = dist.get("byte_size")
    if usable and isinstance(size, int) and size > MAX_DOWNLOAD_BYTES:
        usable = False
        note = f"Datei mit {size // (1024 * 1024)} MB ueberschreitet das Limit von {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB"
    rank_fmt = _LOADABLE_FORMATS.get(fmt or "", 9)
    year = _year_hint(dist)
    return {
        **dist,
        "year": year,
        "format_detected": fmt,
        "role": role,
        "usable": usable,
        "note": note,
        "_sort": (not usable, _ROLE_RANK[role], rank_fmt, -(year or 0), size if isinstance(size, int) else 0),
    }


def _rank_distributions(dists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assessed = sorted((_assess_distribution(d) for d in dists), key=lambda d: d["_sort"])
    for position, d in enumerate(assessed, start=1):
        d.pop("_sort")
        d["rank"] = position
    return assessed


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _resolve_public_ips(host: str) -> list[str]:
    """Loest den Hostnamen auf und verlangt, dass ALLE Adressen oeffentlich sind (sonst ValueError)."""
    try:
        addr_info = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Hostname '{host}' konnte nicht aufgeloest werden: {exc}") from exc
    ips: list[str] = []
    for entry in addr_info:
        ip_str = entry[4][0]
        ip_obj = ipaddress.ip_address(ip_str)
        if not ip_obj.is_global or ip_obj in _NAT64:
            raise ValueError(f"Zugriff auf nicht-oeffentliche IP-Adresse ({ip_str}) ist blockiert.")
        if ip_str not in ips:
            ips.append(ip_str)
    return ips


def _validate_public_url(url: str) -> None:
    """Fruehe Pruefung mit klarer Meldung; die verbindliche Pruefung macht _PublicOnlyBackend beim Verbinden."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Nur vollstaendige http:// oder https:// URLs sind zulaessig.")
    _resolve_public_ips(parsed.hostname)


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Loest den Host beim Verbinden selbst auf, prueft die Adressen und verbindet auf genau diese IP.
    Schliesst DNS-Rebinding aus (Pruefung und Verbindung nutzen dieselbe Aufloesung); TLS/SNI laufen weiter
    ueber den Hostnamen."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        ips = await asyncio.to_thread(_resolve_public_ips, host)
        last: Exception | None = None
        for ip in ips:
            try:
                return await self._inner.connect_tcp(
                    ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except httpcore.ConnectError as exc:
                last = exc
        raise last or httpcore.ConnectError(f"Keine Adresse fuer {host}.")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise ValueError("Unix-Sockets sind nicht erlaubt.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _pinned_transport() -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport()
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):  # pragma: no cover
        raise RuntimeError("Unerwartete httpx-Version: Verbindungsschutz (IP-Pruefung beim Verbinden) nicht moeglich.")
    pool._network_backend = _PublicOnlyBackend()
    return transport


def _sniff_unsupported(head: bytes) -> str | None:
    """Erkennt Dateitypen, die keine Daten sind (PDF, Webseite, XML), damit kein Muell als CSV geladen wird."""
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"%PDF"):
        return "PDF-Dokument (keine tabellarischen Daten)"
    if stripped.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "HTML-Seite (vermutlich Landing-Page oder Fehlerseite statt Datei)"
    if stripped.startswith(b"<?xml"):
        return "XML-Dokument (nicht unterstuetzt)"
    return None


_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"  # altes Excel (.xls)
# Dateiendung im Archiv -> Verarbeitung ("excel" wird zu CSV umgewandelt)
_MEMBER_KINDS = {
    ".csv": ".csv", ".txt": ".csv", ".tsv": ".csv", ".json": ".json", ".geojson": ".json",
    ".parquet": ".parquet", ".xlsx": "excel", ".xlsm": "excel", ".xls": "excel",
}


class _SelectionNeeded(ValueError):
    """Eine Auswahl (Archivdatei, Blatt) ist noetig; info enthaelt die Optionen fuer die Fehlerantwort."""

    def __init__(self, message: str, info: dict[str, Any]):
        super().__init__(message)
        self.info = info


@dataclass
class _Prepared:
    path: Path
    ext: str  # .csv, .json oder .parquet
    info: dict[str, Any]
    size: int = 0


def _detect_format(head: bytes, url: str, fmt_hint: str, content_type: str) -> str:
    hint = fmt_hint.lower()
    parsed_path = urlparse(url).path.lower()
    if head.startswith(b"PAR1") or "parquet" in hint or parsed_path.endswith(".parquet") or "parquet" in content_type:
        return ".parquet"
    if "csv" in hint:
        return ".csv"
    if "json" in hint or parsed_path.endswith((".json", ".geojson")) or "json" in content_type:
        return ".json"
    return ".csv"


_tmp_dir: Path | None = None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # z. B. PermissionError: Prozess existiert, gehoert nur jemand anderem
        return True
    return True


def _tmp_root() -> Path:
    """Eigenes Temp-Verzeichnis je Prozess. Reste abgestuerzter Prozesse (SIGKILL) werden beim naechsten Start entfernt."""
    global _tmp_dir
    if _tmp_dir is None or not _tmp_dir.exists():
        base = Path(tempfile.gettempdir())
        for old in base.glob("datagvat-*"):
            m = re.fullmatch(r"datagvat-(\d+)-\w+", old.name)
            if m and old.is_dir() and not _pid_alive(int(m.group(1))):
                shutil.rmtree(old, ignore_errors=True)
        _tmp_dir = Path(tempfile.mkdtemp(prefix=f"datagvat-{os.getpid()}-"))
        atexit.register(shutil.rmtree, _tmp_dir, True)
    return _tmp_dir


def _mkstemp(suffix: str = "") -> tuple[int, str]:
    return tempfile.mkstemp(suffix=suffix, dir=_tmp_root())


def _zip_entry_count(path: Path) -> int | None:
    """Liest die Eintragszahl aus dem End-Record, ohne das Inhaltsverzeichnis zu laden (0xFFFF = Zip64/sehr viele)."""
    size = path.stat().st_size
    with path.open("rb") as fin:
        fin.seek(max(0, size - 65_557))
        tail = fin.read()
    i = tail.rfind(b"PK\x05\x06")
    if i < 0 or len(tail) < i + 22:
        return None
    return struct.unpack_from("<H", tail, i + 10)[0]


def _normalize_to_utf8(src: Path, suffix: str = ".csv") -> Path:
    """Schreibt eine Textdatei blockweise nach UTF-8 um (UTF-8-BOM oder cp1252, wie bei OGD ueblich)."""
    for encoding in ("utf-8-sig", "cp1252"):
        fd, name = _mkstemp(suffix)
        os.close(fd)
        dst = Path(name)
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict" if encoding == "utf-8-sig" else "replace")
            with src.open("rb") as fin, dst.open("w", encoding="utf-8", newline="") as fout:
                while chunk := fin.read(1024 * 1024):
                    fout.write(decoder.decode(chunk))
                fout.write(decoder.decode(b"", final=True))
            return dst
        except UnicodeDecodeError:
            dst.unlink(missing_ok=True)
        except BaseException:
            dst.unlink(missing_ok=True)
            raise
    raise ValueError("Textkodierung nicht lesbar.")  # unerreichbar: cp1252 mit 'replace' wirft nicht


def _link_or_copy(src: Path, suffix: str) -> Path:
    """Gibt der Datei eine Endung, ohne das (gecachte) Original anzutasten."""
    fd, name = _mkstemp(suffix)
    os.close(fd)
    dst = Path(name)
    dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)
    return dst


def _is_ooxml(names: list[str]) -> bool:
    return "[Content_Types].xml" in names and any(n.startswith("xl/") for n in names)


def _excel_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ") if hasattr(value, "hour") and hasattr(value, "year") else value.isoformat()
    return str(value)


def _header_index(rows: list[list[str]]) -> int:
    """Titelzeilen ueber der Tabelle erkennen: erste Zeile, die (fast) so breit ist wie die breiteste."""
    counts = [sum(1 for c in row if c != "") for row in rows[:50]]
    width = max(counts, default=0)
    if width < 2:
        return next((i for i, c in enumerate(counts) if c), 0)
    needed = max(2, -(-width * 6 // 10))
    return next((i for i, c in enumerate(counts) if c >= needed), 0)


def _prepare_ooxml(raw: Path, sheet: str, skip_rows: int) -> _Prepared:
    """xlsx: Mitglieder mit Zaehler in ein neues Archiv kopieren. Die Header-Groessen eines ZIP koennen gefaelscht
    sein; Pythons zipfile liest nur so viele Bytes wie angegeben und prueft die CRC, ein Fake faellt also auf."""
    fd, name = _mkstemp(".xlsx")
    os.close(fd)
    safe = Path(name)
    total = 0
    try:
        try:
            with zipfile.ZipFile(raw) as zin, zipfile.ZipFile(safe, "w", zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    if info.is_dir():
                        continue
                    with zin.open(info) as fin, zout.open(info.filename, "w") as fout:
                        while chunk := fin.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_UNPACKED_BYTES:
                                raise ValueError("Die Excel-Datei ist entpackt zu gross (Zip-Bomb-Schutz).")
                            fout.write(chunk)
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise ValueError(f"Die Excel-Datei ist beschaedigt (Archiv nicht konsistent): {exc}") from exc
        return _prepare_excel(safe, ".xlsx", sheet, skip_rows)
    finally:
        safe.unlink(missing_ok=True)


def _prepare_excel(path: Path, suffix: str, sheet: str, skip_rows: int) -> _Prepared:
    try:
        from python_calamine import CalamineWorkbook
    except ImportError as exc:  # pragma: no cover
        raise ValueError("Excel-Unterstuetzung nicht verfuegbar (python-calamine fehlt).") from exc

    work = _link_or_copy(path, suffix)  # calamine erkennt das Format an der Endung
    fd, out_name = _mkstemp(".csv")
    os.close(fd)
    out = Path(out_name)
    try:
        try:
            wb = CalamineWorkbook.from_path(str(work))
        except Exception as exc:
            raise ValueError(f"Excel-Datei nicht lesbar (beschaedigt oder passwortgeschuetzt): {exc}") from exc
        names = list(wb.sheet_names)
        if sheet:
            by_lower = {n.lower(): n for n in names}
            if sheet.isdigit() and 1 <= int(sheet) <= len(names):
                chosen = names[int(sheet) - 1]
            elif sheet in names:
                chosen = sheet
            elif sheet.lower() in by_lower:
                chosen = by_lower[sheet.lower()]
            else:
                raise _SelectionNeeded(f"Blatt '{sheet}' nicht gefunden.", {"sheets": names})
            candidates = [chosen]
        else:
            candidates = names
        rows: list[list[str]] = []
        chosen = candidates[0]
        for name in candidates:  # ohne Angabe: erstes Blatt mit Inhalt
            sh = wb.get_sheet_by_name(name)
            if sh.total_height * sh.total_width > MAX_EXCEL_CELLS:
                raise ValueError(
                    f"Blatt '{name}' hat einen zu grossen Zellbereich ({sh.total_height} x {sh.total_width}); "
                    f"mehr als {MAX_EXCEL_CELLS:,} Zellen werden nicht geladen."
                )
            rows = [[_excel_cell(c) for c in r] for r in sh.to_python()]
            rows = [r for r in rows if any(c != "" for c in r)]
            chosen = name
            if rows:
                break
        if not rows:
            raise ValueError("Die Excel-Datei enthaelt keine Daten.")
        skipped = _header_index(rows) if skip_rows < 0 else min(skip_rows, len(rows) - 1)
        rows = rows[skipped:]
        width = max(len(r) for r in rows)
        header = [h if h else f"spalte_{i + 1}" for i, h in enumerate(rows[0] + [""] * (width - len(rows[0])))]
        with out.open("w", encoding="utf-8", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(header)
            writer.writerows(r + [""] * (width - len(r)) for r in rows[1:])
        return _Prepared(out, ".csv", {"sheets": names, "selected_sheet": chosen, "skipped_rows": skipped})
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    finally:
        work.unlink(missing_ok=True)


def _prepare_archive(zf: zipfile.ZipFile, member: str, sheet: str, skip_rows: int, hint: str) -> _Prepared:
    entries = [
        i for i in zf.infolist()
        if not i.is_dir() and not i.filename.startswith("__MACOSX/") and not Path(i.filename).name.startswith(".")
    ]
    listing = [
        {"name": i.filename, "size": i.file_size, "supported": Path(i.filename).suffix.lower() in _MEMBER_KINDS}
        for i in entries[:50]
    ]
    supported = [i for i in entries if Path(i.filename).suffix.lower() in _MEMBER_KINDS]
    if member:
        wanted = member.lower()
        picked = next(
            (i for i in entries if i.filename == member or i.filename.lower() == wanted or Path(i.filename).name.lower() == wanted),
            None,
        )
        if picked is None:
            raise _SelectionNeeded(f"Datei '{member}' nicht im Archiv.", {"archive_members": listing})
    elif len(supported) == 1:
        picked = supported[0]
    elif not supported:
        raise ValueError(
            "Das Archiv enthaelt keine CSV-/JSON-/Parquet-/Excel-Datei. Inhalt: "
            + ", ".join(i.filename for i in entries[:10])
        )
    else:
        raise _SelectionNeeded(
            f"Das Archiv enthaelt {len(supported)} Dateien; bitte eine per archive_member waehlen.",
            {"archive_members": listing},
        )
    if picked.flag_bits & 0x1:
        raise ValueError(f"'{picked.filename}' ist passwortgeschuetzt.")
    if picked.file_size > MAX_UNPACKED_BYTES:
        raise ValueError(
            f"'{picked.filename}' ist entpackt {picked.file_size // (1024 * 1024)} MB gross "
            f"(Limit {MAX_UNPACKED_BYTES // (1024 * 1024)} MB)."
        )
    fd, name = _mkstemp(".member")
    os.close(fd)
    extracted = Path(name)
    try:
        total = 0
        with zf.open(picked) as fin, extracted.open("wb") as fout:  # Groesse nicht dem Header glauben, mitzaehlen
            while chunk := fin.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UNPACKED_BYTES:
                    raise ValueError(f"Entpackte Datei ueberschreitet {MAX_UNPACKED_BYTES // (1024 * 1024)} MB.")
                fout.write(chunk)
        inner = _prepare_sync(extracted, picked.filename, hint, "", "", sheet, skip_rows, nested=True)
    finally:
        extracted.unlink(missing_ok=True)
    inner.info = {"archive_members": listing, "selected_member": picked.filename, **inner.info}
    return inner


def _prepare_sync(
    raw: Path, url: str, hint: str, content_type: str, member: str, sheet: str, skip_rows: int, nested: bool = False
) -> _Prepared:
    """Macht aus einer Rohdatei (CSV, JSON, Parquet, Excel, ZIP) eine Datei, die DuckDB laden kann."""
    with raw.open("rb") as fin:
        head = fin.read(4096)

    if head.startswith(_ZIP_MAGIC):
        count = _zip_entry_count(raw)
        if count is not None and count > MAX_ZIP_ENTRIES:
            raise ValueError(f"Das ZIP-Archiv enthaelt zu viele Dateien (mehr als {MAX_ZIP_ENTRIES:,}).")
        if not zipfile.is_zipfile(raw):
            raise ValueError("Das ZIP-Archiv ist beschaedigt.")
        with zipfile.ZipFile(raw) as zf:
            names = zf.namelist()
            if _is_ooxml(names):
                if sum(i.file_size for i in zf.infolist()) > MAX_UNPACKED_BYTES:
                    raise ValueError("Die Excel-Datei ist entpackt zu gross (Zip-Bomb-Schutz).")
                return _prepare_ooxml(raw, sheet, skip_rows)
            if nested:
                raise ValueError("Verschachtelte ZIP-Archive werden nicht unterstuetzt.")
            return _prepare_archive(zf, member, sheet, skip_rows, hint)
    if head.startswith(_OLE_MAGIC):
        return _prepare_excel(raw, ".xls", sheet, skip_rows)

    problem = _sniff_unsupported(head)
    if problem:
        raise ValueError(f"Unter {url} liegt kein lesbares CSV/JSON/Parquet, sondern: {problem}.")

    ext = _detect_format(head, url, hint, content_type)
    path = _link_or_copy(raw, ext) if ext == ".parquet" else _normalize_to_utf8(raw, ext)
    return _Prepared(path, ext, {})


_sleep = asyncio.sleep  # fuer Tests austauschbar


async def _backoff(attempt: int, retry_after: str | None = None) -> None:
    """Wartet vor dem naechsten Versuch: Retry-After (Sekunden) des Servers, sonst 0,5 s * 2^Versuch, hoechstens 10 s."""
    delay = 0.5 * 2**attempt
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            pass  # HTTP-Datum o. ae.: bei der eigenen Staffelung bleiben
    delay = max(0.0, min(delay, RETRY_MAX_DELAY))
    logger.info("Neuer Versuch in %.1f s", delay)
    await _sleep(delay)


async def _get_with_retry(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """GET mit Wiederholung bei 429/502/503/504 und Netzwerkfehlern. Die Antwort wird nicht auf Status geprueft."""
    for attempt in range(RETRY_ATTEMPTS + 1):
        last = attempt == RETRY_ATTEMPTS
        resp = None
        try:
            resp = await client.get(url, **kwargs)
        except httpx.TransportError as exc:
            if last:
                raise
            logger.warning("Netzwerkfehler bei %s: %s", url, exc)
        if resp is not None and (resp.status_code not in RETRY_STATUSES or last):
            return resp
        await _backoff(attempt, resp.headers.get("retry-after") if resp is not None else None)
    raise AssertionError("unerreichbar")  # pragma: no cover


async def _fetch_hop(client: httpx.AsyncClient, url: str, raw: Path) -> tuple[str | None, str]:
    """Ein Download-Schritt mit Wiederholung. Liefert (Weiterleitungsziel, Content-Type); bei Weiterleitung ohne Datei."""
    for attempt in range(RETRY_ATTEMPTS + 1):
        last = attempt == RETRY_ATTEMPTS
        retry_after = None
        try:
            async with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError("Weiterleitung ohne Location-Header.")
                    return location, ""
                if resp.status_code in RETRY_STATUSES and not last:
                    retry_after = resp.headers.get("retry-after")
                else:
                    resp.raise_for_status()
                    total = 0
                    with raw.open("wb") as fout:  # bei Wiederholung wird neu begonnen
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_DOWNLOAD_BYTES:
                                raise ValueError(
                                    f"Datei ueberschreitet das Sicherheitslimit von {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB."
                                )
                            fout.write(chunk)
                    return None, resp.headers.get("content-type", "").lower()
        except httpx.TransportError as exc:
            if last:
                raise
            logger.warning("Netzwerkfehler bei %s: %s", url, exc)
        await _backoff(attempt, retry_after)
    raise AssertionError("unerreichbar")  # pragma: no cover


async def _download_raw(url: str) -> tuple[Path, str]:
    """Laedt eine Distribution gestreamt auf die Platte. Jede Weiterleitung wird erneut gegen SSRF geprueft."""
    fd, name = _mkstemp(".download")
    os.close(fd)
    raw = Path(name)
    try:
        current = url
        async with _client() as client:
            for _ in range(MAX_REDIRECTS + 1):
                await asyncio.to_thread(_validate_public_url, current)
                location, content_type = await _fetch_hop(client, current, raw)
                if location is None:
                    break
                current = str(httpx.URL(current).join(location))
            else:
                raise ValueError(f"Mehr als {MAX_REDIRECTS} Weiterleitungen.")
        with raw.open("rb") as fin:
            head = fin.read(4096)
        problem = None if head.startswith((_ZIP_MAGIC, _OLE_MAGIC)) else _sniff_unsupported(head)
        if problem:
            raise ValueError(f"Unter {url} liegt kein lesbares CSV/JSON/Parquet, sondern: {problem}.")
        return raw, content_type
    except BaseException:
        raw.unlink(missing_ok=True)
        raise


async def _download_with_deadline(url: str) -> tuple[Path, str]:
    """Wie _download_raw, aber mit Gesamtzeitlimit (der HTTP-Timeout gilt nur je Lesevorgang)."""
    try:
        async with asyncio.timeout(DOWNLOAD_DEADLINE):
            return await _download_raw(url)
    except TimeoutError as exc:
        raise ValueError(f"Der Download dauerte laenger als {DOWNLOAD_DEADLINE:.0f} s und wurde abgebrochen.") from exc


async def _check_link(client: httpx.AsyncClient, url: str) -> dict:
    """Prueft per Range-GET (hoechstens 4096 Bytes), ob hinter dem Link eine Datei liegt. Kein HEAD, keine Wiederholungen."""
    try:
        async with asyncio.timeout(LINK_CHECK_TIMEOUT * 2):
            current = url
            redirected = False
            head = b""
            for _ in range(MAX_REDIRECTS + 1):
                await asyncio.to_thread(_validate_public_url, current)
                async with client.stream(
                    "GET", current, headers={"Range": "bytes=0-4095"}, timeout=httpx.Timeout(LINK_CHECK_TIMEOUT)
                ) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return {"ok": False, "status": "Weiterleitung ohne Ziel"}
                        current = str(httpx.URL(current).join(location))
                        redirected = True
                        continue
                    suffix = f" (weitergeleitet auf {httpx.URL(current).host})" if redirected else ""
                    if resp.status_code == 416:  # Range Not Satisfiable: Datei existiert
                        return {"ok": True, "status": "ok" + suffix}
                    if not 200 <= resp.status_code < 300:
                        return {"ok": False, "status": f"tot (HTTP {resp.status_code})"}
                    head = b""
                    async for chunk in resp.aiter_bytes(4096):
                        head += chunk
                        if len(head) >= 4096:
                            break
                if head.startswith((_ZIP_MAGIC, _OLE_MAGIC)):
                    return {"ok": True, "status": "ok" + suffix}
                problem = _sniff_unsupported(head)
                if problem:
                    return {"ok": False, "status": f"liefert keine Datei: {problem}{suffix}"}
                return {"ok": True, "status": "ok" + suffix}
            return {"ok": False, "status": "zu viele Weiterleitungen"}
    except ValueError:
        return {"ok": False, "status": "gesperrt (keine oeffentliche Adresse)"}
    except (TimeoutError, httpx.HTTPError, httpx.InvalidURL):
        return {"ok": False, "status": "nicht erreichbar"}


async def _check_distribution_links(dists: list[dict[str, Any]]) -> tuple[int, bool]:
    """Prueft die Links der gerankten Distributionen (max. LINK_CHECK_MAX, davon max. LINK_CHECK_PARALLEL parallel).
    Liefert (Anzahl der Pruefungen, ob mindestens ein Link ok war); tote Links sind danach nicht mehr usable."""
    sem = asyncio.Semaphore(LINK_CHECK_PARALLEL)
    checked = 0
    any_ok = False

    async def run(client: httpx.AsyncClient, d: dict[str, Any]) -> None:
        nonlocal checked, any_ok
        async with sem:
            result = await _check_link(client, d["url"])
        checked += 1
        any_ok = any_ok or result["ok"]
        d["link_status"] = result["status"]
        if not result["ok"] and d.get("usable"):
            d["usable"] = False
            d["note"] = f"{d['note']}; {result['status']}" if d.get("note") else result["status"]

    async with _client() as client:
        tasks = []
        for d in dists:
            if not d.get("url"):
                d["link_status"] = "nicht geprueft (keine URL)"
            elif len(tasks) < LINK_CHECK_MAX:
                tasks.append(asyncio.create_task(run(client, d)))
            else:
                d["link_status"] = f"nicht geprueft (mehr als {LINK_CHECK_MAX} Dateien)"
        if tasks:
            await asyncio.gather(*tasks)
    return checked, any_ok


async def _download_to_temp(
    url: str, fmt_hint: str = "", member: str = "", sheet: str = "", skip_rows: int = -1
) -> tuple[Path, str]:
    """Download und Aufbereitung ohne Cache (Pfad und Endung der fertigen Datei)."""
    raw, content_type = await _download_raw(url)
    try:
        prepared = await asyncio.to_thread(_prepare_sync, raw, url, fmt_hint, content_type, member, sheet, skip_rows)
    finally:
        raw.unlink(missing_ok=True)
    return prepared.path, prepared.ext


@dataclass
class _CacheEntry:
    raw: Path
    content_type: str
    stored: float
    last_used: float
    raw_size: int
    prepared: dict[tuple, _Prepared]
    users: int = 0  # laufende Abfragen; solche Eintraege werden nie geloescht

    @property
    def size(self) -> int:
        return self.raw_size + sum(p.size for p in self.prepared.values())


_cache: dict[str, _CacheEntry] = {}
_download_locks: dict[str, asyncio.Lock] = {}
_slot_sem: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _slots() -> asyncio.Semaphore:
    """Begrenzt gleichzeitige Tool-Aufrufe mit Datei (je Aufruf bis 512 MB DuckDB plus Platte)."""
    loop = asyncio.get_running_loop()
    if loop not in _slot_sem:
        _slot_sem.clear()
        _slot_sem[loop] = asyncio.Semaphore(MAX_PARALLEL)
    return _slot_sem[loop]


def _cache_drop(key: str) -> None:
    entry = _cache.pop(key, None)
    if entry:
        entry.raw.unlink(missing_ok=True)
        for prepared in entry.prepared.values():
            prepared.path.unlink(missing_ok=True)


def _cache_evict() -> None:
    """Entfernt abgelaufene Eintraege, dann die am laengsten ungenutzten, bis das Byte-Limit passt."""
    for k, lk in list(_download_locks.items()):  # Sperren ohne Eintrag und ohne Halter wegraeumen
        if k not in _cache and not lk.locked():
            del _download_locks[k]
    now = time.monotonic()
    for key, entry in list(_cache.items()):
        if entry.users == 0 and now - entry.stored >= CACHE_TTL_SECONDS:
            _cache_drop(key)
    total = sum(e.size for e in _cache.values())
    for key, entry in sorted(_cache.items(), key=lambda kv: kv[1].last_used):
        if total <= CACHE_MAX_BYTES:
            break
        if entry.users == 0:
            total -= entry.size
            _cache_drop(key)


def _cache_clear() -> None:
    for key in list(_cache):
        _cache_drop(key)
    _download_locks.clear()


atexit.register(_cache_clear)


@asynccontextmanager
async def _cached_download(
    url: str, fmt_hint: str = "", member: str = "", sheet: str = "", skip_rows: int = -1
):
    """Liefert (Pfad, Endung, Info) der aufbereiteten Datei. Die Rohdatei einer URL wird innerhalb der TTL nur
    einmal geladen (auch fuer verschiedene Archivdateien/Blaetter), parallele Anfragen teilen sich den Download.
    Fehlgeschlagene Downloads werden nicht gemerkt."""
    key = hashlib.sha256(url.encode()).hexdigest()
    async with _slots():
        lock = _download_locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = _cache.get(key)
            if entry and entry.users == 0 and (
                time.monotonic() - entry.stored >= CACHE_TTL_SECONDS or not entry.raw.exists()
            ):
                _cache_drop(key)
                entry = None
            if entry is None:
                raw, content_type = await _download_with_deadline(url)
                now = time.monotonic()
                entry = _CacheEntry(raw, content_type, now, now, raw.stat().st_size, {}, users=1)
                _cache[key] = entry
            else:
                logger.info("Cache-Treffer fuer %s", url)
                entry.users += 1  # sofort, vor jedem await: ein parallel laufendes _cache_evict darf ihn nicht loeschen
            try:
                variant = (fmt_hint.strip().lower(), member, sheet, skip_rows)
                prepared = entry.prepared.get(variant)
                if prepared is None:
                    prepared = await asyncio.to_thread(
                        _prepare_sync, entry.raw, url, fmt_hint, entry.content_type, member, sheet, skip_rows
                    )
                    prepared.size = prepared.path.stat().st_size
                    entry.prepared[variant] = prepared
                entry.last_used = time.monotonic()
            except BaseException:
                entry.users -= 1
                raise
        _cache_evict()
        try:
            yield prepared.path, prepared.ext, prepared.info
        finally:
            entry.users -= 1
            _cache_evict()


def _load_table(con: duckdb.DuckDBPyConnection, name: str, path: Path, ext: str) -> None:
    """Laedt eine aufbereitete Datei als Tabelle '<name>' (doppelt gequoteter Identifier)."""
    escaped_path = str(path).replace("'", "''")
    quoted = '"' + name.replace('"', '""') + '"'
    if ext == ".parquet":
        con.execute(f"CREATE TABLE {quoted} AS SELECT * FROM read_parquet('{escaped_path}');")
    elif ext == ".json":
        con.execute(f"CREATE TABLE {quoted} AS SELECT * FROM read_json_auto('{escaped_path}');")
    else:
        csv_sql = "CREATE TABLE {tbl} AS SELECT * FROM read_csv_auto('{path}', sample_size=20000, ignore_errors=true{opts});"
        try:
            con.execute(csv_sql.format(tbl=quoted, path=escaped_path, opts=""))
        except duckdb.InvalidInputException:
            # Z. B. Steuerzeichen am Dateiende (Statistik-Austria-Codelisten): lockerer Parser statt Abbruch.
            con.execute(f"DROP TABLE IF EXISTS {quoted};")
            con.execute(csv_sql.format(tbl=quoted, path=escaped_path, opts=", strict_mode=false"))


def _create_sandboxed_duckdb(
    file_path: Path, ext: str, extra: list[tuple[str, Path, str]] | None = None
) -> duckdb.DuckDBPyConnection:
    """In-Memory DuckDB: Datei als Tabelle 'dataset' laden (plus Zusatztabellen), danach externe Zugriffe sperren."""
    con = duckdb.connect(":memory:")
    timer = threading.Timer(QUERY_TIMEOUT_SECONDS * 2, con.interrupt)
    try:
        con.execute("SET memory_limit = '512MB';")
        con.execute("SET max_temp_directory_size = '1GB';")  # Auslagerung auf Platte begrenzen
        con.execute("SET threads = 2;")

        timer.start()
        _load_table(con, "dataset", file_path, ext)
        for name, path, ext2 in extra or []:
            _load_table(con, name, path, ext2)
        timer.cancel()

        con.execute("SET enable_external_access = false;")
        con.execute("SET lock_configuration = true;")
        return con
    except duckdb.InterruptException as exc:
        con.close()
        raise ValueError(f"Das Laden der Datei dauerte laenger als {QUERY_TIMEOUT_SECONDS * 2:.0f} s (abgebrochen).") from exc
    except BaseException:
        con.close()
        raise
    finally:
        timer.cancel()


def _open_dataset(file_path: Path, ext: str, extra: list[tuple[str, Path, str]] | None = None) -> duckdb.DuckDBPyConnection:
    con = _create_sandboxed_duckdb(file_path, ext, extra)
    try:
        empty = ext == ".csv" and con.execute("SELECT COUNT(*) FROM dataset;").fetchone()[0] == 0
    except BaseException:
        con.close()
        raise
    if empty:
        con.close()
        raise ValueError(
            "Aus der Datei konnten 0 Datenzeilen gelesen werden (leer, nur Kopfzeile oder Zeilen passen nicht zur Kopfzeile). "
            "Vermutlich eine Metadaten-/Header-Datei. Andere Distribution desselben Datensatzes waehlen."
        )
    return con


SAMPLE_ROWS = 5
SAMPLE_MAX_COLUMNS = 20
SAMPLE_MAX_CELL = 80


def _sample_cell(value: Any) -> Any:
    if isinstance(value, str) and len(value) > SAMPLE_MAX_CELL:
        return value[:SAMPLE_MAX_CELL] + "…"
    return value


def _cell_cut(value: Any) -> bool:
    return isinstance(value, str) and len(value) > SAMPLE_MAX_CELL


def _inspect_sync(file_path: Path, ext: str) -> dict[str, Any]:
    con = _open_dataset(file_path, ext)
    try:
        schema_rows = con.execute("DESCRIBE dataset;").fetchall()
        row_count = con.execute("SELECT COUNT(*) FROM dataset;").fetchone()[0]
        sample_cur = con.execute(f"SELECT * FROM dataset LIMIT {SAMPLE_ROWS};")
        col_names = [desc[0] for desc in sample_cur.description]
        shown = col_names[:SAMPLE_MAX_COLUMNS]
        raw_rows = [row[: len(shown)] for row in sample_cur.fetchall()]
        cut = any(_cell_cut(v) for row in raw_rows for v in row)
        sample_rows = [{name: _sample_cell(value) for name, value in zip(shown, row)} for row in raw_rows]
    finally:
        con.close()
    result = {
        "table_name": "dataset",
        "detected_extension": ext,
        "total_rows": row_count,
        "columns": [{"name": r[0], "type": r[1]} for r in schema_rows],
        "sample_rows": sample_rows,
    }
    if len(col_names) > len(shown) or cut:
        notes = []
        if len(col_names) > len(shown):
            notes.append(
                f"Beispielzeilen zeigen nur die ersten {len(shown)} von {len(col_names)} Spalten; "
                "alle Spalten stehen in 'columns'. Weitere Werte per query_distribution_sql abrufen."
            )
        if cut:
            notes.append(f"Texte in Beispielzeilen sind auf {SAMPLE_MAX_CELL} Zeichen gekuerzt.")
        result["sample_note"] = " ".join(notes)
    return result


def _check_single_select(con: duckdb.DuckDBPyConnection, sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise ValueError("Leere SQL-Abfrage.")
    try:
        statements = con.extract_statements(sql)
    except duckdb.Error as exc:
        raise ValueError(f"SQL nicht lesbar: {exc}") from exc
    if len(statements) != 1:
        raise ValueError("Genau eine SQL-Anweisung erlaubt.")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise ValueError("Nur lesende Abfragen (SELECT, WITH, DESCRIBE, SUMMARIZE) sind erlaubt.")
    return sql


def _query_sync(
    file_path: Path, ext: str, sql: str, extra: list[tuple[str, Path, str]] | None = None
) -> dict[str, Any]:
    con = _open_dataset(file_path, ext, extra)
    timer = threading.Timer(QUERY_TIMEOUT_SECONDS, con.interrupt)
    try:
        checked = _check_single_select(con, sql)
        timer.start()
        # Unterabfrage mit LIMIT, damit nie Millionen Zeilen materialisiert werden.
        cur = con.execute(f"SELECT * FROM (\n{checked}\n) AS _q LIMIT {MAX_SQL_ROWS + 1}")
        cols = [desc[0] for desc in cur.description]
        rows: list[list[Any]] = []
        total_chars = 0
        truncated = False
        shortened = False
        while not truncated:
            batch = cur.fetchmany(10)
            if not batch:
                break
            for r in batch:
                if len(rows) >= MAX_SQL_ROWS:
                    truncated = True  # die LIMIT-Zeile ueber dem Maximum
                    break
                cells = []
                for v in r:
                    if isinstance(v, (str, bytes)):
                        if len(v) > MAX_CELL_CHARS:
                            v = v[:MAX_CELL_CHARS] + ("\u2026" if isinstance(v, str) else b"")
                            shortened = True
                        total_chars += len(v)
                    else:
                        total_chars += 8
                    cells.append(v)
                rows.append(cells)
                if total_chars > MAX_RESULT_CHARS:
                    truncated = True
                    break
    except duckdb.InterruptException as exc:
        raise ValueError(f"Abfrage nach {QUERY_TIMEOUT_SECONDS:.0f} s abgebrochen (Zeitlimit).") from exc
    finally:
        timer.cancel()
        con.close()
    result = {
        "sql_executed": checked,
        "returned_rows": len(rows),
        "truncated_at_limit": truncated,
        "results": [dict(zip(cols, r)) for r in rows],
    }
    if shortened:
        result["cells_shortened"] = f"Texte laenger als {MAX_CELL_CHARS} Zeichen wurden gekuerzt (mit \u2026); mit substr()/Filtern gezielt abfragen."
    return result


def _jsonable(obj: Any) -> dict[str, Any]:
    """Macht Ergebnisse strikt JSON-tauglich (Decimal, Datum usw. als Text, NaN/Infinity als null)."""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str), parse_constant=lambda _: None)


_PATH_RE = re.compile(r"(?<![\w)\].:/])/(?:[^\s'\"/]+/)+[^\s'\"]*")


def _clean_error(exc: BaseException) -> str:
    """Fehlertext ohne lokale Pfade (Temp, Home) und ohne DuckDB-Quelltextauszug (LINE n: ...)."""
    text = str(exc).split("\nLINE ")[0]
    return _PATH_RE.sub("<Datei>", text).strip()


def _error(message: str, **info: Any) -> CallToolResult:
    """Fehler als echtes MCP-Toolergebnis mit isError=True, damit Clients ihn nicht als Antwort werten."""
    payload = _jsonable({"error": message, **info})
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))],
        structuredContent=payload,
        isError=True,
    )


# Alle Tools lesen nur: weder lokaler noch entfernter Zustand wird veraendert; sie greifen aber ins offene Netz.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


# DCAT-Themen (EU-Vokabular), wie sie data.gv.at als Facette 'categories' fuehrt.
_CATEGORIES = {
    "AGRI": "Landwirtschaft, Fischerei, Forstwirtschaft und Nahrungsmittel",
    "ECON": "Wirtschaft und Finanzen",
    "EDUC": "Bildung, Kultur und Sport",
    "ENER": "Energie",
    "ENVI": "Umwelt",
    "GOVE": "Regierung und öffentlicher Sektor",
    "HEAL": "Gesundheit",
    "INTR": "Internationale Themen",
    "JUST": "Justiz, Rechtssystem und öffentliche Sicherheit",
    "REGI": "Regionen und Städte",
    "SOCI": "Bevölkerung und Gesellschaft",
    "TECH": "Wissenschaft und Technologie",
    "TRAN": "Verkehr",
}
_SORTS = {"relevance": None, "modified_desc": "modified+desc", "modified_asc": "modified+asc", "issued_desc": "issued+desc"}
_LOADABLE_FACET_FORMATS = ["CSV", "JSON", "GEOJSON", "PARQUET", "XLSX", "XLS", "ZIP"]
_FACET_SUMMARY = (("publisher", 8), ("format", 8), ("categories", 8))


def _resolve_category(value: str) -> str:
    wanted = value.strip().casefold()
    for cid, label in _CATEGORIES.items():
        if wanted in (cid.casefold(), label.casefold()):
            return cid
    hits = [cid for cid, label in _CATEGORIES.items() if wanted and wanted in label.casefold()]
    if len(hits) == 1:
        return hits[0]
    options = "; ".join(f"{cid} = {label}" for cid, label in _CATEGORIES.items())
    raise ValueError(f"Kategorie '{value}' nicht eindeutig/unbekannt. Gueltig: {options}")


def _build_facets(publisher: str, fmt: str, category: str, loadable_only: bool) -> dict[str, list[str]]:
    facets: dict[str, list[str]] = {}
    if publisher.strip():
        facets["publisher"] = [publisher.strip()]
    formats = [f.strip() for f in re.split(r"[,;]", fmt) if f.strip()]
    if formats:
        facets["format"] = formats
    elif loadable_only:
        facets["format"] = list(_LOADABLE_FACET_FORMATS)
    if category.strip():
        facets["categories"] = [_resolve_category(category)]
    return facets


def _facet_items(facets: list[dict[str, Any]], facet_id: str) -> list[dict[str, Any]]:
    for facet in facets:
        if facet.get("id") == facet_id:
            return [i for i in facet.get("items", []) if isinstance(i, dict) and i.get("id") is not None]
    return []


def _resolve_facet_values(requested: dict[str, list[str]], summary: list[dict[str, Any]]) -> tuple[dict[str, list[str]], list[str]]:
    """Facettenwerte sind in der API gross-/kleinschreibungsabhaengig. Bei 0 Treffern gleichen wir mit den
    tatsaechlichen Werten der Suche ab: exakt, ohne Gross-/Kleinschreibung, beim Publisher auch eindeutiger Teiltreffer."""
    resolved: dict[str, list[str]] = {}
    notes: list[str] = []
    for key, values in requested.items():
        items = {str(i["id"]): int(i.get("count") or 0) for i in _facet_items(summary, key)}
        out: list[str] = []
        for value in values:
            folded = value.casefold().strip()
            exact = [k for k in items if k.casefold().strip() == folded]
            partial = [k for k in items if folded and folded in k.casefold()] if key == "publisher" and not exact else []
            pick = exact[0] if exact else partial[0] if len(partial) == 1 else None
            if pick is None:
                if partial:
                    top = sorted(partial, key=lambda k: -items[k])[:8]
                    notes.append(f"{key} '{value}' ist nicht eindeutig. Moeglich: " + "; ".join(top))
                else:
                    top = sorted(items, key=lambda k: -items[k])[:8]
                    notes.append(f"{key} '{value}' kommt in dieser Suche nicht vor. Haeufig: " + "; ".join(top))
                out.append(value)
            else:
                if pick != value:
                    notes.append(f"{key} '{value}' wurde als '{pick}' verwendet.")
                out.append(pick)
        resolved[key] = out
    return resolved, notes


def _facet_overview(facets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    overview: dict[str, list[dict[str, Any]]] = {}
    for facet_id, limit in _FACET_SUMMARY:
        top = sorted(_facet_items(facets, facet_id), key=lambda i: -int(i.get("count") or 0))[:limit]
        top = [i for i in top if int(i.get("count") or 0) > 0]
        if not top:
            continue
        entries = []
        for item in top:
            entry: dict[str, Any] = {"value": str(item["id"]), "count": int(item.get("count") or 0)}
            if facet_id == "categories":
                entry["label"] = _CATEGORIES.get(str(item["id"])) or _extract_localized(item.get("title"))
            entries.append(entry)
        overview[facet_id] = entries
    return overview


async def _search_request(
    client: httpx.AsyncClient, query: str, limit: int, page: int, facets: dict[str, list[str]], sort: str
) -> dict[str, Any]:
    params: dict[str, Any] = {"filter": "dataset", "limit": limit, "page": page}
    if query.strip():
        params["q"] = query.strip()
    if facets:
        params["facets"] = json.dumps(facets, ensure_ascii=False)
    if _SORTS[sort]:
        params["sort"] = _SORTS[sort]
    resp = await _get_with_retry(client, f"{SEARCH_API_BASE}/search", params=params, follow_redirects=True)
    resp.raise_for_status()
    return resp.json().get("result", {})


@mcp.tool(
    name="search_datasets",
    title="Datensaetze im Katalog suchen",
    annotations=_READ_ONLY,
    description=(
        "Durchsucht den oesterreichischen Open-Data-Katalog (data.gv.at). Liefert Datensatz-IDs, Titel, Herausgeber, "
        "Formate und eine Facetten-Uebersicht (haeufigste publisher, format, categories) zum Eingrenzen. "
        "Filter: publisher (genauer Name, z. B. 'Statistik Austria', 'Stadt Wien'), format (z. B. 'CSV' oder 'CSV,ZIP'), "
        "category (z. B. 'Umwelt' oder 'ENVI'), loadable_only fuer nur direkt auswertbare Formate. "
        "Mehrere Formate gelten als ODER, verschiedene Filter als UND. Query darf leer sein, wenn ein Filter gesetzt ist."
    ),
)
async def search_datasets(
    query: Annotated[str, Field(description="Suchbegriff, z. B. 'Bevölkerung Wien', 'Luftgüte', 'Vornamen'. Darf mit Filter leer sein.")] = "",
    limit: Annotated[int, Field(ge=1, le=50, description="Anzahl der Treffer (1-50).")] = 10,
    page: Annotated[int, Field(ge=0, description="Nullbasierte Seitenzahl fuer Paginierung.")] = 0,
    publisher: Annotated[str, Field(description="Herausgeber, z. B. 'Statistik Austria'. Schreibweise wird bei 0 Treffern automatisch korrigiert.")] = "",
    format: Annotated[str, Field(description="Dateiformat, z. B. 'CSV', 'XLSX' oder 'CSV,ZIP' (ODER).")] = "",
    category: Annotated[str, Field(description="Thema: ID (ENVI, TRAN, ...) oder deutscher Name (Umwelt, Verkehr, ...).")] = "",
    loadable_only: Annotated[bool, Field(description="Nur Datensaetze mit CSV/JSON/GeoJSON/Parquet/Excel/ZIP. Ignoriert, wenn format gesetzt ist.")] = False,
    sort: Annotated[
        Literal["relevance", "modified_desc", "modified_asc", "issued_desc"],
        Field(description="Sortierung. 'modified_desc' = zuletzt geaendert zuerst (viele Portale zeigen hier das Harvest-Datum)."),
    ] = "relevance",
) -> dict[str, Any]:
    try:
        facets = _build_facets(publisher, format, category, loadable_only)
    except ValueError as exc:
        return _error(str(exc))
    if not query.strip() and not facets:
        return _error("Bitte einen Suchbegriff oder mindestens einen Filter (publisher, format, category) angeben.")

    notes: list[str] = []
    async with _client() as client:
        try:
            result = await _search_request(client, query, limit, page, facets, sort)
            if result.get("count", 0) == 0 and (publisher.strip() or format.strip()):
                summary = (await _search_request(client, query, 1, 0, {}, "relevance")).get("facets", [])
                user_given = {k: v for k, v in facets.items() if (k == "publisher" and publisher.strip()) or (k == "format" and format.strip())}
                resolved, notes = _resolve_facet_values(user_given, summary)
                if any(facets.get(k) != v for k, v in resolved.items()):
                    facets = {**facets, **resolved}
                    result = await _search_request(client, query, limit, page, facets, sort)
        except (httpx.HTTPError, ValueError) as exc:
            return _error(f"Suche auf data.gv.at fehlgeschlagen: {exc}")

    datasets = []
    for entry in result.get("results", []):
        dists = [d for d in (entry.get("distributions") or []) if isinstance(d, dict)]
        formats = sorted({_label(d.get("format")) or "UNKNOWN" for d in dists})
        loadable = any(
            _effective_format(
                {"format": _label(d.get("format")), "url": _first_url(d.get("download_url")) or _first_url(d.get("access_url"))}
            )
            in _LOADABLE_FORMATS
            for d in dists
        )
        pub = entry.get("publisher")
        pub_name = pub.get("name") if isinstance(pub, dict) else str(pub or "")
        datasets.append(
            {
                "id": entry.get("id"),
                "title": _extract_localized(entry.get("title")),
                "description": _extract_localized(entry.get("description"))[:350],
                "publisher": pub_name,
                "modified": entry.get("modified"),
                "formats": formats,
                "has_loadable_data": loadable,
                "distribution_count": len(dists),
            }
        )

    out: dict[str, Any] = {"total_count": result.get("count", 0), "page": page, "datasets": datasets}
    if facets:
        out["applied_filters"] = facets
    if notes:
        out["filter_notes"] = notes
    overview = _facet_overview(result.get("facets", []))
    if overview and datasets:
        out["facets"] = overview
    return _jsonable(out)


@mcp.tool(
    name="get_dataset_details",
    title="Datensatz-Details und Distributionen",
    annotations=_READ_ONLY,
    description=(
        "Ruft Metadaten und Distributionen (url, format, license, byte_size) fuer eine dataset_id aus "
        "search_datasets ab. Distributionen sind nach Eignung sortiert (rank, role, usable, note); "
        "'recommended_url' ist die beste direkt ladbare Datendatei. Mit check_links=True werden die Links vorab "
        "per kurzer Range-Anfrage geprueft (Ergebnis je Distribution in 'link_status')."
    ),
)
async def get_dataset_details(
    dataset_id: Annotated[str, Field(description="Exakte Datensatz-ID aus search_datasets.")],
    check_links: Annotated[bool, Field(description="Prueft vorab per kurzer Anfrage, ob die Links erreichbar sind und eine Datei liefern (langsamer). Ergebnis je Distribution in 'link_status'.")] = False,
) -> dict[str, Any]:
    async with _client() as client:
        try:
            resp = await _get_with_retry(
                client, f"{SEARCH_API_BASE}/datasets/{quote(dataset_id, safe='')}", follow_redirects=True
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return _error(f"Datensatz '{dataset_id}' nicht gefunden. Exakte ID aus search_datasets verwenden.")
            return _error(f"Abruf von Datensatz '{dataset_id}' fehlgeschlagen: HTTP {exc.response.status_code}.")
        except (httpx.HTTPError, ValueError) as exc:
            return _error(f"Abruf von Datensatz '{dataset_id}' fehlgeschlagen: {exc}")

    ds = payload.get("result", payload)
    pub = ds.get("publisher")
    dists = _rank_distributions([_distribution(d) for d in ds.get("distributions") or [] if isinstance(d, dict)])
    links_checked = 0
    any_link_ok = False
    if check_links:
        links_checked, any_link_ok = await _check_distribution_links(dists)
    best = next((d for d in dists if d["usable"] and d["role"] == "data"), None)
    peers = [d for d in dists if best and d["usable"] and d["role"] == "data" and d["format_detected"] == best["format_detected"]]
    if best:
        peer_years = {d["year"] for d in peers if d.get("year")}
        if len(peers) > 1 and len(peer_years) > 1:
            peer_note = (
                f" Es gibt {len(peers) - 1} weitere gleichartige Dateien (z. B. andere Jahre, siehe 'year'); "
                "gewaehlt wurde die neueste. Passend zur Frage waehlen."
            )
        elif len(peers) > 1:
            peer_note = (
                f" Es gibt {len(peers) - 1} weitere gleichartige Dateien (andere Varianten, siehe 'title'); "
                "gewaehlt wurde die erste. Passend zur Frage waehlen."
            )
        else:
            peer_note = ""
        recommendation = f"Beste Datendatei: Rang {best['rank']} ({best['format_detected']})." + peer_note
    else:
        recommendation = "Keine direkt ladbare Datendatei (CSV/JSON/Parquet) vorhanden; Quelle und Links nennen statt raten."
        if check_links and links_checked and not any_link_ok:
            recommendation += " Alle geprueften Links sind tot oder liefern keine Datei; Datensatz-Seite auf data.gv.at nennen."
    lookup_tables: dict[str, str] = {}
    used_names: set[str] = set()
    for d in dists:
        if d["role"] == "dimension" and d["usable"] and d.get("url") and len(lookup_tables) < 10:
            lookup_tables[_lookup_table_name(d["url"], used_names)] = d["url"]
    if lookup_tables:
        recommendation += " Codes entschluesseln: Codelisten aus 'lookup_tables' per extra_tables in query_distribution_sql mitladen und joinen."
    out: dict[str, Any] = {
        "id": ds.get("id"),
        "title": _extract_localized(ds.get("title")),
        "description": _extract_localized(ds.get("description")),
        "publisher": pub.get("name") if isinstance(pub, dict) else pub,
        "modified": ds.get("modified"),
        "recommended_url": best["url"] if best else None,
        "recommendation": recommendation,
        "distributions": dists,
    }
    if lookup_tables:
        out["lookup_tables"] = lookup_tables
    if check_links:
        out["links_checked"] = links_checked
    return _jsonable(out)


@mcp.tool(
    name="inspect_distribution_schema",
    title="Schema einer Distribution pruefen",
    annotations=_READ_ONLY,
    description=(
        "Laedt eine CSV-, JSON-, Parquet-, Excel- oder ZIP-Distribution (ZIP mit einer Datei wird automatisch entpackt) in DuckDB und gibt Spaltennamen, "
        "Datentypen, Gesamtzeilenzahl sowie 5 Beispielzeilen zurueck (bei breiten Tabellen nur die ersten 20 Spalten, Texte gekuerzt; Hinweis in 'sample_note'). Immer VOR query_distribution_sql aufrufen. "
        "Die Datei wird 15 Minuten zwischengespeichert, die folgende Abfrage laedt nicht erneut."
    ),
)
async def inspect_distribution_schema(
    url: Annotated[str, Field(description="Direkte URL (Feld 'url' aus get_dataset_details) der CSV-, JSON-, Parquet-, Excel- oder ZIP-Datei.")],
    format_hint: Annotated[str, Field(description="Optionaler Format-Hinweis: 'csv', 'json' oder 'parquet'.")] = "",
    archive_member: Annotated[
        str, Field(description="Nur bei ZIP mit mehreren Dateien: Dateiname im Archiv (die Fehlermeldung listet ihn auf).")
    ] = "",
    sheet: Annotated[
        str, Field(description="Nur bei Excel: Blattname oder Nummer (1 = erstes). Standard: erstes Blatt mit Daten.")
    ] = "",
    skip_rows: Annotated[
        int, Field(ge=-1, le=1000, description="Nur bei Excel: nicht leere Zeilen ueber der Kopfzeile ueberspringen (Titelzeilen). -1 = automatisch erkennen.")
    ] = -1,
) -> dict[str, Any]:
    try:
        async with _cached_download(url, format_hint, archive_member, sheet, skip_rows) as (path, ext, info):
            result = await asyncio.to_thread(_inspect_sync, path, ext)
            return _jsonable({**result, "source": info} if info else result)
    except _SelectionNeeded as exc:
        return _error(str(exc), **exc.info)
    except Exception as exc:
        logger.error("Fehler in inspect_distribution_schema: %s", exc)
        return _error(_clean_error(exc))


@mcp.tool(
    name="query_distribution_sql",
    title="SQL auf Distribution ausfuehren",
    annotations=_READ_ONLY,
    description=(
        "Fuehrt eine schreibgeschuetzte DuckDB-SQL-Abfrage gegen die Tabelle 'dataset' einer "
        "CSV-, JSON-, Parquet-, Excel- oder ZIP-Distribution aus (max. 200 Ergebniszeilen). Nutze vorab "
        "inspect_distribution_schema, um die exakten Spaltennamen zu kennen. Ueber extra_tables lassen sich "
        "bis zu 3 weitere Dateien (z. B. Codelisten aus get_dataset_details) als eigene Tabellen laden und "
        "per JOIN mit 'dataset' verbinden."
    ),
)
async def query_distribution_sql(
    url: Annotated[str, Field(description="Direkte URL (Feld 'url' aus get_dataset_details) der Ressource.")],
    sql_query: Annotated[
        str,
        Field(description="Genau eine DuckDB-SELECT-Abfrage gegen die Tabelle 'dataset', z. B. 'SELECT * FROM dataset LIMIT 20'."),
    ],
    format_hint: Annotated[str, Field(description="Optionaler Format-Hinweis: 'csv', 'json' oder 'parquet'.")] = "",
    archive_member: Annotated[
        str, Field(description="Nur bei ZIP mit mehreren Dateien: Dateiname im Archiv (die Fehlermeldung listet ihn auf).")
    ] = "",
    sheet: Annotated[
        str, Field(description="Nur bei Excel: Blattname oder Nummer (1 = erstes). Standard: erstes Blatt mit Daten.")
    ] = "",
    skip_rows: Annotated[
        int, Field(ge=-1, le=1000, description="Nur bei Excel: nicht leere Zeilen ueber der Kopfzeile ueberspringen (Titelzeilen). -1 = automatisch erkennen.")
    ] = -1,
    extra_tables: Annotated[
        dict[str, str] | None,
        Field(
            description=(
                "Optional: weitere Dateien als zusaetzliche Tabellen, Name -> URL, hoechstens 3. Fuer Codelisten "
                "(role 'dimension' aus get_dataset_details), z. B. {\"bezirk\": \"https://.../..._C-WOHNBEZIRK-0.csv\"}. "
                "Danach per JOIN nutzbar. Nur einfache Dateien (CSV/JSON/Parquet/Excel/ZIP mit einer Datei), ohne archive_member/sheet."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    extras = extra_tables or {}
    if len(extras) > 3:
        return _error(f"extra_tables darf hoechstens 3 Eintraege haben, es sind {len(extras)}.")
    for name, extra_url in extras.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,29}", name or ""):
            return _error(f"Ungueltiger Tabellenname '{name}' in extra_tables: erlaubt ist ^[a-z][a-z0-9_]{{0,29}}$ (1-30 Zeichen).")
        if name == "dataset":
            return _error("Der Tabellenname 'dataset' in extra_tables ist reserviert (Haupttabelle).")
        if not (extra_url or "").strip():
            return _error(f"extra_tables['{name}']: URL darf nicht leer sein.")
    try:
        async with AsyncExitStack() as stack:
            path, ext, info = await stack.enter_async_context(
                _cached_download(url, format_hint, archive_member, sheet, skip_rows)
            )
            extra: list[tuple[str, Path, str]] = []
            for name, extra_url in extras.items():
                try:
                    epath, eext, _einfo = await stack.enter_async_context(_cached_download(extra_url))
                except _SelectionNeeded as exc:
                    return _error(f"Zusatztabelle '{name}': {exc}", **exc.info)
                except Exception as exc:
                    logger.error("Fehler in Zusatztabelle '%s': %s", name, exc)
                    return _error(f"Zusatztabelle '{name}': {_clean_error(exc)}")
                extra.append((name, epath, eext))
            result = await asyncio.to_thread(_query_sync, path, ext, sql_query, extra)
            out = _jsonable({**result, "source": info} if info else result)
            if extras:
                out["tables"] = {"dataset": url, **extras}
            return out
    except _SelectionNeeded as exc:
        return _error(str(exc), **exc.info)
    except Exception as exc:
        logger.error("Fehler in query_distribution_sql: %s", exc)
        return _error(_clean_error(exc))


@mcp.resource(
    "datagvat://dataset/{dataset_id}",
    description="Liefert den rohen DCAT-AP JSON-LD Metadatensatz aus dem Piveau hub-repo.",
    mime_type="application/ld+json",
)
async def get_dataset_rdf_resource(dataset_id: str) -> str:
    async with _client() as client:
        resp = await _get_with_retry(
            client,
            f"{REPO_API_BASE}/datasets/{quote(dataset_id, safe='')}",
            headers={"Accept": "application/ld+json"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text


def _selftest() -> int:
    """Prueft Importe und DuckDB-Sandbox ohne Netzwerk (z. B. zum Vorwaermen im Docker-Build)."""
    import python_calamine  # noqa: F401  (Excel-Unterstuetzung)
    import uvicorn  # noqa: F401  (HTTP-Betrieb)

    con = duckdb.connect(":memory:")
    con.execute("SELECT 1").fetchone()
    con.close()
    print("selftest ok", file=sys.stderr)
    return 0


MIN_TOKEN_LENGTH = 16


class _BearerAuthMiddleware:
    """ASGI-Middleware: verlangt 'Authorization: Bearer <Token>' auf HTTP-Anfragen (Lifespan bleibt unberuehrt)."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self._expected = token.encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # z. B. websocket: es gibt keine solche Route, ohne Auth nie durchlassen
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        supplied = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, credentials = value.partition(b" ")
                if scheme.lower() == b"bearer":
                    supplied = credentials.strip()
                break
        if hmac.compare_digest(supplied, self._expected):
            await self.app(scope, receive, send)
            return
        body = b'{"error": "Unauthorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _http_security_problem(host: str, token: str, allow_no_auth: bool) -> str | None:
    """Gibt den Grund zurueck, warum der HTTP-Start so nicht erlaubt ist (sonst None)."""
    if token and len(token) < MIN_TOKEN_LENGTH:
        return f"MCP_AUTH_TOKEN ist zu kurz (mindestens {MIN_TOKEN_LENGTH} Zeichen, z. B. 'openssl rand -hex 32')."
    if not token and not _is_loopback(host) and not allow_no_auth:
        return (
            f"HTTP auf {host} ohne Authentifizierung ist nicht erlaubt. MCP_AUTH_TOKEN setzen "
            "(oder bewusst MCP_ALLOW_NO_AUTH=1, nur in einem abgeschotteten Netz)."
        )
    return None


def _build_http_app(transport: str, token: str, host: str = "127.0.0.1") -> Any:
    kwargs = {"host": host} if MCP_V2 else {}
    app = mcp.streamable_http_app(**kwargs) if transport == "streamable-http" else mcp.sse_app(**kwargs)
    return _BearerAuthMiddleware(app, token) if token else app


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    if transport not in ("streamable-http", "sse"):
        sys.exit(f"Unbekannter MCP_TRANSPORT '{transport}' (erlaubt: stdio, streamable-http, sse).")
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8000"))
    token = os.getenv("MCP_AUTH_TOKEN", "").strip()
    problem = _http_security_problem(host, token, os.getenv("MCP_ALLOW_NO_AUTH") == "1")
    if problem:
        print(f"[FEHLER] {problem}", file=sys.stderr)
        sys.exit(2)
    if not token:
        logger.warning("HTTP ohne Authentifizierung auf %s:%s (nur lokal bzw. im abgeschotteten Netz verwenden).", host, port)
    import uvicorn

    uvicorn.run(_build_http_app(transport, token, host), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
