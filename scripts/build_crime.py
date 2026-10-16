"""Build src/spain_open_data_mcp/data/crime.json from the Interior ministry's crime balance.

    uv run python scripts/build_crime.py

Source: Ministerio del Interior, Portal Estadístico de Criminalidad, "Balance de Criminalidad"
(quarterly; cumulative from January). Its municipal table covers every municipality over
20,000 residents plus provinces, regions and Spain, by type of offence, for the period and
the same period a year earlier. Public sector information reused under Ley 37/2007.

Two periods are kept: the latest full year (the fourth-quarter balance) and the latest
balance (year to date). The portal lists each quarter's tables on an index page; the table
with municipal rows is found by its content, since file names change every quarter.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from spain_open_data_mcp.config import settings  # noqa: E402
from spain_open_data_mcp.places.codes import CCAA, PROVINCES  # noqa: E402
from spain_open_data_mcp.places.resolver import name_variants, normalize  # noqa: E402

HOST = "https://estadisticasdecriminalidad.ses.mir.es"
BALANCES = f"{HOST}/publico/portalestadistico/balances.html"
INDEX = f"{HOST}/sec/dynPx/inebase/index.htm?type=pcaxis&path=/DatosBalanceAnt/{{q}}/&file=pcaxis"
CSV_URL = f"{HOST}/sec/jaxiPx/files/_px/es/csv_bdsc{{path}}{{file}}.csv_bdsc?nocab=1"
OUT = ROOT / "src" / "spain_open_data_mcp" / "data" / "crime.json"

# Offence types kept, by the number or roman numeral that starts the ministry's label.
TYPES = {
    "total": "III.",
    "conventional": "I.",
    "homicide": "1.",
    "attempted_homicide": "2.",
    "assault": "3.",
    "sexual": "5.",
    "robbery_violence": "6.",
    "burglary": "7.",
    "home_burglary": "7.1",
    "theft": "8.",
    "vehicle_theft": "9.",
    "drug_trafficking": "10.",
    "cybercrime": "II.",
    "online_fraud": "12.",
}

client = httpx.Client(headers={"User-Agent": settings.user_agent}, timeout=120, follow_redirects=True)


def quarters() -> list[str]:
    html = client.get(BALANCES).text
    return sorted(set(re.findall(r"/DatosBalanceAnt/(\d{5})/", html)))


def municipal_table(q: str) -> tuple[str, list[list[str]]]:
    """(url, rows) of the quarter's table with municipal rows."""
    html = client.get(INDEX.format(q=q)).text
    links = dict.fromkeys(
        re.findall(r"Tabla\.htm\?path=(/DatosBalance\w+/l0/)&(?:amp;)?file=(\w+)\.px", html)
    )
    for path, file in links:
        url = CSV_URL.format(path=path, file=file)
        r = client.get(url)
        r.raise_for_status()
        rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
        if any(re.match(r"\d{5} ", row[0]) for row in rows[1:] if row):
            return url, rows
    raise SystemExit(f"No municipal table found for quarter {q}")


def area_code(name: str, lookup: dict[str, str]) -> str | None:
    if m := re.match(r"(\d{5}) ", name):
        return m.group(1)
    if name.strip().upper() == "NACIONAL":
        return "ES"
    if name.strip().upper() == "EXTRANJERA" or normalize(name).startswith("isla de "):
        return None  # offences recorded abroad; islands have no INE area code
    clean = re.sub(r"^(provincia de|ciudad autonoma de) ", "", normalize(name))
    is_province = normalize(name).startswith("provincia de")
    key = ("p:" if is_province else "c:") + clean
    if key in lookup:
        return lookup[key]
    raise SystemExit(f"Unmatched area name in the crime table: {name!r}")


def name_lookup() -> dict[str, str]:
    out: dict[str, str] = {}
    for code, (name, _ccaa, extra) in PROVINCES.items():
        for n in (name, *extra):
            for v in name_variants(n):
                out["p:" + v] = code
    for code, (name, extra) in CCAA.items():
        for n in (name, *extra):
            for v in name_variants(n):
                out["c:" + v] = f"CA{code}"
    # The ministry writes articles after the name: 'RIOJA (LA)', 'BALEARS (ILLES)', 'CORUÑA (A)'.
    for key in list(out):
        prefix, rest = key[:2], key[2:]
        parts = rest.split(" ", 1)
        if len(parts) == 2 and parts[0] in ("la", "a", "illes", "les", "el", "las", "los"):
            out[f"{prefix}{parts[1]} {parts[0]}"] = out[key]
    out["c:comunitat valenciana"] = out.get("c:comunitat valenciana", "CA10")
    return out


def number(text: str) -> float | None:
    t = text.strip().replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def parse(rows: list[list[str]], lookup: dict[str, str]) -> tuple[dict, str, str]:
    """{code: {'name', 'values': {type: [previous, current]}}}, current label, previous label."""
    periods = sorted({r[2] for r in rows[1:] if len(r) > 3 and not r[2].startswith("Variación")})
    previous, current = sorted(periods, key=lambda p: int(p[-4:]))
    areas: dict[str, dict] = {}
    for r in rows[1:]:
        if len(r) < 4 or r[2].startswith("Variación"):
            continue
        slug = next(
            (s for s, prefix in TYPES.items() if re.match(re.escape(prefix) + r"(?!\d)", r[1].strip())), None
        )
        if slug is None:
            continue
        code = area_code(r[0], lookup)
        if code is None:
            continue
        entry = areas.setdefault(code, {"name": re.sub(r"^\d{5} ", "", r[0]).strip(), "values": {}})
        pair = entry["values"].setdefault(slug, [None, None])
        pair[0 if r[2] == previous else 1] = number(r[3])
    return areas, current, previous


def main() -> None:
    lookup = name_lookup()
    qs = quarters()
    latest_q = qs[-1]
    year_q = max(q for q in qs if q.endswith("4"))
    out: dict = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "publisher": "Ministerio del Interior, Portal Estadístico de Criminalidad",
        "dataset": "Balance de Criminalidad",
        "licence": "Public sector information, reuse under Ley 37/2007 (cite the source, do not distort the data)",
        "types": {slug: prefix for slug, prefix in TYPES.items()},
        "periods": {},
    }
    for key, q in (("year", year_q), ("latest", latest_q)):
        url, rows = municipal_table(q)
        areas, current, previous = parse(rows, lookup)
        out["periods"][key] = {
            "quarter": q,
            "label": current,
            "previous_label": previous,
            "url": url,
            "areas": areas,
        }
        n_mun = sum(1 for c in areas if len(c) == 5)
        print(f"{key}: quarter {q}, {current}: {n_mun} municipalities, {len(areas)} areas")
        if n_mun < 300 or "ES" not in areas:
            raise SystemExit("Fewer municipalities than expected; the table layout may have changed")
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e3:.0f} kB)")


if __name__ == "__main__":
    main()
