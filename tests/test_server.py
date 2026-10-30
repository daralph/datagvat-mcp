import asyncio
import json
import socket
import struct
import time
import zipfile
import re
import sys
import tempfile
from pathlib import Path

import duckdb
import httpcore
import httpx
import pytest
from mcp.types import CallToolResult

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import datagvat_mcp as m
from datagvat_mcp import (
    _create_sandboxed_duckdb,
    _validate_public_url,
    search_datasets,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _g(obj, camel: str):
    """Attribut lesen: mcp 1.x nutzt camelCase (isError), mcp 2.x snake_case (is_error)."""
    if hasattr(obj, camel):
        return getattr(obj, camel)
    return getattr(obj, re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower())


def _data(result) -> dict:
    """Tools liefern ein dict oder bei Fehlern ein CallToolResult(isError=True); beides als dict lesen."""
    if isinstance(result, CallToolResult):
        assert _g(result, "isError") is True
        return _g(result, "structuredContent")
    return result
PUBLIC = "http://93.184.216.34/daten.csv"  # IP-Literal: kein DNS noetig, aber oeffentlich


@pytest.fixture(autouse=True)
def clean_cache():
    m._cache_clear()
    yield
    m._cache_clear()


@pytest.fixture(autouse=True)
def delays(monkeypatch):
    """Ersetzt das Warten zwischen Wiederholungen durch eine Aufzeichnung (Tests schlafen nie)."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(m, "_sleep", fake_sleep)
    return recorded


@pytest.fixture
def transport(monkeypatch):
    """Setzt einen MockTransport; Test liefert den Handler und bekommt die aufgerufenen URLs zurueck."""
    calls: list[str] = []

    def install(handler):
        def wrapped(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return handler(request)

        monkeypatch.setattr(m, "_TRANSPORT", httpx.MockTransport(wrapped))
        return calls

    return install


def _csv_file(text: str, encoding: str = "utf-8") -> Path:
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(text.encode(encoding))
    return Path(tmp.name)


# ---------- Bestehende Kernpruefungen ----------


def test_ssrf_protection_blocks_private_ips() -> None:
    with pytest.raises(ValueError, match="nicht-oeffentliche IP-Adresse"):
        _validate_public_url("http://127.0.0.1:8000/secret.csv")

    with pytest.raises(ValueError, match="nicht-oeffentliche IP-Adresse"):
        _validate_public_url("http://169.254.169.254/latest/meta-data/")

    with pytest.raises(ValueError, match="Nur vollstaendige"):
        _validate_public_url("file:///etc/passwd")


def test_duckdb_sandbox_blocks_local_file_access() -> None:
    tmp_path = _csv_file("id;name\n1;Wien\n")
    try:
        con = _create_sandboxed_duckdb(tmp_path, ".csv")
        assert con.execute("SELECT COUNT(*) FROM dataset;").fetchone()[0] == 1

        with pytest.raises(duckdb.Error):
            con.execute("SELECT * FROM read_csv_auto('/etc/hosts');")
        con.close()
    finally:
        tmp_path.unlink(missing_ok=True)


def test_cp1252_and_semicolon_csv_parsing() -> None:
    tmp_path = _csv_file("Bezirk;Fläche_km2;Österreich\nInnere Stadt;2,87;Ja\nDöbling;24,94;Ja\n")
    try:
        con = _create_sandboxed_duckdb(tmp_path, ".csv")
        rows = con.execute('SELECT "Bezirk", "Österreich" FROM dataset ORDER BY 1;').fetchall()
        assert rows == [("Döbling", "Ja"), ("Innere Stadt", "Ja")]
        con.close()
    finally:
        tmp_path.unlink(missing_ok=True)


def test_csv_with_control_bytes_falls_back_to_lenient_parser() -> None:
    """Statistik-Austria-Codelisten enden mit Steuerzeichen; der strikte Sniffer scheitert, der lockere nicht."""
    raw = b"code;name;;en_name\r\n1;m\xc3\xa4nnlich;;male\r\n2;weiblich;;female\r\r\x00\n\x00"
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as fh:
        fh.write(raw)
    tmp_path = Path(fh.name)
    try:
        con = _create_sandboxed_duckdb(tmp_path, ".csv")
        assert con.execute("SELECT code, name FROM dataset ORDER BY 1").fetchall() == [(1, "m\u00e4nnlich"), (2, "weiblich")]
        con.close()
    finally:
        tmp_path.unlink(missing_ok=True)


def test_clean_error_hides_temp_paths_and_sql_excerpt() -> None:
    exc = ValueError('Error when sniffing file "/var/folders/ab/T/tmpx.csv".\nLINE 1: CREATE TABLE dataset AS SELECT\n^')
    text = m._clean_error(exc)
    assert "/var/folders" not in text and "LINE 1" not in text and "<Datei>" in text


async def test_get_dataset_details_404_gives_friendly_message(transport) -> None:
    transport(lambda req: httpx.Response(404, json={"status": "not found"}))
    out = _data(await m.get_dataset_details(dataset_id="gibt-es-nicht"))
    assert "nicht gefunden" in out["error"] and "search_datasets" in out["error"]
    assert "http" not in out["error"]


# ---------- Download, Encoding, SSRF ueber Weiterleitungen ----------


async def test_redirect_to_private_ip_is_blocked(transport) -> None:
    calls = transport(lambda req: httpx.Response(302, headers={"location": "http://127.0.0.1:8080/secret.csv"}))
    with pytest.raises(ValueError, match="nicht-oeffentliche IP-Adresse"):
        await m._download_to_temp(PUBLIC)
    assert calls == [PUBLIC], "Die interne Adresse darf nie angefragt werden"


async def test_relative_redirect_is_followed(transport) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/daten.csv":
            return httpx.Response(301, headers={"location": "/neu.csv"})
        return httpx.Response(200, content=b"a;b\n1;2\n")

    transport(handler)
    path, ext = await m._download_to_temp(PUBLIC)
    try:
        assert ext == ".csv" and path.read_text() == "a;b\n1;2\n"
    finally:
        path.unlink(missing_ok=True)


async def test_redirect_loop_is_cut_off(transport) -> None:
    transport(lambda req: httpx.Response(302, headers={"location": PUBLIC}))
    with pytest.raises(ValueError, match="Weiterleitungen"):
        await m._download_to_temp(PUBLIC)


async def test_download_size_limit(transport, monkeypatch) -> None:
    monkeypatch.setattr(m, "MAX_DOWNLOAD_BYTES", 100)
    transport(lambda req: httpx.Response(200, content=b"a;b\n" + b"1;2\n" * 1000))
    with pytest.raises(ValueError, match="Sicherheitslimit"):
        await m._download_to_temp(PUBLIC)


@pytest.mark.parametrize(
    "body, expected",
    [
        (b"PK\x03\x04" + bytes(100), "ZIP-Archiv ist beschaedigt"),
        (b"%PDF-1.7\n" + bytes(100), "PDF"),
        (b"  <!DOCTYPE html><html><body>404</body></html>", "HTML"),
        (b"<?xml version='1.0'?><a/>", "XML"),
    ],
)
async def test_unsupported_content_gives_clear_error(transport, body, expected) -> None:
    transport(lambda req: httpx.Response(200, content=body))
    with pytest.raises(ValueError, match=expected):
        await m._download_to_temp(PUBLIC)


async def test_cp1252_download_is_normalized_to_utf8(transport) -> None:
    transport(lambda req: httpx.Response(200, content="Bezirk;Fläche\nDöbling;24,94\n".encode("cp1252")))
    path, ext = await m._download_to_temp(PUBLIC)
    try:
        con = _create_sandboxed_duckdb(path, ext)
        assert con.execute('SELECT "Bezirk", "Fläche" FROM dataset').fetchall() == [("Döbling", "24,94")]
    finally:
        path.unlink(missing_ok=True)


async def test_utf8_bom_is_stripped(transport) -> None:
    transport(lambda req: httpx.Response(200, content=b"\xef\xbb\xbfid;name\n1;Wien\n"))
    path, ext = await m._download_to_temp(PUBLIC)
    try:
        con = _create_sandboxed_duckdb(path, ext)
        assert [d[0] for d in con.execute("DESCRIBE dataset").fetchall()] == ["id", "name"]
    finally:
        path.unlink(missing_ok=True)


async def test_parquet_is_detected_by_magic_bytes(transport) -> None:
    src = Path(tempfile.mkdtemp()) / "x.parquet"
    duckdb.connect().execute(f"COPY (SELECT 1 AS a, 'Wien' AS b) TO '{src}' (FORMAT parquet)")
    transport(lambda req: httpx.Response(200, content=src.read_bytes(), headers={"content-type": "application/octet-stream"}))
    path, ext = await m._download_to_temp("http://93.184.216.34/download?id=7")
    try:
        assert ext == ".parquet"
        assert _create_sandboxed_duckdb(path, ext).execute("SELECT b FROM dataset").fetchone() == ("Wien",)
    finally:
        path.unlink(missing_ok=True)


# ---------- SQL-Sandbox und Ergebnislimit ----------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "COPY dataset TO '/tmp/datagvat_x.csv'",
        "ATTACH '/tmp/datagvat_y.db' AS y",
        "INSTALL httpfs",
        "SET enable_external_access = true",
        "DROP TABLE dataset",
        "SELECT * FROM read_text('/etc/hosts')",
        "WITH x AS (SELECT 1) SELECT * FROM read_blob('/etc/hosts')",
        "SELECT * FROM glob('/etc/*')",
        "PRAGMA version",
        "",
    ],
)
def test_sql_attacks_are_rejected(sql: str) -> None:
    path = _csv_file("a;b\n1;2\n")
    try:
        with pytest.raises((ValueError, duckdb.Error)):
            m._query_sync(path, ".csv", sql)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("n, truncated, returned", [(10, False, 10), (200, False, 200), (201, True, 200), (1000, True, 200)])
def test_result_limit_and_truncation_flag(n: int, truncated: bool, returned: int) -> None:
    path = _csv_file("id;name\n" + "".join(f"{i};x\n" for i in range(n)))
    try:
        result = m._query_sync(path, ".csv", "SELECT * FROM dataset ORDER BY id;")
        assert result["returned_rows"] == returned
        assert result["truncated_at_limit"] is truncated
    finally:
        path.unlink(missing_ok=True)


def test_describe_and_summarize_are_allowed() -> None:
    path = _csv_file("a;b\n1;2\n")
    try:
        assert m._query_sync(path, ".csv", "DESCRIBE dataset")["returned_rows"] == 2
        assert m._query_sync(path, ".csv", "SUMMARIZE dataset")["returned_rows"] == 2
    finally:
        path.unlink(missing_ok=True)


def test_header_only_file_gives_clear_error() -> None:
    # So sieht die Statistik-Austria-Datei *_HEADER.csv aus: Zeilen passen nicht zur Kopfzeile
    path = _csv_file("code;name;en_name\nC-JAHR-0;Jahr\nC-WOHNBEZIRK-0;Bezirk\n")
    try:
        with pytest.raises(ValueError, match="0 Datenzeilen"):
            m._inspect_sync(path, ".csv")
    finally:
        path.unlink(missing_ok=True)


async def test_tools_work_without_optional_parameters(transport) -> None:
    transport(lambda req: httpx.Response(200, content=b"a;b\n1;2\n3;4\n"))
    out = _data(await m.query_distribution_sql(url=PUBLIC, sql_query="SELECT SUM(a) AS s FROM dataset"))
    assert out["results"] == [{"s": 4}]
    schema = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert schema["total_rows"] == 2


async def test_inspect_wide_table_limits_sample_columns(transport) -> None:
    n = 82
    kopf = ";".join(f"c{i}" for i in range(n))
    zeilen = "\n".join(";".join(str(r * n + i) for i in range(n)) for r in range(8))
    transport(lambda req: httpx.Response(200, content=(kopf + "\n" + zeilen + "\n").encode()))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert len(out["columns"]) == n, "Spaltenliste bleibt vollstaendig"
    assert len(out["sample_rows"]) == 5
    assert all(len(r) == m.SAMPLE_MAX_COLUMNS for r in out["sample_rows"])
    assert list(out["sample_rows"][0])[-1] == f"c{m.SAMPLE_MAX_COLUMNS - 1}"
    assert "20 von 82" in out["sample_note"] and "gekuerzt" not in out["sample_note"]


async def test_inspect_long_text_is_cut_and_noted(transport) -> None:
    lang = "x" * 500
    transport(lambda req: httpx.Response(200, content=f"a;b\n{lang};1\n".encode()))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert out["sample_rows"][0]["a"] == "x" * m.SAMPLE_MAX_CELL + "…"
    assert out["sample_rows"][0]["b"] == 1
    assert "gekuerzt" in out["sample_note"]


async def test_inspect_narrow_table_has_no_note(transport) -> None:
    transport(lambda req: httpx.Response(200, content=b"a;b\n1;2\n3;4\n"))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert "sample_note" not in out and out["sample_rows"] == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]


# ---------- Metadaten: URL- und Lizenzfelder gegen echte API-Antworten ----------


def test_helpers_first_url_label_license() -> None:
    assert m._first_url(["https://a", "https://b"]) == "https://a"
    assert m._first_url("https://a") == "https://a"
    assert m._first_url(None) is None and m._first_url([]) is None
    assert m._label({"id": "CSV", "label": "CSV"}) == "CSV"
    assert m._label({"resource": "http://x"}) == "http://x"
    assert m._license_name({"resource": "https://creativecommons.org/licenses/by/4.0/deed.de"})[0] == "CC BY 4.0"
    assert m._license_name({"resource": "https://creativecommons.org/licenses/by-nc-nd/4.0/"})[0] == "CC BY-NC-ND 4.0"
    assert m._license_name({"resource": "https://creativecommons.org/publicdomain/zero/1.0/"})[0] == "CC0 1.0"
    assert m._license_name(None) == (None, None)


async def test_get_dataset_details_with_recorded_api_response(transport) -> None:
    payload = (FIXTURES / "dataset_vornamen.json").read_bytes()
    calls = transport(lambda req: httpx.Response(200, content=payload, headers={"content-type": "application/json"}))
    out = _data(await m.get_dataset_details(dataset_id="603066f6-0f0a-3806-b394-f14b7d2cb437"))
    assert calls[0].endswith("/datasets/603066f6-0f0a-3806-b394-f14b7d2cb437")
    assert out["publisher"] == "Statistik Austria"
    urls = [d["url"] for d in out["distributions"]]
    assert "https://data.statistik.gv.at/data/OGDEXT_VORNAMEN_1.csv" in urls
    assert all(isinstance(u, str) for u in urls), "url muss ein String sein, keine Liste"


async def test_dataset_id_is_url_quoted(transport) -> None:
    calls = transport(lambda req: httpx.Response(200, json={"result": {"id": "x", "distributions": []}}))
    await m.get_dataset_details(dataset_id="../repo/x?y=1")
    assert "/datasets/..%2Frepo%2Fx%3Fy%3D1" in calls[0]


def test_distribution_with_license_object() -> None:
    d = m._distribution(
        {
            "id": "1",
            "title": {"de": "Bevölkerung"},
            "format": {"id": "CSV", "label": "CSV"},
            "license": {"resource": "https://creativecommons.org/licenses/by/4.0/"},
            "access_url": ["https://example.org/a.csv"],
            "byte_size": 12,
        }
    )
    assert d["license"] == "CC BY 4.0" and d["url"] == "https://example.org/a.csv" and d["byte_size"] == 12


# ---------- Download-Cache ----------

CSV_BODY = b"a;b\n1;2\n3;4\n"


async def test_cache_downloads_once_for_inspect_and_query(transport) -> None:
    calls = transport(lambda req: httpx.Response(200, content=CSV_BODY))
    await m.inspect_distribution_schema(url=PUBLIC)
    out = _data(await m.query_distribution_sql(url=PUBLIC, sql_query="SELECT SUM(a) AS s FROM dataset"))
    assert out["results"] == [{"s": 4}]
    assert len(calls) == 1


async def test_raw_download_is_shared_across_format_hints(transport) -> None:
    calls = transport(lambda req: httpx.Response(200, content=CSV_BODY))
    await m.inspect_distribution_schema(url=PUBLIC, format_hint="csv")
    await m.inspect_distribution_schema(url=PUBLIC, format_hint="")
    assert len(calls) == 1
    assert len(next(iter(m._cache.values())).prepared) == 2


async def test_cache_expires_after_ttl(transport, monkeypatch) -> None:
    calls = transport(lambda req: httpx.Response(200, content=CSV_BODY))
    monkeypatch.setattr(m, "CACHE_TTL_SECONDS", 0.0)
    await m.inspect_distribution_schema(url=PUBLIC)
    await m.inspect_distribution_schema(url=PUBLIC)
    assert len(calls) == 2
    assert not m._cache, "Mit TTL 0 darf nichts liegen bleiben"


async def test_failed_download_is_not_cached(transport) -> None:
    responses = [httpx.Response(404), httpx.Response(200, content=CSV_BODY)]
    calls = transport(lambda req: responses.pop(0))
    first = _data(await m.inspect_distribution_schema(url=PUBLIC))
    second = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert "error" in first and second["total_rows"] == 2
    assert len(calls) == 2


async def test_parallel_requests_share_one_download(transport) -> None:
    calls = transport(lambda req: httpx.Response(200, content=CSV_BODY))
    results = await asyncio.gather(
        *[m.query_distribution_sql(url=PUBLIC, sql_query="SELECT COUNT(*) AS n FROM dataset") for _ in range(5)]
    )
    assert all(_data(r)["results"] == [{"n": 2}] for r in results)
    assert len(calls) == 1


async def test_cache_evicts_least_recently_used_when_over_limit(transport, monkeypatch) -> None:
    transport(lambda req: httpx.Response(200, content=CSV_BODY))
    monkeypatch.setattr(m, "CACHE_MAX_BYTES", 60)  # Eintrag = Rohdatei + aufbereitete Datei = 24 Byte, zwei passen
    paths = {}
    for name in ("a", "b", "c"):
        async with m._cached_download(f"http://93.184.216.34/{name}.csv") as (path, _, _info):
            paths[name] = path
    assert not paths["a"].exists(), "aeltester Eintrag muss weichen"
    assert paths["b"].exists() and paths["c"].exists()
    assert len(m._cache) == 2


async def test_entry_in_use_is_never_evicted(transport, monkeypatch) -> None:
    transport(lambda req: httpx.Response(200, content=CSV_BODY))
    monkeypatch.setattr(m, "CACHE_MAX_BYTES", 1)
    async with m._cached_download("http://93.184.216.34/a.csv") as (path_a, _, _i):
        async with m._cached_download("http://93.184.216.34/b.csv") as (path_b, _, _i):
            assert path_a.exists() and path_b.exists()
        assert path_a.exists(), "a wird noch benutzt"
        assert not path_b.exists(), "b ist frei und ueber dem Limit"
    assert not path_a.exists()


def test_cache_clear_removes_files(transport) -> None:
    raw, prep = _csv_file("a;b\n1;2\n"), _csv_file("a;b\n1;2\n")
    m._cache["k"] = m._CacheEntry(raw, "", 0.0, 0.0, 8, {("",): m._Prepared(prep, ".csv", {}, 8)})
    m._cache_clear()
    assert not raw.exists() and not prep.exists() and not m._cache


# ---------- Bessere Distributionswahl ----------


def _dist(url, fmt="CSV", size=None, title="x"):
    return m._distribution(
        {"id": url, "title": {"de": title}, "format": {"id": fmt, "label": fmt} if fmt else None,
         "access_url": [url], "byte_size": size}
    )


@pytest.mark.parametrize(
    "url, role",
    [
        ("https://data.statistik.gv.at/data/OGDEXT_VORNAMEN_1.csv", "data"),
        ("https://data.statistik.gv.at/data/OGDEXT_VORNAMEN_1_HEADER.csv", "header"),
        ("https://data.statistik.gv.at/data/OGDEXT_VORNAMEN_1_C-JAHR-0.csv", "dimension"),
        ("https://data.wien.gv.at/export/luft.csv?x=_HEADER", "data"),
        ("", "data"),
    ],
)
def test_file_role_heuristic(url: str, role: str) -> None:
    assert m._file_role(url) == role


async def test_details_recommend_data_file_over_header_and_dimensions(transport) -> None:
    payload = (FIXTURES / "dataset_vornamen.json").read_bytes()
    transport(lambda req: httpx.Response(200, content=payload))
    out = _data(await m.get_dataset_details(dataset_id="603066f6-0f0a-3806-b394-f14b7d2cb437"))
    assert out["recommended_url"] == "https://data.statistik.gv.at/data/OGDEXT_VORNAMEN_1.csv"
    roles = [d["role"] for d in out["distributions"]]
    assert roles == ["data", "dimension", "dimension", "dimension", "header"]
    assert [d["rank"] for d in out["distributions"]] == [1, 2, 3, 4, 5]
    header = out["distributions"][-1]
    assert header["usable"] is False and "Spaltenbeschreibung" in header["note"]
    assert out["distributions"][1]["usable"] is True, "Codelisten bleiben abfragbar"


def test_ranking_prefers_parquet_then_csv_then_json_and_puts_unusable_last() -> None:
    ranked = m._rank_distributions(
        [
            _dist("https://x.at/a.pdf", "PDF"),
            _dist("https://x.at/a.json", "JSON"),
            _dist("https://x.at/a.csv", "CSV"),
            _dist("https://x.at/a.parquet", "PARQUET"),
            _dist("https://x.at/a.zip", "ZIP"),
            _dist("https://x.at/a.xlsx", "XLSX"),
        ]
    )
    assert [d["format_detected"] for d in ranked] == ["PARQUET", "CSV", "JSON", "XLSX", "ZIP", "PDF"]
    assert [d["usable"] for d in ranked] == [True, True, True, True, True, False]
    assert "sheet" in ranked[3]["note"] and "archive_member" in ranked[4]["note"]


@pytest.mark.parametrize(
    "fmt, url, detected, usable",
    [
        ("external", "https://x.at/daten.csv", "CSV", True),
        (None, "https://x.at/daten.json", "JSON", True),
        ("XLSX", "https://x.at/daten.xlsx", "XLSX", True),
        ("PDF", "https://x.at/daten.pdf", "PDF", False),
        ("WMS", "https://x.at/wms?service=WMS", "WMS", False),
        (None, "https://x.at/portal", None, False),
    ],
)
def test_effective_format_falls_back_to_url_suffix(fmt, url, detected, usable) -> None:
    d = m._assess_distribution(_dist(url, fmt))
    assert d["format_detected"] == detected and d["usable"] is usable
    if not usable:
        assert d["note"]


def test_too_large_file_is_not_usable() -> None:
    d = m._assess_distribution(_dist("https://x.at/gross.csv", "CSV", size=m.MAX_DOWNLOAD_BYTES + 1))
    assert d["usable"] is False and "MB" in d["note"]


async def test_no_loadable_data_gives_no_recommendation(transport) -> None:
    payload = {"result": {"id": "x", "distributions": [{"id": "1", "format": {"id": "PDF"}, "access_url": ["https://x.at/a.pdf"]}]}}
    transport(lambda req: httpx.Response(200, json=payload))
    out = _data(await m.get_dataset_details(dataset_id="x"))
    assert out["recommended_url"] is None and "Keine direkt ladbare" in out["recommendation"]


async def test_search_flags_loadable_data(transport) -> None:
    payload = {"result": {"count": 2, "results": [
        {"id": "1", "title": {"de": "A"}, "distributions": [{"format": {"id": "PDF"}, "access_url": ["https://x.at/a.pdf"]}]},
        {"id": "2", "title": {"de": "B"}, "distributions": [{"format": {"id": "external"}, "access_url": ["https://x.at/b.csv"]}]},
    ]}}
    transport(lambda req: httpx.Response(200, json=payload))
    out = _data(await m.search_datasets(query="x"))
    assert [d["has_loadable_data"] for d in out["datasets"]] == [False, True]


# ---------- ZIP-Archive ----------

import io
import zipfile


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def test_zip_with_single_csv_is_unpacked_and_cp1252_normalized(transport) -> None:
    body = _zip_bytes({"luftguete-2022.csv": "Messort;Wert\nSalzburg Süd;0,4\n".encode("cp1252")})
    transport(lambda req: httpx.Response(200, content=body, headers={"content-type": "application/x-zip-compressed"}))
    out = _data(await m.inspect_distribution_schema(url="http://93.184.216.34/luftguete-2022.zip"))
    assert out["total_rows"] == 1
    assert [c["name"] for c in out["columns"]] == ["Messort", "Wert"]
    assert out["sample_rows"][0]["Messort"] == "Salzburg Süd"
    assert out["source"]["selected_member"] == "luftguete-2022.csv"


async def test_zip_with_several_files_asks_for_selection_and_reuses_download(transport) -> None:
    body = _zip_bytes(
        {"2021.csv": b"a;b\n1;2\n", "2022.csv": b"a;b\n3;4\n5;6\n", "readme.pdf": b"%PDF", "__MACOSX/._2021.csv": b"x"}
    )
    url = "http://93.184.216.34/jahre.zip"
    calls = transport(lambda req: httpx.Response(200, content=body))
    first = _data(await m.inspect_distribution_schema(url=url))
    assert "archive_member" in first["error"]
    assert [x["name"] for x in first["archive_members"]] == ["2021.csv", "2022.csv", "readme.pdf"]
    assert [x["supported"] for x in first["archive_members"]] == [True, True, False]
    second = _data(await m.query_distribution_sql(url=url, sql_query="SELECT SUM(a) AS s FROM dataset", archive_member="2022.csv"))
    third = _data(await m.query_distribution_sql(url=url, sql_query="SELECT SUM(a) AS s FROM dataset", archive_member="2021.CSV"))
    assert second["results"] == [{"s": 8}] and third["results"] == [{"s": 1}]
    assert len(calls) == 1, "Archiv darf trotz mehrerer Dateien nur einmal geladen werden"


async def test_zip_unknown_member_lists_options(transport) -> None:
    transport(lambda req: httpx.Response(200, content=_zip_bytes({"a.csv": b"x;y\n1;2\n", "b.csv": b"x;y\n1;2\n"})))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC, archive_member="c.csv"))
    assert "nicht im Archiv" in out["error"] and len(out["archive_members"]) == 2


async def test_zip_without_supported_file(transport) -> None:
    transport(lambda req: httpx.Response(200, content=_zip_bytes({"karte.shp": b"x", "karte.dbf": b"y"})))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert "keine CSV" in out["error"] and "karte.shp" in out["error"]


async def test_zip_member_over_unpacked_limit_is_rejected(transport, monkeypatch) -> None:
    monkeypatch.setattr(m, "MAX_UNPACKED_BYTES", 1000)
    transport(lambda req: httpx.Response(200, content=_zip_bytes({"gross.csv": b"a;b\n" + b"1;2\n" * 5000})))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert "Limit" in out["error"]


async def test_zip_with_lying_header_cannot_unpack_more_than_declared(transport, monkeypatch) -> None:
    monkeypatch.setattr(m, "MAX_UNPACKED_BYTES", 1000)
    body = bytearray(_zip_bytes({"x.csv": b"a;b\n" + b"1;2\n" * 5000}))
    # file_size im zentralen Verzeichnis auf 10 faelschen (Zip-Bomb-Trick); zipfile liest dann hoechstens 10 Byte und meldet CRC-Fehler
    idx = body.rfind(b"PK\x01\x02")
    body[idx + 24 : idx + 28] = (10).to_bytes(4, "little")
    transport(lambda req: httpx.Response(200, content=bytes(body)))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert "error" in out


async def test_nested_zip_is_rejected(transport) -> None:
    inner = _zip_bytes({"a.csv": b"a;b\n1;2\n"})
    transport(lambda req: httpx.Response(200, content=_zip_bytes({"inner.zip": inner, "x.csv": b"a;b\n1;2\n"})))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC, archive_member="inner.zip"))
    assert "error" in out


# ---------- Excel ----------


def _xlsx_response() -> httpx.Response:
    return httpx.Response(200, content=(FIXTURES / "mini.xlsx").read_bytes())


async def test_xlsx_picks_first_sheet_with_data_and_detects_title_rows(transport) -> None:
    transport(lambda req: _xlsx_response())
    out = _data(await m.inspect_distribution_schema(url="http://93.184.216.34/daten.xlsx", sheet="Daten"))
    assert out["source"]["sheets"] == ["Titelblatt", "Daten"]
    assert out["source"]["skipped_rows"] == 1, "Titelzeile ueber der Kopfzeile (leere Zeilen zaehlen nicht)"
    assert [c["name"] for c in out["columns"]] == ["Messort", "Wert", "Anzahl", "Datum", "Aktiv"]
    assert out["total_rows"] == 3
    types = {c["name"]: c["type"] for c in out["columns"]}
    assert types["Wert"] == "DOUBLE" and types["Anzahl"] == "BIGINT", "Ganzzahlen bleiben Ganzzahlen"
    assert out["sample_rows"][0]["Messort"] == "Wien Hohe Warte"


async def test_xlsx_without_sheet_uses_first_nonempty_sheet(transport) -> None:
    transport(lambda req: _xlsx_response())
    out = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert out["source"]["selected_sheet"] == "Titelblatt"


@pytest.mark.parametrize("sheet", ["2", "daten", "Daten"])
async def test_xlsx_sheet_by_number_or_case_insensitive_name(transport, sheet) -> None:
    transport(lambda req: _xlsx_response())
    out = _data(await m.query_distribution_sql(url=PUBLIC, sql_query='SELECT "Messort" FROM dataset ORDER BY 1', sheet=sheet))
    assert [r["Messort"] for r in out["results"]] == ["Graz Süd", "Linz", "Wien Hohe Warte"]


async def test_xlsx_unknown_sheet_lists_sheets(transport) -> None:
    transport(lambda req: _xlsx_response())
    out = _data(await m.inspect_distribution_schema(url=PUBLIC, sheet="gibts nicht"))
    assert "nicht gefunden" in out["error"] and out["sheets"] == ["Titelblatt", "Daten"]


async def test_xlsx_explicit_skip_rows_overrides_auto(transport) -> None:
    transport(lambda req: _xlsx_response())
    out = _data(await m.inspect_distribution_schema(url=PUBLIC, sheet="Daten", skip_rows=0))
    assert out["source"]["skipped_rows"] == 0
    assert out["columns"][0]["name"] == "Messwerte 2024"


async def test_legacy_xls(transport) -> None:
    transport(lambda req: httpx.Response(200, content=(FIXTURES / "mini.xls").read_bytes()))
    out = _data(await m.query_distribution_sql(url=PUBLIC, sql_query='SELECT "Ort", "Wert" FROM dataset ORDER BY 1'))
    assert out["results"] == [{"Ort": "Döbling", "Wert": 1.5}, {"Ort": "Wieden", "Wert": 2}]


async def test_xlsx_inside_zip(transport) -> None:
    body = _zip_bytes({"daten.xlsx": (FIXTURES / "mini.xlsx").read_bytes()})
    transport(lambda req: httpx.Response(200, content=body))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC, sheet="Daten"))
    assert out["source"]["selected_member"] == "daten.xlsx" and out["total_rows"] == 3


async def test_xlsx_zip_bomb_guard(transport, monkeypatch) -> None:
    monkeypatch.setattr(m, "MAX_UNPACKED_BYTES", 1000)
    transport(lambda req: _xlsx_response())
    out = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert "zu gross" in out["error"]


async def test_broken_excel_gives_clear_error(transport) -> None:
    transport(lambda req: httpx.Response(200, content=b"\xd0\xcf\x11\xe0" + bytes(600)))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert "Excel" in out["error"]


def test_header_index_heuristic() -> None:
    assert m._header_index([["Titel", ""], ["a", "b", "c"], ["1", "2", "3"]]) == 1
    assert m._header_index([["a", "b", "c"], ["1", "2", "3"]]) == 0
    assert m._header_index([["nur eine Spalte"], ["x"]]) == 0
    assert m._header_index([]) == 0


def test_excel_cell_formatting() -> None:
    import datetime

    assert m._excel_cell(3.0) == "3" and m._excel_cell(2.5) == "2.5" and m._excel_cell(None) == ""
    assert m._excel_cell(True) == "true"
    assert m._excel_cell(datetime.date(2024, 1, 31)) == "2024-01-31"
    assert m._excel_cell(datetime.datetime(2024, 3, 1, 13, 30)) == "2024-03-01 13:30:00"


def test_year_hint_prefers_newest_among_equal_distributions() -> None:
    ranked = m._rank_distributions(
        [_dist(f"https://x.at/ogd/e768cb15-ab7c/luftguete-{y}.zip", "ZIP") for y in (2017, 2022, 2019)]
        + [_dist("https://x.at/aktuell.zip", "ZIP", title="Messwerte 2023 und 2024")]
    )
    assert [d["year"] for d in ranked] == [2024, 2022, 2019, 2017]


@pytest.mark.parametrize("stem, year", [("daten_2019", 2019), ("OGDEXT_VORNAMEN_1", None), ("a2019b", None), ("bev-1999-2003", 2003)])
def test_year_hint_only_matches_standalone_years(stem: str, year) -> None:
    assert m._year_hint({"url": f"https://x.at/{stem}.csv", "title": ""}) == year


async def test_details_mention_equal_peers(transport) -> None:
    payload = {"result": {"id": "x", "distributions": [
        {"id": str(y), "format": {"id": "ZIP"}, "access_url": [f"https://x.at/l-{y}.zip"]} for y in (2020, 2021, 2022)
    ]}}
    transport(lambda req: httpx.Response(200, json=payload))
    out = _data(await m.get_dataset_details(dataset_id="x"))
    assert out["recommended_url"] == "https://x.at/l-2022.zip"
    assert "2 weitere gleichartige" in out["recommendation"]


# ---------- Link-Pruefung (check_links) ----------


async def test_details_without_check_links_has_no_link_status(transport) -> None:
    calls = transport(lambda req: httpx.Response(200, json={"result": {"id": "x", "distributions": [
        {"id": "1", "format": {"id": "CSV"}, "access_url": [PUBLIC]},
    ]}}))
    out = _data(await m.get_dataset_details(dataset_id="x"))
    assert "links_checked" not in out
    assert all("link_status" not in d for d in out["distributions"])
    assert len(calls) == 1, "ohne check_links darf kein Distributions-Link angefragt werden"


async def test_check_links_reports_ok_for_csv_file(transport) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/datasets/" in req.url.path:
            return httpx.Response(200, json={"result": {"id": "x", "distributions": [
                {"id": "1", "format": {"id": "CSV"}, "access_url": [PUBLIC]},
            ]}})
        return httpx.Response(200, content=b"a;b\n1;2\n")

    transport(handler)
    out = _data(await m.get_dataset_details(dataset_id="x", check_links=True))
    d = out["distributions"][0]
    assert d["link_status"] == "ok" and d["usable"] is True
    assert out["links_checked"] == 1 and out["recommended_url"] == PUBLIC


async def test_check_links_marks_dead_link_and_recommends_next_usable(transport) -> None:
    dead, good = "http://93.184.216.34/dead.csv", "http://93.184.216.34/good.csv"

    def handler(req: httpx.Request) -> httpx.Response:
        if "/datasets/" in req.url.path:
            return httpx.Response(200, json={"result": {"id": "x", "distributions": [
                {"id": "1", "format": {"id": "CSV"}, "access_url": [dead]},
                {"id": "2", "format": {"id": "CSV"}, "access_url": [good]},
            ]}})
        if req.url.path == "/dead.csv":
            return httpx.Response(404)
        return httpx.Response(200, content=b"a;b\n1;2\n")

    transport(handler)
    out = _data(await m.get_dataset_details(dataset_id="x", check_links=True))
    by_url = {d["url"]: d for d in out["distributions"]}
    assert by_url[dead]["link_status"] == "tot (HTTP 404)"
    assert by_url[dead]["usable"] is False and "tot (HTTP 404)" in by_url[dead]["note"]
    assert by_url[good]["link_status"] == "ok" and by_url[good]["usable"] is True
    assert out["recommended_url"] == good and out["links_checked"] == 2


async def test_check_links_detects_redirect_to_html_landing_page(transport) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/datasets/" in req.url.path:
            return httpx.Response(200, json={"result": {"id": "x", "distributions": [
                {"id": "1", "format": {"id": "CSV"}, "access_url": [PUBLIC]},
            ]}})
        if req.url.path == "/daten.csv":
            return httpx.Response(302, headers={"location": "http://93.184.216.34/"})
        return httpx.Response(200, content=b"<!DOCTYPE html><html><body>Portal</body></html>")

    transport(handler)
    out = _data(await m.get_dataset_details(dataset_id="x", check_links=True))
    d = out["distributions"][0]
    assert d["link_status"].startswith("liefert keine Datei: HTML-Seite")
    assert d["usable"] is False and out["recommended_url"] is None
    assert "Alle geprueften Links sind tot oder liefern keine Datei" in out["recommendation"]


async def test_check_links_blocks_redirect_to_private_ip(transport) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/datasets/" in req.url.path:
            return httpx.Response(200, json={"result": {"id": "x", "distributions": [
                {"id": "1", "format": {"id": "CSV"}, "access_url": [PUBLIC]},
            ]}})
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8080/secret.csv"})

    calls = transport(handler)
    out = _data(await m.get_dataset_details(dataset_id="x", check_links=True))
    d = out["distributions"][0]
    assert d["link_status"] == "gesperrt (keine oeffentliche Adresse)" and d["usable"] is False
    assert all("127.0.0.1" not in c for c in calls), "Die interne Adresse darf nie angefragt werden"


async def test_check_links_sends_range_header(transport) -> None:
    ranges: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "/datasets/" in req.url.path:
            return httpx.Response(200, json={"result": {"id": "x", "distributions": [
                {"id": "1", "format": {"id": "CSV"}, "access_url": [PUBLIC]},
            ]}})
        ranges.append(req.headers.get("range"))
        return httpx.Response(200, content=b"a;b\n1;2\n")

    transport(handler)
    await m.get_dataset_details(dataset_id="x", check_links=True)
    assert ranges == ["bytes=0-4095"]



# ---------- MCP-Protokoll: Annotations, strukturierte Antworten, isError ----------


def _session():
    """In-Memory-MCP-Client gegen den echten Server (als async-with im Test, nicht als Fixture: anyio-Scopes)."""
    if m.MCP_V2:
        from mcp.client.client import Client

        return Client(m.mcp)
    from mcp.shared.memory import create_connected_server_and_client_session

    return create_connected_server_and_client_session(m.mcp._mcp_server)


async def test_all_tools_are_annotated_read_only_with_title() -> None:
    async with _session() as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == {
            "search_datasets", "get_dataset_details", "inspect_distribution_schema", "query_distribution_sql",
        }
        for tool in tools:
            assert tool.title, tool.name
            a = tool.annotations
            assert (_g(a, "readOnlyHint"), _g(a, "destructiveHint"), _g(a, "idempotentHint"), _g(a, "openWorldHint")) == (True, False, True, True)
            schema = _g(tool, "outputSchema")
        assert schema and schema["type"] == "object"


async def test_success_is_structured_and_text(transport) -> None:
    transport(lambda req: httpx.Response(200, content=CSV_BODY))
    async with _session() as client:
        res = await client.call_tool("query_distribution_sql", {"url": PUBLIC, "sql_query": "SELECT SUM(a) AS s FROM dataset"})
        assert _g(res, "isError") is False
        assert _g(res, "structuredContent")["results"] == [{"s": 4}]
        assert json.loads(res.content[0].text)["results"] == [{"s": 4}]


async def test_failure_sets_is_error_and_keeps_details(transport) -> None:
    transport(lambda req: httpx.Response(200, content=_zip_bytes({"a.csv": b"x;y\n1;2\n", "b.csv": b"x;y\n1;2\n"})))
    async with _session() as client:
        res = await client.call_tool("inspect_distribution_schema", {"url": PUBLIC})
        assert _g(res, "isError") is True
        assert "archive_member" in _g(res, "structuredContent")["error"]
        assert len(_g(res, "structuredContent")["archive_members"]) == 2
        assert "archive_members" in res.content[0].text


async def test_ssrf_error_is_flagged() -> None:
    async with _session() as client:
        res = await client.call_tool("inspect_distribution_schema", {"url": "http://127.0.0.1:1/x.csv"})
        assert _g(res, "isError") is True and "nicht-oeffentliche" in _g(res, "structuredContent")["error"]


def test_results_are_strict_json_safe() -> None:
    import datetime, decimal

    out = m._jsonable({"n": float("nan"), "i": float("inf"), "d": decimal.Decimal("1.50"), "t": datetime.date(2024, 1, 2), "ö": "Ä"})
    assert out == {"n": None, "i": None, "d": "1.50", "t": "2024-01-02", "ö": "Ä"}
    json.dumps(out, allow_nan=False)


async def test_nan_from_sql_does_not_break_the_response(transport) -> None:
    transport(lambda req: httpx.Response(200, content=CSV_BODY))
    out = _data(await m.query_distribution_sql(url=PUBLIC, sql_query="SELECT 'NaN'::DOUBLE AS x, 1.50::DECIMAL(4,2) AS d"))
    assert out["results"] == [{"x": None, "d": "1.50"}]


# ---------- Suchfilter, Sortierung, Facetten ----------


def _search_payload(count=1, facets=None, title="Luftgüte"):
    return {"result": {"count": count, "facets": facets or [], "results": [
        {"id": "1", "title": {"de": title}, "publisher": {"name": "Land Salzburg"},
         "distributions": [{"format": {"id": "CSV"}, "access_url": ["https://x.at/a.csv"]}]}
    ] if count else []}}


def _facet(facet_id, items):
    return {"id": facet_id, "items": [{"id": i, "title": i, "count": c} for i, c in items]}


async def test_search_sends_facets_sort_and_omits_empty_query(transport) -> None:
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=_search_payload())

    transport(handler)
    out = _data(await m.search_datasets(publisher="Land Salzburg", format="CSV, zip", category="Umwelt", sort="modified_desc"))
    params = seen[0]
    assert "q" not in params and params["sort"] == "modified+desc" and params["filter"] == "dataset"
    assert json.loads(params["facets"]) == {"publisher": ["Land Salzburg"], "format": ["CSV", "zip"], "categories": ["ENVI"]}
    assert out["applied_filters"]["categories"] == ["ENVI"]


async def test_search_without_query_and_filter_is_an_error() -> None:
    res = await m.search_datasets()
    assert isinstance(res, CallToolResult) and _g(res, "isError")
    assert "Suchbegriff" in _g(res, "structuredContent")["error"]


@pytest.mark.parametrize("value, expected", [("ENVI", "ENVI"), ("envi", "ENVI"), ("Verkehr", "TRAN"), ("bildung", "EDUC"), ("gesundheit", "HEAL")])
def test_category_resolution(value, expected) -> None:
    assert m._resolve_category(value) == expected


@pytest.mark.parametrize("value", ["", "xyz", "und"])  # '' / unbekannt / mehrdeutig
def test_category_resolution_errors_list_valid_values(value) -> None:
    with pytest.raises(ValueError, match="ENVI = Umwelt"):
        m._resolve_category(value)


async def test_unknown_category_returns_error_with_options() -> None:
    res = await m.search_datasets(query="x", category="Kochen")
    assert _g(res, "isError") and "TRAN = Verkehr" in _g(res, "structuredContent")["error"]


async def test_loadable_only_and_explicit_format(transport) -> None:
    seen = []
    transport(lambda req: (seen.append(json.loads(req.url.params["facets"])), httpx.Response(200, json=_search_payload()))[1])
    await m.search_datasets(query="x", loadable_only=True)
    await m.search_datasets(query="x", loadable_only=True, format="XLSX")
    assert "CSV" in seen[0]["format"] and "ZIP" in seen[0]["format"] and "PDF" not in seen[0]["format"]
    assert seen[1]["format"] == ["XLSX"], "explizites format schlaegt loadable_only"


async def test_publisher_spelling_is_corrected_when_no_hits(transport) -> None:
    seen = []

    def handler(req):
        facets = json.loads(req.url.params.get("facets", "{}"))
        seen.append(facets)
        if facets.get("publisher") == ["Land Salzburg"]:
            return httpx.Response(200, json=_search_payload(count=3))
        if facets:
            return httpx.Response(200, json=_search_payload(count=0))
        return httpx.Response(200, json=_search_payload(count=9, facets=[
            _facet("publisher", [("Land Salzburg", 5), ("Stadt Wien", 4)]), _facet("format", [("CSV", 7), ("ZIP", 2)])]))

    transport(handler)
    out = _data(await m.search_datasets(query="Luftgüte", publisher="land salzburg", format="csv"))
    assert out["total_count"] == 3
    assert out["applied_filters"] == {"publisher": ["Land Salzburg"], "format": ["CSV"]}
    assert any("'land salzburg' wurde als 'Land Salzburg'" in n for n in out["filter_notes"])
    assert len(seen) == 3


async def test_publisher_unique_partial_match(transport) -> None:
    def handler(req):
        facets = json.loads(req.url.params.get("facets", "{}"))
        if facets.get("publisher") == ["Stadt Wien"]:
            return httpx.Response(200, json=_search_payload(count=2))
        if facets:
            return httpx.Response(200, json=_search_payload(count=0))
        return httpx.Response(200, json=_search_payload(count=9, facets=[_facet("publisher", [("Stadt Wien", 5), ("Land Tirol", 4)])]))

    transport(handler)
    out = _data(await m.search_datasets(query="x", publisher="wien"))
    assert out["total_count"] == 2 and out["applied_filters"]["publisher"] == ["Stadt Wien"]


async def test_ambiguous_publisher_returns_candidates_without_guessing(transport) -> None:
    calls = transport(lambda req: httpx.Response(200, json=(
        _search_payload(count=0) if "facets" in req.url.params else _search_payload(count=9, facets=[
            _facet("publisher", [("Land Salzburg", 5), ("Stadt Salzburg", 4), ("Land Tirol", 1)])]))))
    out = _data(await m.search_datasets(query="x", publisher="Salzburg"))
    assert out["total_count"] == 0
    note = " ".join(out["filter_notes"])
    assert "nicht eindeutig" in note and "Land Salzburg" in note and "Stadt Salzburg" in note and "Land Tirol" not in note
    assert len(calls) == 2, "keine geratene dritte Suche"


async def test_unknown_publisher_lists_frequent_values(transport) -> None:
    transport(lambda req: httpx.Response(200, json=(
        _search_payload(count=0) if "facets" in req.url.params else _search_payload(count=9, facets=[
            _facet("publisher", [("Stadt Wien", 5), ("Land Tirol", 4)])]))))
    out = _data(await m.search_datasets(query="x", publisher="Gemeinde Nirgendwo"))
    assert "kommt in dieser Suche nicht vor" in out["filter_notes"][0] and "Stadt Wien" in out["filter_notes"][0]


async def test_loadable_only_list_is_not_part_of_spelling_correction(transport) -> None:
    def handler(req):
        facets = json.loads(req.url.params.get("facets", "{}"))
        if facets.get("publisher") == ["Land Salzburg"]:
            return httpx.Response(200, json=_search_payload(count=3))
        if facets:
            return httpx.Response(200, json=_search_payload(count=0))
        return httpx.Response(200, json=_search_payload(count=9, facets=[_facet("publisher", [("Land Salzburg", 5)]), _facet("format", [("CSV", 7)])]))

    transport(handler)
    out = _data(await m.search_datasets(query="x", publisher="salzburg", loadable_only=True))
    assert out["total_count"] == 3
    assert out["filter_notes"] == ["publisher 'salzburg' wurde als 'Land Salzburg' verwendet."]
    assert "GEOJSON" in out["applied_filters"]["format"], "die Formatliste bleibt unveraendert"


async def test_no_fallback_requests_when_hits_exist(transport) -> None:
    calls = transport(lambda req: httpx.Response(200, json=_search_payload(count=4)))
    await m.search_datasets(query="x", publisher="Land Salzburg")
    assert len(calls) == 1


async def test_facet_overview_is_compact_with_category_labels(transport) -> None:
    facets = [
        _facet("publisher", [(f"P{i}", 100 - i) for i in range(20)] + [("Leer", 0)]),
        _facet("format", [("CSV", 5), ("PDF", 9)]),
        _facet("categories", [("ENVI", 7)]),
        _facet("license", [("x", 1)]),
    ]
    transport(lambda req: httpx.Response(200, json=_search_payload(facets=facets)))
    out = _data(await m.search_datasets(query="x"))
    assert len(out["facets"]["publisher"]) == 8 and out["facets"]["publisher"][0] == {"value": "P0", "count": 100}
    assert [f["value"] for f in out["facets"]["format"]] == ["PDF", "CSV"]
    assert out["facets"]["categories"] == [{"value": "ENVI", "count": 7, "label": "Umwelt"}]
    assert "license" not in out["facets"]


async def test_search_http_error_is_flagged(transport) -> None:
    transport(lambda req: httpx.Response(500))
    res = await m.search_datasets(query="x")
    assert _g(res, "isError") and "fehlgeschlagen" in _g(res, "structuredContent")["error"]


# ---------- Wiederholungen bei 429/5xx und Netzwerkfehlern ----------


def _sequence(*responses):
    """Handler, der nacheinander die angegebenen Antworten (oder Exceptions) liefert."""
    queue = list(responses)

    def handler(req):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


async def test_search_retries_503_with_exponential_backoff(transport, delays) -> None:
    calls = transport(_sequence(httpx.Response(503), httpx.Response(503), httpx.Response(200, json=_search_payload())))
    out = _data(await m.search_datasets(query="x"))
    assert out["total_count"] == 1 and len(calls) == 3
    assert delays == [0.5, 1.0]


async def test_retry_after_header_is_honoured_and_capped(transport, delays) -> None:
    transport(_sequence(
        httpx.Response(429, headers={"retry-after": "2"}),
        httpx.Response(429, headers={"retry-after": "3600"}),
        httpx.Response(200, json=_search_payload()),
    ))
    await m.search_datasets(query="x")
    assert delays == [2.0, 10.0]


async def test_retry_after_http_date_falls_back_to_backoff(transport, delays) -> None:
    transport(_sequence(httpx.Response(503, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}), httpx.Response(200, json=_search_payload())))
    await m.search_datasets(query="x")
    assert delays == [0.5]


async def test_retries_are_exhausted_then_error_is_reported(transport, delays) -> None:
    calls = transport(lambda req: httpx.Response(503))
    res = await m.search_datasets(query="x")
    assert _g(res, "isError") and "503" in _g(res, "structuredContent")["error"]
    assert len(calls) == m.RETRY_ATTEMPTS + 1 == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
async def test_other_errors_are_not_retried(transport, delays, status) -> None:
    calls = transport(lambda req: httpx.Response(status))
    res = await m.search_datasets(query="x")
    assert _g(res, "isError") and len(calls) == 1 and delays == []


async def test_network_error_is_retried(transport, delays) -> None:
    calls = transport(_sequence(httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), httpx.Response(200, json=_search_payload())))
    out = _data(await m.search_datasets(query="x"))
    assert out["total_count"] == 1 and len(calls) == 3


async def test_persistent_network_error_is_reported(transport) -> None:
    transport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("kein Netz")))
    res = await m.search_datasets(query="x")
    assert _g(res, "isError") and "fehlgeschlagen" in _g(res, "structuredContent")["error"]


async def test_retries_can_be_disabled(transport, monkeypatch) -> None:
    monkeypatch.setattr(m, "RETRY_ATTEMPTS", 0)
    calls = transport(lambda req: httpx.Response(503))
    res = await m.search_datasets(query="x")
    assert _g(res, "isError") and len(calls) == 1


async def test_details_are_retried(transport, delays) -> None:
    calls = transport(_sequence(httpx.Response(502), httpx.Response(200, json={"result": {"id": "x", "distributions": []}})))
    out = _data(await m.get_dataset_details(dataset_id="x"))
    assert out["id"] == "x" and len(calls) == 2


async def test_download_is_retried_and_restarts_cleanly(transport, delays) -> None:
    calls = transport(_sequence(httpx.Response(503, headers={"retry-after": "1"}), httpx.Response(200, content=CSV_BODY)))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert out["total_rows"] == 2 and len(calls) == 2 and delays == [1.0]


async def test_download_network_error_midway_is_retried(transport) -> None:
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a;b\n1;"
            raise httpx.ReadError("Verbindung getrennt")

    calls = transport(_sequence(httpx.Response(200, stream=Broken()), httpx.Response(200, content=CSV_BODY)))
    out = _data(await m.inspect_distribution_schema(url=PUBLIC))
    assert out["total_rows"] == 2, "Es darf kein halbes erstes Stueck uebrig bleiben"
    assert len(calls) == 2


async def test_download_503_on_last_attempt_is_an_error(transport) -> None:
    calls = transport(lambda req: httpx.Response(503))
    res = await m.inspect_distribution_schema(url=PUBLIC)
    assert _g(res, "isError") and len(calls) == 3


async def test_redirect_hops_retry_independently(transport) -> None:
    def handler(req):
        if req.url.path == "/start":
            return httpx.Response(302, headers={"location": "/ziel.csv"})
        return responses.pop(0)

    responses = [httpx.Response(503), httpx.Response(200, content=CSV_BODY)]
    transport(handler)
    out = _data(await m.inspect_distribution_schema(url="http://93.184.216.34/start"))
    assert out["total_rows"] == 2


# ---------- Bearer-Auth und Start-Schutz fuer HTTP ----------

TOKEN = "t" * 32


async def _ok_app(scope, receive, send):
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})


def _auth_client(token: str = TOKEN) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=m._BearerAuthMiddleware(_ok_app, token)), base_url="http://test")


async def test_bearer_middleware_accepts_correct_token() -> None:
    async with _auth_client() as c:
        assert (await c.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 200
        assert (await c.post("/mcp", headers={"authorization": f"bearer {TOKEN}"})).status_code == 200, "Schema ist case-insensitive"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer falsch"}, {"Authorization": f"Basic {TOKEN}"}, {"Authorization": TOKEN}, {"Authorization": "Bearer "}, {"Authorization": f"Bearer {TOKEN}x"}],
)
async def test_bearer_middleware_rejects_everything_else(headers) -> None:
    async with _auth_client() as c:
        res = await c.post("/mcp", headers=headers)
    assert res.status_code == 401 and res.headers["www-authenticate"] == "Bearer"
    assert res.json() == {"error": "Unauthorized"}


async def test_bearer_middleware_does_not_leak_token_in_response() -> None:
    async with _auth_client() as c:
        res = await c.get("/mcp", headers={"Authorization": "Bearer falsch"})
    assert TOKEN not in res.text and TOKEN not in str(res.headers)


async def test_bearer_middleware_passes_lifespan_through() -> None:
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    await m._BearerAuthMiddleware(app, TOKEN)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


@pytest.mark.parametrize(
    "host, token, allow, ok",
    [
        ("127.0.0.1", "", False, True),
        ("localhost", "", False, True),
        ("::1", "", False, True),
        ("0.0.0.0", "", False, False),
        ("192.168.1.5", "", False, False),
        ("mcp-datagvat", "", False, False),
        ("0.0.0.0", "", True, True),
        ("0.0.0.0", TOKEN, False, True),
        ("127.0.0.1", "kurz", False, False),
        ("0.0.0.0", "kurz", True, False),
    ],
)
def test_http_security_policy(host, token, allow, ok) -> None:
    assert (m._http_security_problem(host, token, allow) is None) is ok


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(env_extra: dict[str, str]):
    import os, subprocess

    env = {**os.environ, "MCP_TRANSPORT": "streamable-http", **env_extra}
    return subprocess.Popen([sys.executable, str(Path(m.__file__))], env=env, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)


async def _wait_for_port(port: int, proc) -> None:
    import socket

    for _ in range(100):
        if proc.poll() is not None:
            raise AssertionError(f"Server beendet: {proc.stderr.read().decode()[-500:]}")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        await asyncio.sleep(0.1)
    raise AssertionError("Server startet nicht")


async def test_http_server_end_to_end_with_token() -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    port = _free_port()
    proc = _start_server({"MCP_HOST": "127.0.0.1", "MCP_PORT": str(port), "MCP_AUTH_TOKEN": TOKEN})
    try:
        await _wait_for_port(port, proc)
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient() as c:
            assert (await c.post(url, json={})).status_code == 401
            assert (await c.post(url, json={}, headers={"Authorization": "Bearer nope"})).status_code == 401
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as authed:
            async with streamable_http_client(url, http_client=authed) as streams:  # 1.x: 3 Werte, 2.x: 2 Werte
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    names = {t.name for t in (await session.list_tools()).tools}
        assert "search_datasets" in names
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_server_refuses_public_bind_without_token() -> None:
    import os, subprocess

    env = {**os.environ, "MCP_TRANSPORT": "streamable-http", "MCP_HOST": "0.0.0.0", "MCP_PORT": str(_free_port())}
    env.pop("MCP_AUTH_TOKEN", None)
    env.pop("MCP_ALLOW_NO_AUTH", None)
    res = subprocess.run([sys.executable, str(Path(m.__file__))], env=env, capture_output=True, timeout=60)
    assert res.returncode == 2 and b"MCP_AUTH_TOKEN" in res.stderr


# ---------- Live (nur mit: pytest -m live) ----------


@pytest.mark.live
async def test_live_piveau_search() -> None:
    payload = _data(await search_datasets(query="Wien", limit=2, page=0))
    assert payload["total_count"] > 0
    assert len(payload["datasets"]) > 0


@pytest.mark.live
async def test_live_vornamen_header_file_is_reported_as_not_data() -> None:
    out = _data(await m.inspect_distribution_schema(url="https://data.statistik.gv.at/data/OGDEXT_VORNAMEN_1_HEADER.csv"))
    assert "0 Datenzeilen" in out["error"]


@pytest.mark.live
async def test_live_salzburg_luftguete_zip() -> None:
    url = "https://www.salzburg.gv.at/ogd/e768cb15-ab7c-4317-a072-94b8ab05cf7f/luftguete-2022.zip"
    out = _data(await m.query_distribution_sql(url=url, sql_query="SELECT COUNT(DISTINCT \"Messort\") AS orte FROM dataset"))
    assert out["results"][0]["orte"] > 3
    assert out["source"]["selected_member"].endswith(".csv")


@pytest.mark.live
async def test_live_search_filters() -> None:
    out = _data(await m.search_datasets(query="Luftgüte", format="csv", publisher="land salzburg", limit=3))
    assert out["total_count"] > 0 and out["applied_filters"]["publisher"] == ["Land Salzburg"]
    assert out["applied_filters"]["format"] == ["CSV"]
    only_stat = _data(await m.search_datasets(publisher="Statistik Austria", category="Bevölkerung", limit=2))
    assert only_stat["total_count"] > 0 and all(d["publisher"] == "Statistik Austria" for d in only_stat["datasets"])


# ---------- Haertung (Review 2026-10-14) ----------


def _addrinfo(*ips: str):
    return lambda host, port, *a, **k: [(socket.AF_INET6 if ":" in ip else socket.AF_INET, 0, 0, "", (ip, 0)) for ip in ips]


class _FakeInner(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append((host, port))
        return "stream"

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # pragma: no cover
        raise AssertionError

    async def sleep(self, seconds):  # pragma: no cover
        pass


async def test_connect_backend_connects_to_checked_ip_not_hostname(monkeypatch) -> None:
    inner = _FakeInner()
    monkeypatch.setattr(socket, "getaddrinfo", _addrinfo("93.184.216.34"))
    assert await m._PublicOnlyBackend(inner).connect_tcp("example.org", 443) == "stream"
    assert inner.connected == [("93.184.216.34", 443)]


@pytest.mark.parametrize("answer", [("127.0.0.1",), ("93.184.216.34", "10.0.0.5"), ("64:ff9b::7f00:1",), ("::1",)])
async def test_connect_backend_blocks_rebinding_to_private_ip(monkeypatch, answer) -> None:
    """Die Pruefung beim Verbinden loest selbst auf: ein Host, der erst oeffentlich, dann privat antwortet, kommt nicht durch."""
    inner = _FakeInner()
    monkeypatch.setattr(socket, "getaddrinfo", _addrinfo(*answer))
    with pytest.raises(ValueError, match="blockiert"):
        await m._PublicOnlyBackend(inner).connect_tcp("rebind.example", 80)
    assert inner.connected == []


async def test_connect_backend_refuses_unix_sockets() -> None:
    with pytest.raises(ValueError):
        await m._PublicOnlyBackend(_FakeInner()).connect_unix_socket("/var/run/docker.sock")


async def test_default_client_uses_pinned_backend_and_ignores_env() -> None:
    assert m._TRANSPORT is None
    async with m._client() as client:
        assert isinstance(client._transport._pool._network_backend, m._PublicOnlyBackend)
        assert client._trust_env is False


def test_nat64_range_is_blocked(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _addrinfo("64:ff9b::7f00:1"))
    with pytest.raises(ValueError, match="blockiert"):
        m._validate_public_url("http://nat64.example/x")


async def test_cache_entry_is_protected_while_it_is_being_prepared(transport, monkeypatch) -> None:
    """Parallele Aufrufe mit abgelaufener TTL duerfen sich nicht gegenseitig die Rohdatei loeschen."""
    transport(lambda req: httpx.Response(200, content=CSV_BODY))
    monkeypatch.setattr(m, "CACHE_TTL_SECONDS", 0)
    real = m._prepare_sync

    def slow(*args, **kwargs):
        time.sleep(0.05)
        return real(*args, **kwargs)

    monkeypatch.setattr(m, "_prepare_sync", slow)

    async def one(n: int) -> int:
        async with m._cached_download(f"http://93.184.216.34/{n}.csv") as (path, _e, _i):
            await asyncio.sleep(0.01)
            return path.stat().st_size

    assert all(size > 0 for size in await asyncio.gather(*(one(n) for n in range(4))))


async def test_failed_preparation_releases_the_cache_entry(transport) -> None:
    transport(lambda req: httpx.Response(200, content=b"PK\x03\x04kaputt"))
    with pytest.raises(ValueError):
        async with m._cached_download(PUBLIC):
            pass
    assert all(e.users == 0 for e in m._cache.values())


async def test_download_has_a_total_deadline(monkeypatch) -> None:
    async def hang(url):
        await asyncio.sleep(5)

    monkeypatch.setattr(m, "_download_raw", hang)
    monkeypatch.setattr(m, "DOWNLOAD_DEADLINE", 0.05)
    with pytest.raises(ValueError, match="dauerte laenger"):
        await m._download_with_deadline(PUBLIC)


async def test_excel_sheet_with_huge_cell_range_is_refused(transport, monkeypatch) -> None:
    transport(lambda req: _xlsx_response())
    monkeypatch.setattr(m, "MAX_EXCEL_CELLS", 5)
    out = _data(await m.inspect_distribution_schema(url=PUBLIC, sheet="Daten"))
    assert "Zellbereich" in out["error"]


def _forged_xlsx() -> bytes:
    """mini.xlsx ohne Kompression, bei dem das groesste Mitglied im Header nur halb so gross angegeben ist."""
    src = zipfile.ZipFile(FIXTURES / "mini.xlsx")
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zout:
        for info in src.infolist():
            zout.writestr(info.filename, src.read(info))
    data = buf.getvalue()
    biggest = max(src.infolist(), key=lambda i: i.file_size).file_size
    needle = struct.pack("<I", biggest)
    assert data.count(needle) >= 4, "lokaler und zentraler Header, je Kompr./Unkompr.-Groesse"
    return data.replace(needle, struct.pack("<I", biggest // 2))


def test_xlsx_with_forged_header_sizes_is_refused(tmp_path) -> None:
    raw = tmp_path / "fake.xlsx"
    raw.write_bytes(_forged_xlsx())
    with pytest.raises(ValueError, match="beschaedigt"):
        m._prepare_sync(raw, "http://x/fake.xlsx", "", "", "", "", -1)


def test_sql_result_cells_and_total_size_are_capped(monkeypatch) -> None:
    path = _csv_file("a;b\n1;2\n")
    try:
        monkeypatch.setattr(m, "MAX_CELL_CHARS", 50)
        out = m._query_sync(path, ".csv", "SELECT repeat('x', 5000) AS t FROM range(3)")
        assert out["returned_rows"] == 3 and len(out["results"][0]["t"]) == 51 and "cells_shortened" in out
        assert out["truncated_at_limit"] is False
        monkeypatch.setattr(m, "MAX_RESULT_CHARS", 120)
        out = m._query_sync(path, ".csv", "SELECT repeat('x', 5000) AS t FROM range(200)")
        assert out["truncated_at_limit"] is True and out["returned_rows"] < 200
    finally:
        path.unlink(missing_ok=True)


def test_sql_row_limit_still_reports_truncation() -> None:
    path = _csv_file("a;b\n1;2\n")
    try:
        out = m._query_sync(path, ".csv", "SELECT * FROM range(1000)")
        assert out["returned_rows"] == m.MAX_SQL_ROWS and out["truncated_at_limit"] is True
        out = m._query_sync(path, ".csv", f"SELECT * FROM range({m.MAX_SQL_ROWS})")
        assert out["returned_rows"] == m.MAX_SQL_ROWS and out["truncated_at_limit"] is False
    finally:
        path.unlink(missing_ok=True)


def test_duckdb_spill_to_disk_is_limited() -> None:
    path = _csv_file("a;b\n1;2\n")
    try:
        con = _create_sandboxed_duckdb(path, ".csv")
        assert con.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0].lower().startswith(("953", "1.0", "1gb", "1000"))
        con.close()
    finally:
        path.unlink(missing_ok=True)


async def test_bearer_middleware_closes_websocket_without_calling_app() -> None:
    seen, sent = [], []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    async def send(msg):
        sent.append(msg)

    await m._BearerAuthMiddleware(app, TOKEN)({"type": "websocket", "headers": []}, None, send)
    assert seen == [] and sent[0]["type"] == "websocket.close"


def test_clean_error_hides_home_paths() -> None:
    text = m._clean_error(ValueError("Extension fehlt in /Users/jemand/.duckdb/extensions/v1/x.duckdb_extension, Division 4/2 ok"))
    assert "/Users" not in text and "4/2" in text


def test_temp_files_live_in_own_dir_and_stale_dirs_are_swept(tmp_path, monkeypatch) -> None:
    import os

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(m, "_tmp_dir", None)
    stale = tmp_path / "datagvat-2147483646-abcd"  # PID existiert nicht
    alive = tmp_path / f"datagvat-{os.getpid()}-zzzz"  # eigener Prozess
    other = tmp_path / "anderes-verzeichnis"
    for d in (stale, alive, other):
        d.mkdir()
    fd, name = m._mkstemp(".x")
    os.close(fd)
    assert Path(name).parent == m._tmp_root() and m._tmp_root().parent == tmp_path
    assert not stale.exists() and alive.exists() and other.exists()
    monkeypatch.setattr(m, "_tmp_dir", None)  # Fixture-Verzeichnis nicht ins echte Temp zurueckschreiben


def test_zip_with_too_many_entries_is_refused_before_reading_directory(tmp_path, monkeypatch) -> None:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for i in range(m.MAX_ZIP_ENTRIES + 1):
            z.writestr(f"{i}.csv", "")
    raw = tmp_path / "viele.zip"
    raw.write_bytes(buf.getvalue())
    called = []
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: called.append(1))
    with pytest.raises(ValueError, match="zu viele"):
        m._prepare_sync(raw, "http://x/viele.zip", "", "", "", "", -1)
    assert called == [], "das Inhaltsverzeichnis darf gar nicht erst geladen werden"


async def test_unused_download_locks_are_dropped(transport) -> None:
    transport(lambda req: httpx.Response(200, content=CSV_BODY))
    async with m._cached_download(PUBLIC):
        pass
    m._cache_clear()
    m._download_locks["alt"] = asyncio.Lock()
    m._cache_evict()
    assert "alt" not in m._download_locks


# ---------- Zusatztabellen (extra_tables) und Codelisten ----------


async def test_query_joins_dataset_with_extra_table(transport) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/daten.csv":
            return httpx.Response(200, content=b"code;wert\n901;10\n902;20\n")
        return httpx.Response(200, content=b"code;name\n901;Wien\n902;Graz\n")

    transport(handler)
    out = _data(await m.query_distribution_sql(
        url="http://93.184.216.34/daten.csv",
        sql_query="SELECT b.name, a.wert FROM dataset a JOIN bezirk b ON a.code = b.code ORDER BY 1",
        extra_tables={"bezirk": "http://93.184.216.34/bezirke.csv"},
    ))
    assert out["results"] == [{"name": "Graz", "wert": 20}, {"name": "Wien", "wert": 10}]
    assert out["tables"] == {"dataset": "http://93.184.216.34/daten.csv", "bezirk": "http://93.184.216.34/bezirke.csv"}


async def test_query_supports_up_to_three_extra_tables(transport) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/daten.csv":
            return httpx.Response(200, content=b"code;wert\n1;10\n")
        return httpx.Response(200, content=b"code;name\n1;Wien\n")

    transport(handler)
    extras = {f"t{i}": f"http://93.184.216.34/t{i}.csv" for i in range(3)}
    out = _data(await m.query_distribution_sql(
        url="http://93.184.216.34/daten.csv",
        sql_query="SELECT COUNT(*) AS n FROM dataset d JOIN t0 ON d.code = t0.code JOIN t1 ON d.code = t1.code JOIN t2 ON d.code = t2.code",
        extra_tables=extras,
    ))
    assert out["results"] == [{"n": 1}]
    assert set(out["tables"]) == {"dataset", "t0", "t1", "t2"}


@pytest.mark.parametrize(
    "extra",
    [
        {"a": "http://93.184.216.34/a.csv", "b": "http://93.184.216.34/b.csv", "c": "http://93.184.216.34/c.csv", "d": "http://93.184.216.34/d.csv"},
        {"1bezirk": "http://93.184.216.34/b.csv"},
        {"Bezirk": "http://93.184.216.34/b.csv"},
        {"be-zirk": "http://93.184.216.34/b.csv"},
        {"dataset": "http://93.184.216.34/b.csv"},
        {"bezirk": ""},
    ],
)
async def test_query_extra_tables_validation_errors(transport, extra) -> None:
    calls = transport(lambda req: httpx.Response(200, content=CSV_BODY))
    out = _data(await m.query_distribution_sql(url=PUBLIC, sql_query="SELECT * FROM dataset", extra_tables=extra))
    assert "error" in out and calls == [], "bei ungueltigen extra_tables darf nichts geladen werden"


async def test_query_extra_table_error_names_the_table(transport) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/daten.csv":
            return httpx.Response(200, content=CSV_BODY)
        return httpx.Response(404)

    transport(handler)
    out = _data(await m.query_distribution_sql(
        url="http://93.184.216.34/daten.csv",
        sql_query="SELECT * FROM dataset",
        extra_tables={"bezirk": "http://93.184.216.34/kaputt.csv"},
    ))
    assert "Zusatztabelle 'bezirk'" in out["error"]


async def test_query_extra_table_with_multi_file_zip_asks_for_selection_with_prefix(transport) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/daten.csv":
            return httpx.Response(200, content=CSV_BODY)
        return httpx.Response(200, content=_zip_bytes({"a.csv": b"x;y\n1;2\n", "b.csv": b"x;y\n3;4\n"}))

    transport(handler)
    out = _data(await m.query_distribution_sql(
        url="http://93.184.216.34/daten.csv",
        sql_query="SELECT * FROM dataset",
        extra_tables={"bezirk": "http://93.184.216.34/archiv.zip"},
    ))
    assert "Zusatztabelle 'bezirk'" in out["error"] and len(out["archive_members"]) == 2


async def test_empty_extra_table_does_not_trigger_the_empty_check(transport) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/daten.csv":
            return httpx.Response(200, content=CSV_BODY)
        return httpx.Response(200, content=b"code;name\n")

    transport(handler)
    out = _data(await m.query_distribution_sql(
        url="http://93.184.216.34/daten.csv",
        sql_query="SELECT COUNT(*) AS n FROM bezirk",
        extra_tables={"bezirk": "http://93.184.216.34/leer.csv"},
    ))
    assert out["results"] == [{"n": 0}]


def test_sandbox_with_extra_tables_blocks_external_access() -> None:
    main = _csv_file("a;b\n1;2\n")
    extra = _csv_file("code;name\n1;Wien\n")
    try:
        con = _create_sandboxed_duckdb(main, ".csv", [("bezirk", extra, ".csv")])
        assert con.execute("SELECT name FROM bezirk;").fetchall() == [("Wien",)]
        with pytest.raises(duckdb.Error):
            con.execute("SELECT * FROM read_csv_auto('/etc/hosts');")
        con.close()
    finally:
        main.unlink(missing_ok=True)
        extra.unlink(missing_ok=True)


# ---------- lookup_tables, Pfad-Maskierung, Peer-Hinweis ----------


async def test_details_expose_lookup_tables_for_dimension_files(transport) -> None:
    payload = {"result": {"id": "x", "distributions": [
        {"id": "1", "format": {"id": "CSV"}, "access_url": ["https://data.statistik.gv.at/data/OGDEXT_X_1.csv"]},
        {"id": "2", "format": {"id": "CSV"}, "access_url": ["https://data.statistik.gv.at/data/OGDEXT_X_1_C-WOHNBEZIRK-0.csv"]},
        {"id": "3", "format": {"id": "CSV"}, "access_url": ["https://data.statistik.gv.at/data/OGDEXT_X_1_C-JAHR-0.csv"]},
    ]}}
    transport(lambda req: httpx.Response(200, json=payload))
    out = _data(await m.get_dataset_details(dataset_id="x"))
    assert out["lookup_tables"] == {
        "wohnbezirk": "https://data.statistik.gv.at/data/OGDEXT_X_1_C-WOHNBEZIRK-0.csv",
        "jahr": "https://data.statistik.gv.at/data/OGDEXT_X_1_C-JAHR-0.csv",
    }
    assert "lookup_tables" in out["recommendation"] and "extra_tables" in out["recommendation"]


def test_lookup_table_name_derivation() -> None:
    used: set[str] = set()
    assert m._lookup_table_name("https://x.at/a_C-WOHNBEZIRK-0.csv", used) == "wohnbezirk"
    assert m._lookup_table_name("https://x.at/b_C-WOHNBEZIRK-0.csv", used) == "wohnbezirk_2"
    assert m._lookup_table_name("https://x.at/c_C-9CODE-1.csv", used) == "t_9code"
    assert m._lookup_table_name("https://x.at/d_C-A.B-C-3.csv", used) == "a_b_c"
    assert m._lookup_table_name("https://x.at/e.csv", used) == "lookup1"
    assert m._lookup_table_name("https://x.at/f.csv", used) == "lookup2"
    assert m._lookup_table_name("https://x.at/g_C-" + "A" * 40 + "-0.csv", used) == "a" * 30


def test_clean_error_keeps_urls_but_hides_local_paths() -> None:
    text = m._clean_error(ValueError("Client error '404 Not Found' for url 'https://www.wien.gv.at/gogv/abc'"))
    assert "https://www.wien.gv.at/gogv/abc" in text
    masked = m._clean_error(ValueError("open /private/var/folders/x/datagvat-1/abc.csv failed"))
    assert "/private/var" not in masked and "<Datei>" in masked


async def test_details_equal_peers_without_distinct_years_say_variants(transport) -> None:
    payload = {"result": {"id": "x", "distributions": [
        {"id": "1", "format": {"id": "CSV"}, "access_url": ["https://x.at/a.csv"], "title": {"de": "Variante A"}},
        {"id": "2", "format": {"id": "CSV"}, "access_url": ["https://x.at/b.csv"], "title": {"de": "Variante B"}},
    ]}}
    transport(lambda req: httpx.Response(200, json=payload))
    out = _data(await m.get_dataset_details(dataset_id="x"))
    assert "1 weitere gleichartige" in out["recommendation"]
    assert "andere Varianten, siehe 'title'" in out["recommendation"]
    assert "gewaehlt wurde die erste" in out["recommendation"]
