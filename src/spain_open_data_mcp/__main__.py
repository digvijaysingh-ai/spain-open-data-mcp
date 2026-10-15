"""Command line entry point: `spain-open-data-mcp` (stdio) or `spain-open-data-mcp http`."""

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
    args = parser.parse_args()

    # stdio carries the protocol on stdout, so logs must go to stderr.
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "WARNING"), format="%(levelname)s %(name)s: %(message)s"
    )

    from .server import mcp

    if args.transport == "http":
        mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
