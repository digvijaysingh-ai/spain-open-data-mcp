"""datos.gob.es, Spain's national open-data catalogue: search, metadata, and reading files.

- Search: the catalogue API (apidata) by title and by keyword, merged. Page size is capped
  at 200 by the API; 50 per query is enough to rank.
- Publisher names: one SPARQL query per result page (foaf:name of each publisher).
- Reading: only download links listed in the catalogue for that dataset are fetched (never a
  URL from the caller), over http(s), to public addresses, at most MAX_BYTES. CSV and JSON
  are parsed; columns that look like personal data are withheld (see screen_columns).
"""

from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from ..config import settings
from ..core.errors import DataError, UpstreamError
from ..core.http import DAY, HttpClient
from ..core.text import clean_text
from ..places.resolver import normalize

API = "https://datos.gob.es/apidata/catalog"
SPARQL = "https://datos.gob.es/virtuoso/sparql"
CATALOGUE_PAGE = "https://datos.gob.es/es/catalogo/"
MAX_BYTES = 5_000_000
PAGE = 50
LICENCES = {
    "creativecommons.org/licenses/by/4.0": "CC BY 4.0",
    "creativecommons.org/licenses/by-sa/4.0": "CC BY-SA 4.0",
    "creativecommons.org/publicdomain/zero/1.0": "CC0 1.0",
    "creativecommons.org/licenses/by/3.0": "CC BY 3.0",
    "opendatacommons.org/licenses/odbl": "ODbL",
    "datos.gob.es/avisolegal": "datos.gob.es legal notice (reuse with attribution)",
}
# Column names that hold data about identifiable people. Matched as whole words on the
# normalised header. A plain 'nombre' column is usually a place or business name; it is
# withheld only when the table also has a surname column.
_PERSONAL = re.compile(
    r"\b(nombre y apellidos|nombre completo|apellido|apellidos|dni|nif|nie|pasaporte|email|e mail|correo|"
    r"telefono|movil|fecha nacimiento|nacimiento|iban|cuenta bancaria|matricula|domicilio|direccion postal|firma)\b"
)
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", re.IGNORECASE)
_DNI = re.compile(r"^\s*[XYZ]?\d{7,8}[A-Z]\s*$", re.IGNORECASE)
_PHONE = re.compile(r"^\s*(\+34\s?)?[6-9]\d{2}\s?\d{3}\s?\d{3}\s*$")


@dataclass
class Dataset:
    id: str
    title: str
    description: str
    publisher_uri: str
    publisher: str | None
    licence: str
    licence_url: str
    formats: list[str]
    modified: str | None
    periodicity: str | None
    spatial: str | None
    url: str
    distributions: list[dict[str, Any]] = field(default_factory=list)


def _text(value: Any) -> str:
    """apidata gives text as [{'_value', '_lang'}], a dict, or a string; prefer Spanish."""
    if isinstance(value, list):
        es = [v for v in value if isinstance(v, dict) and v.get("_lang") == "es"]
        value = (es or value or [""])[0]
    if isinstance(value, dict):
        value = value.get("_value", "")
    return str(value or "")


def _last(uri: str | None) -> str | None:
    return uri.rstrip("/").rsplit("/", 1)[-1] if uri else None


MIME_FORMATS = {
    "text/csv": "CSV", "application/json": "JSON", "application/pdf": "PDF", "text/html": "HTML",
    "application/vnd.ms-excel": "XLS", "application/xml": "XML", "text/xml": "XML",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
}  # fmt: skip
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    )
}


def file_format(value: Any) -> str:
    """The catalogue gives a file-type URI ('.../file-type/CSV') or a {'value': MIME type}."""
    if isinstance(value, dict):
        mime = str(value.get("value") or value.get("_value") or "").split(";")[0].strip().lower()
        return MIME_FORMATS.get(mime) or (mime.rsplit("/", 1)[-1].upper() if mime else "?")
    tail = _last(value) if isinstance(value, str) else None
    return tail.upper() if tail else "?"


def iso_date(value: str) -> str | None:
    """'mar, 13 ene 2026 13:32:21 GMT+0000' -> '2026-01-13'; ISO strings keep their date."""
    if m := re.match(r"\d{4}-\d{2}-\d{2}", value):
        return m.group(0)
    if m := re.search(r"(\d{1,2}) ([a-z]{3}) (\d{4})", value.lower()):
        month = _MONTHS.get(m.group(2))
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}"
    return value or None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _uri_tail(value: Any) -> str | None:
    if isinstance(value, list):
        return ", ".join(t for v in value if (t := _uri_tail(v))) or None
    if isinstance(value, dict):
        value = value.get("_about") or value.get("value")
    tail = _last(value) if isinstance(value, str) else None
    return tail.replace("_", " ").lower() if tail else None


def licence_name(uri: str) -> str:
    for key, name in LICENCES.items():
        if key in uri:
            return name
    if "/bnode/" in uri:  # described on the publisher's portal, not in the catalogue
        return "see the dataset page"
    return uri or "not stated"


def parse(item: dict[str, Any]) -> Dataset:
    about = item.get("_about", "")
    dists = item.get("distribution") or []
    if isinstance(dists, dict):
        dists = [dists]
    dists = [d for d in dists if isinstance(d, dict)]
    for d in dists:
        d["_format"] = file_format(d.get("format"))
    formats = sorted({d["_format"] for d in dists})
    lic = _str(item.get("license"))
    return Dataset(
        id=_last(about) or "",
        title=clean_text(_text(item.get("title")), 200) or "(untitled)",
        description=clean_text(_text(item.get("description")), 400) or "",
        publisher_uri=_str(item.get("publisher")),
        publisher=None,
        licence=licence_name(lic),
        licence_url=lic,
        formats=formats,
        modified=iso_date(_text(item.get("modified"))),
        periodicity=_uri_tail(item.get("accrualPeriodicity")),
        spatial=_uri_tail(item.get("spatial")),
        url=CATALOGUE_PAGE + (_last(about) or ""),
        distributions=dists,
    )


class DatosGob:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    async def _items(self, path: str) -> list[dict[str, Any]]:
        fetched = await self.http.get_json(
            f"{API}/{path}",
            [("_pageSize", PAGE), ("_page", 0)],
            ttl_s=DAY,
            source_name="datos.gob.es catalogue",
        )
        return fetched.data.get("result", {}).get("items", []) if isinstance(fetched.data, dict) else []

    async def search(self, query: str) -> list[Dataset]:
        q = quote(query.strip())
        by_title, by_keyword = await asyncio.gather(
            self._items(f"dataset/title/{q}"), self._items(f"dataset/keyword/{q}")
        )
        seen: dict[str, Dataset] = {}
        for item in [*by_title, *by_keyword]:  # title matches first
            d = parse(item)
            if d.id and d.id not in seen:
                seen[d.id] = d
        return list(seen.values())

    async def dataset(self, dataset_id: str) -> Dataset:
        raw = dataset_id.strip()
        # A bare id, or a datos.gob.es catalogue link to one.
        slug = (_last(raw) or "") if re.match(r"https?://datos\.gob\.es/", raw) else raw
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,200}", slug):
            raise DataError(
                problem=f"'{dataset_id}' is not a datos.gob.es dataset id.",
                cause="Ids look like 'l01281317-estadisticas-de-la-oficina-de-turismo'.",
                fix="Use an id returned by find_datasets.",
            )
        items = await self._items(f"dataset/{slug}")
        if not items:
            raise DataError(
                problem=f"No dataset '{slug}' in the catalogue.",
                cause="It may have been withdrawn or renamed.",
                fix="Search again with find_datasets.",
            )
        return parse(items[0])

    async def publisher_names(self, uris: list[str]) -> dict[str, str]:
        uris = sorted({u for u in uris if u.startswith("http")})
        if not uris:
            return {}
        values = " ".join(f"<{u}>" for u in uris)
        query = (
            f"SELECT ?p ?name WHERE {{ VALUES ?p {{ {values} }} ?p <http://xmlns.com/foaf/0.1/name> ?name }}"
        )
        try:
            fetched = await self.http.get_json(
                SPARQL,
                [("query", query), ("format", "application/sparql-results+json")],
                ttl_s=30 * DAY,
                source_name="datos.gob.es SPARQL",
                accept="application/sparql-results+json",
            )
        except UpstreamError:
            return {}
        rows = fetched.data.get("results", {}).get("bindings", []) if isinstance(fetched.data, dict) else []
        return {r["p"]["value"]: r["name"]["value"] for r in rows if "p" in r and "name" in r}


# Reading files


def _public_host(url: str) -> bool:
    """Refuse non-http(s) URLs and hosts that resolve to private, loopback or link-local addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


MAX_REDIRECTS = 5


def _not_public() -> DataError:
    return DataError(
        problem="The file's address is not a public web address.",
        cause="Only http(s) links to public hosts listed in the catalogue are fetched.",
        fix="Open the dataset page in a browser instead.",
    )


async def download(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> tuple[bytes, bool]:
    """(bytes, truncated). At most MAX_BYTES are read. Redirects are followed by hand so each
    hop is checked against private addresses. `transport` (tests only) skips the DNS check."""
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        follow_redirects=False,
        timeout=settings.http_timeout_s,
        transport=transport,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            if transport is None and not await asyncio.to_thread(_public_host, url):
                raise _not_public()
            async with client.stream("GET", url) as r:
                if r.is_redirect and "location" in r.headers:
                    url = str(r.url.join(r.headers["location"]))
                    continue
                if r.status_code >= 400:
                    raise UpstreamError(
                        problem="The dataset file could not be downloaded.",
                        cause=f"Its publisher's server returned HTTP {r.status_code}.",
                        fix="Try another file of the dataset, or open the dataset page.",
                    )
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        return bytes(buf[:MAX_BYTES]), True
                return bytes(buf), False
    raise UpstreamError(
        problem="The dataset file could not be downloaded.",
        cause=f"More than {MAX_REDIRECTS} redirects.",
        fix="Open the dataset page in a browser instead.",
    )


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_table(raw: bytes, fmt: str, truncated: bool) -> tuple[list[str], list[list[str]]]:
    text = decode(raw)
    if fmt == "JSON":
        doc = json.loads(text)
        records = doc if isinstance(doc, list) else next((v for v in doc.values() if isinstance(v, list)), [])
        records = [r for r in records if isinstance(r, dict)]
        header = list(dict.fromkeys(k for r in records[:200] for k in r))
        return header, [[str(r.get(h, "")) for h in header] for r in records]
    if truncated:
        text = text[: text.rfind("\n")]  # drop the cut last line
    sample = text[:20_000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if sample.count(";") > sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    return (rows[0], rows[1:]) if rows else ([], [])


def screen_columns(header: list[str], rows: list[list[str]]) -> list[int]:
    """Indexes of columns that look like personal data: by name, or by values that are mostly
    emails, Spanish ID numbers or mobile and landline numbers."""
    withheld = []
    names = [normalize(h) for h in header]
    has_surname = any(re.search(r"\bapellidos?\b", n) for n in names)
    for i, h in enumerate(names):
        if _PERSONAL.search(h) or (has_surname and re.fullmatch(r"nombres?", h)):
            withheld.append(i)
            continue
        values = [r[i] for r in rows[:200] if i < len(r) and r[i].strip()]
        if values:
            hits = sum(1 for v in values if _EMAIL.search(v) or _DNI.match(v) or _PHONE.match(v))
            if hits / len(values) > 0.3:
                withheld.append(i)
    return withheld
