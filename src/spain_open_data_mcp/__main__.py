"""Command line entry point: `spain-open-data-mcp` (stdio), `spain-open-data-mcp http`, or
`spain-open-data-mcp prefetch` (download the data files now instead of on first use)."""

from __future__ import annotations

import argparse
import logging
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="spain-open-data-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="transport")
    sub.add_parser("stdio", help="Run over stdio for Claude Desktop / Claude Code (default).")
    http = sub.add_parser("http", help="Run the Streamable HTTP server (for hosting).")
    http.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    http.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    pre = sub.add_parser(
        "prefetch", help="Download the OpenStreetMap snapshot, tract shapes and walking graphs."
    )
    pre.add_argument("--no-walk", action="store_true", help="Skip the walking graphs (about 175 MB).")
    args = parser.parse_args()

    # stdio carries the protocol on stdout, so logs must go to stderr.
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "WARNING"), format="%(levelname)s %(name)s: %(message)s"
    )

    if args.transport == "http":
        from .hosting import serve

        serve(args.host, args.port)
    elif args.transport == "prefetch":
        import asyncio

        asyncio.run(prefetch(walk=not args.no_walk))
    else:
        from .server import mcp

        mcp.run()


async def prefetch(walk: bool = True) -> None:
    """Fetch every data file into the cache directory (used when building the server image)."""
    from .sources.geo.geometry import GeometryStore
    from .sources.geo.walk import WalkGraphs
    from .sources.osm.snapshot import SnapshotStore

    for name, store in (("OpenStreetMap snapshot", SnapshotStore()), ("tract geometry", GeometryStore())):
        await store.get()
        print(f"{name}: {store.db_path}")
    if walk:
        graphs = WalkGraphs()
        manifest = await graphs.manifest() or {}
        for province in sorted(manifest.get("provinces", {})):
            await graphs.graph(province)
            graphs._graphs.clear()  # keep memory flat; the files stay on disk
        print(f"walking graphs: {len(manifest.get('provinces', {}))} provinces in {graphs.directory}")


if __name__ == "__main__":
    main()
