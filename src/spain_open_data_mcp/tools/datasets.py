"""find_datasets and read_dataset: Spain's national open-data catalogue (datos.gob.es)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, Field

from ..core.envelope import Check, Envelope, Source, confidence_from
from ..core.errors import DataError
from ..core.text import clean_text
from ..places.resolver import normalize, resolve_one
from ..sources import datosgob
from ..sources.datosgob import DatosGob
from .common import to_result

READABLE = ("CSV", "JSON")
SAMPLE_ROWS = 20


class DatasetSummary(BaseModel):
    id: str = Field(description="Pass to read_dataset.")
    title: str
    description: str
    publisher: str | None
    licence: str
    formats: list[str]
    modified: str | None
    update_frequency: str | None
    spatial: str | None
    url: str


class FindData(BaseModel):
    query: str
    place: str | None
    total_found: int
    datasets: list[DatasetSummary]


class ReadData(BaseModel):
    dataset: DatasetSummary
    file_url: str
    file_format: str
    columns: list[str]
    withheld_columns: list[str] = Field(description="Columns that look like personal data; not returned.")
    rows_read: int
    truncated: bool = Field(description="True if the file was larger than the 5 MB read limit.")
    sample: list[dict[str, str]]


def _summary(d: datosgob.Dataset) -> DatasetSummary:
    return DatasetSummary(
        id=d.id, title=d.title, description=d.description, publisher=d.publisher, licence=d.licence,
        formats=d.formats, modified=d.modified, update_frequency=d.periodicity, spatial=d.spatial, url=d.url,
    )  # fmt: skip


def _source(d: datosgob.Dataset | None, url: str, period: str) -> Source:
    return Source(
        publisher=(d.publisher if d and d.publisher else "datos.gob.es catalogue"),
        dataset=d.title if d else "datos.gob.es catalogue search",
        dataset_id=d.id if d else "apidata/catalog",
        url=url,
        licence=d.licence if d else "datos.gob.es legal notice",
        attribution="Source: datos.gob.es and the publisher named; reuse under the dataset's licence.",
        data_period=period,
        retrieved_at=datetime.now(UTC),
    )


async def find_datasets(
    client: DatosGob, query: str, place: str | None = None, limit: int = 10
) -> Envelope[FindData]:
    q = query.strip()
    if len(q) < 3:
        raise DataError(
            problem="The search is too short.",
            cause="Use at least 3 letters.",
            fix="E.g. 'turismo', 'aparcamiento'.",
        )
    found = await client.search(q)
    checks: list[Check] = []
    place_name = None
    if place:
        p, _alternatives = resolve_one(place)
        place_name = p.name
        names = {normalize(n) for n in (p.name, p.province or "", p.ccaa or "", p.municipality or "") if n}
        names |= {normalize(n.split("/")[0]) for n in names}

        def matches(d: datosgob.Dataset) -> bool:
            hay = normalize(" ".join([d.title, d.description, d.spatial or ""]))
            return any(n and n in hay for n in names)

        local = [d for d in found if matches(d)]
        if local:
            found = local
        else:
            checks.append(
                Check(
                    kind="scope",
                    severity="info",
                    message=f"No result mentions {p.name}; showing results for all of Spain.",
                )
            )
    shown = found[:limit]
    names = await client.publisher_names([d.publisher_uri for d in shown])
    for d in shown:
        d.publisher = names.get(d.publisher_uri) or d.publisher_uri.rsplit("/", 1)[-1] or None
    checks.append(
        Check(
            kind="scope",
            severity="info",
            message="Catalogue entries are described by their publishers; check each dataset's licence and update "
            "date before relying on it.",
        )
    )
    data = FindData(query=q, place=place_name, total_found=len(found), datasets=[_summary(d) for d in shown])
    src = _source(None, f"https://datos.gob.es/es/catalogo?q={q}", datetime.now(UTC).date().isoformat())
    return Envelope(data=data, sources=[src], checks=checks, confidence=confidence_from(checks))


async def read_dataset(
    client: DatosGob,
    dataset_id: str,
    file_index: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Envelope[ReadData]:
    d = await client.dataset(dataset_id)
    names = await client.publisher_names([d.publisher_uri])
    d.publisher = names.get(d.publisher_uri)
    files = [(dist, dist["_format"]) for dist in d.distributions]
    readable = [
        (dist, fmt) for dist, fmt in files if fmt in READABLE and isinstance(dist.get("accessURL"), str)
    ]
    if file_index is not None:
        if not 0 <= file_index < len(files):
            raise DataError(
                problem=f"The dataset has no file number {file_index}.",
                cause=f"It has {len(files)} files (numbered from 0).",
                fix="Omit file_index to read the first CSV or JSON file.",
            )
        chosen = files[file_index]
        if chosen[1] not in READABLE or not isinstance(chosen[0].get("accessURL"), str):
            raise DataError(
                problem=f"File {file_index} is {chosen[1] or 'of unknown format'}.",
                cause="Only CSV and JSON files are read.",
                fix=f"Pick a CSV or JSON file; formats: {', '.join(f for _d, f in files)}.",
            )
    elif not readable:
        raise DataError(
            problem=f"'{d.title}' has no CSV or JSON file.",
            cause=f"Its files are: {', '.join(f or '?' for _d, f in files) or 'none'}.",
            fix=f"Open the dataset page: {d.url}",
        )
    else:
        chosen = readable[0]
    dist, fmt = chosen
    url = str(dist["accessURL"])
    raw, truncated = await datosgob.download(url, transport=transport)
    try:
        header, rows = datosgob.parse_table(raw, fmt, truncated)
    except (ValueError, KeyError) as e:
        raise DataError(
            problem="The file could not be read as a table.",
            cause=f"{type(e).__name__}: the {fmt} file is not in a tabular layout.",
            fix=f"Open it from the dataset page: {d.url}",
        ) from e
    withheld = datosgob.screen_columns(header, rows)
    keep = [i for i in range(len(header)) if i not in withheld]
    sample = [
        {clean_text(header[i], 60) or f"col{i}": clean_text(r[i], 120) or "" for i in keep if i < len(r)}
        for r in rows[:SAMPLE_ROWS]
    ]
    checks: list[Check] = []
    if withheld:
        checks.append(
            Check(
                kind="privacy",
                severity="info",
                message=f"Withheld {len(withheld)} column(s) that look like personal data: "
                f"{', '.join(header[i] for i in withheld)}.",
            )
        )
    if truncated:
        checks.append(
            Check(
                kind="scope",
                severity="warning",
                message="The file is larger than 5 MB; only the first 5 MB were read.",
            )
        )
    checks.append(
        Check(
            kind="scope",
            severity="info",
            message=f"Published by {d.publisher or 'the publisher'} under {d.licence}; last modified {d.modified or 'unknown'}. "
            "Values are shown as published, not checked by this server.",
        )
    )
    data = ReadData(
        dataset=_summary(d), file_url=url, file_format=fmt, columns=[header[i] for i in keep],
        withheld_columns=[header[i] for i in withheld], rows_read=len(rows), truncated=truncated, sample=sample,
    )  # fmt: skip
    return Envelope(
        data=data,
        sources=[_source(d, d.url, d.modified or "unknown")],
        checks=checks,
        confidence=confidence_from(checks, "medium"),
    )


def render_find(env: Envelope[FindData]) -> str:
    d = env.data
    lines = [
        f"Datasets for '{d.query}'"
        + (f" ({d.place})" if d.place else "")
        + f": {d.total_found} found, showing {len(d.datasets)}"
    ]
    for s in d.datasets:
        lines.append(
            f"- {s.title} [{', '.join(s.formats) or 'no files'}] by {s.publisher or 'unknown'}; {s.licence}; "
            f"modified {s.modified or 'unknown'}; id {s.id}"
        )
    return "\n".join(lines)


def render_read(env: Envelope[ReadData]) -> str:
    d = env.data
    lines = [
        f"{d.dataset.title} ({d.file_format}, {d.rows_read:,} rows{', first 5 MB' if d.truncated else ''})",
        f"Columns: {', '.join(d.columns)}",
    ]
    if d.withheld_columns:
        lines.append(f"Withheld (personal data): {', '.join(d.withheld_columns)}")
    for row in d.sample[:10]:
        lines.append("- " + "; ".join(f"{k}: {v}" for k, v in row.items()))
    return "\n".join(lines)


async def find_datasets_tool(client: DatosGob, *args, **kwargs):
    env = await find_datasets(client, *args, **kwargs)
    return to_result(env, render_find(env))


async def read_dataset_tool(client: DatosGob, *args, **kwargs):
    env = await read_dataset(client, *args, **kwargs)
    return to_result(env, render_read(env))
