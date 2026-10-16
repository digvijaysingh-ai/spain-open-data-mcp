"""Data status: how fresh each source is on this server, read from local state only.

Served as the MCP resource spain-open-data://status. It never calls upstream APIs, so it
is safe to read often; it reports what the server would answer from right now.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from . import __version__
from .config import settings
from .engine.model import METHOD_VERSION
from .sources.ine.datasets import tables
from .sources.osm import snapshot as osm

OSM_STALE_DAYS = 45
# When each source is next expected; INE publishes on a calendar (ine.es/calendario).
RELEASES: dict[str, str] = {
    "INE income atlas (ADRH)": "yearly, in October; 2024 data due 2026-10-19",
    "INE household budget survey (EPF)": "yearly, in June",
    "INE company directory (DIRCE)": "yearly, in December",
    "INE official population (Padrón)": "yearly, in December",
    "INE consumer prices (CPI)": "monthly, mid-month",
    "OpenStreetMap snapshot": "monthly, on the 3rd",
}


def _snapshot_line() -> str:
    path = osm.SnapshotStore().fixed_path or settings.cache_dir / "osm" / osm.DB_NAME
    if not path.exists():
        return "| OpenStreetMap snapshot | not downloaded yet | | downloaded on first use |"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        con.close()
    except sqlite3.DatabaseError:
        return "| OpenStreetMap snapshot | damaged | | re-downloaded on next use |"
    when = datetime.fromisoformat(meta["osm_timestamp"])
    age = (datetime.now(UTC) - when).days
    flag = f"**stale ({age} days)**" if age > OSM_STALE_DAYS else f"ok ({age} days)"
    return f"| OpenStreetMap snapshot | {when:%Y-%m-%d} | {meta.get('built_at', '')[:10]} | {flag} |"


def _cached_lines() -> list[str]:
    """When each key INE table was last fetched, from the HTTP cache."""
    path = settings.cache_dir / "http.sqlite"
    t = tables()
    wanted = {
        "INE income atlas (ADRH), aggregates": t["adrh_aggregates"]["income"],
        "INE household budget survey (EPF)": t["epf"]["spend_by_code"],
        "INE company directory (DIRCE)": t["dirce_companies"],
        "INE consumer prices (CPI)": t["cpi"]["national"],
    }
    if not path.exists():
        return [f"| {name} | | not fetched yet | |" for name in wanted]
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    lines = []
    for name, table_id in wanted.items():
        row = con.execute(
            "SELECT max(fetched_at) FROM http WHERE key LIKE ?", (f"%DATOS_TABLA/{table_id}%",)
        ).fetchone()
        fetched = (
            datetime.fromtimestamp(row[0], UTC).strftime("%Y-%m-%d") if row and row[0] else "not fetched yet"
        )
        lines.append(f"| {name} | table {table_id} | {fetched} | |")
    con.close()
    return lines


def data_status() -> str:
    t = tables()
    lines = [
        "# Data status",
        "",
        f"Server {__version__}, opportunity method {METHOD_VERSION}, checked {date.today().isoformat()}.",
        "",
        "| Source | Data | Fetched or built | Status |",
        "|---|---|---|---|",
        _snapshot_line(),
        f"| INE official population (Padrón), bundled | {t['padron_year']}-01-01 | {t['generated_at'][:10]} | |",
        *_cached_lines(),
        "",
        "## Release calendar",
        *[f"- {name}: {when}" for name, when in RELEASES.items()],
        "",
        "Bundled table ids are rebuilt with scripts/build_reference.py after INE releases.",
    ]
    return "\n".join(lines)
