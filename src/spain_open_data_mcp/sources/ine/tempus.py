"""INE Tempus3 JSON API client (servicios.ine.es/wstempus).

Always requests `tip=AM`: ISO dates plus per-series metadata, which is the only place
INE returns unit codes (series names carry none). Parsing is split into pure functions.

Missing data comes in two forms, and they mean different things:
- `Valor: null` with a 'secreto estadístico' note: INE suppressed the value (small unit).
- `Data: []`: no value published for this unit, indicator and period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ...core.errors import UpstreamError
from ...core.http import DAY, Fetched, HttpClient

BASE = "https://servicios.ine.es/wstempus/js/ES"
TABLE_PAGE = "https://www.ine.es/jaxiT3/Tabla.htm?t={table_id}"
LICENCE = "CC BY 4.0"
ATTRIBUTION = "Fuente: INE. Elaboración propia con datos extraídos del sitio web del INE: www.ine.es"

Status = Literal["ok", "suppressed", "missing"]


@dataclass(frozen=True)
class Point:
    year: int
    period: str  # e.g. 'A' (annual), 'M09'
    date: str  # ISO date of the reference period
    value: float | None
    status: Status
    provisional: bool = False


@dataclass(frozen=True)
class Series:
    code: str  # INE series code, e.g. 'DPOP19723'
    name: str
    unit: str
    dims: dict[str, tuple[str, str]] = field(default_factory=dict)  # variable -> (code, name)
    points: list[Point] = field(default_factory=list)

    def dim_name(self, variable: str) -> str | None:
        v = self.dims.get(variable)
        return v[1] if v else None

    def dim_code(self, variable: str) -> str | None:
        v = self.dims.get(variable)
        return v[0] if v else None

    def latest(self) -> Point | None:
        return max(self.points, key=lambda p: p.date) if self.points else None


def parse_series_list(raw: Any) -> list[Series]:
    """Parse a DATOS_TABLA / DATOS_SERIE response fetched with tip=AM."""
    if isinstance(raw, dict) and "status" in raw:
        raise UpstreamError(
            problem="INE refused to return this table.",
            cause=f"INE said: {raw['status']!r} (usually the table is too large to return unfiltered).",
            fix="Filter the request to fewer places or indicators.",
        )
    if not isinstance(raw, list):
        raise UpstreamError(
            problem="INE returned an unexpected response.",
            cause=f"Expected a list of series, got {type(raw).__name__}.",
            fix="Check the table id; INE may have retired or renumbered it.",
        )
    return [_parse_series(s) for s in raw]


def _parse_series(s: dict[str, Any]) -> Series:
    dims: dict[str, tuple[str, str]] = {}
    for m in s.get("MetaData") or []:
        dims[m.get("T3_Variable", "")] = (str(m.get("Codigo") or ""), str(m.get("Nombre") or ""))
    points = [_parse_point(d) for d in s.get("Data") or []]
    return Series(
        code=s.get("COD", ""),
        name=(s.get("Nombre") or "").strip().rstrip("."),
        unit=(s.get("T3_Unidad") or "").strip(),
        dims=dims,
        points=points,
    )


def _parse_point(d: dict[str, Any]) -> Point:
    value = d.get("Valor")
    notes = " ".join(str(n.get("texto", "")) for n in d.get("Notas") or [])
    if value is None:
        status: Status = "suppressed" if (d.get("Secreto") or "secreto" in notes.lower()) else "missing"
    else:
        status = "ok"
    fecha = str(d.get("Fecha") or "")
    return Point(
        year=int(d.get("Anyo") or fecha[:4] or 0),
        period=str(d.get("T3_Periodo") or ""),
        date=fecha[:10],
        value=float(value) if value is not None else None,
        status=status,
        provisional=str(d.get("T3_TipoDato") or "").lower().startswith("provisional"),
    )


@dataclass(frozen=True)
class TableResult:
    table_id: int
    series: list[Series]
    retrieved_at: datetime
    stale_cache: bool
    url: str


class TempusClient:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def table(
        self,
        table_id: int,
        *,
        last_n: int = 1,
        filters: list[tuple[int, int]] | None = None,
        ttl_s: float = 7 * DAY,
    ) -> TableResult:
        """DATOS_TABLA with `nult` periods and optional `tv=variable:value` filters."""
        params: list[tuple[str, Any]] = [("nult", last_n), ("tip", "AM")]
        params += [("tv", f"{var}:{val}") for var, val in filters or []]
        fetched: Fetched = await self._http.get_json(
            f"{BASE}/DATOS_TABLA/{table_id}", params, ttl_s=ttl_s, source_name=f"INE table {table_id}"
        )
        return TableResult(
            table_id=table_id,
            series=parse_series_list(fetched.data),
            retrieved_at=fetched.fetched_at,
            stale_cache=fetched.stale,
            url=TABLE_PAGE.format(table_id=table_id),
        )

    async def raw(self, function: str, arg: str, *, ttl_s: float = 30 * DAY) -> Any:
        """Metadata endpoints such as TABLAS_OPERACION or VALORES_GRUPOSTABLA."""
        fetched = await self._http.get_json(
            f"{BASE}/{function}/{arg}", ttl_s=ttl_s, source_name=f"INE {function}"
        )
        return fetched.data
