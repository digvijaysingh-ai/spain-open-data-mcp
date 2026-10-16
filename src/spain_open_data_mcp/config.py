"""Runtime settings, read from environment variables with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

PROJECT_URL = "https://github.com/digvijaysingh-ai/spain-open-data-mcp"


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "spain-open-data-mcp"


@dataclass(frozen=True)
class Settings:
    cache_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("SPAIN_MCP_CACHE_DIR") or _default_cache_dir())
    )
    # Upstream operators ask for an identifying User-Agent with a contact URL.
    user_agent: str = f"spain-open-data-mcp/{__version__} (+{PROJECT_URL})"
    http_timeout_s: float = float(os.environ.get("SPAIN_MCP_HTTP_TIMEOUT", "60"))
    offline: bool = os.environ.get("SPAIN_MCP_OFFLINE", "") == "1"
    # Upper bound on the on-disk HTTP cache; oldest responses are dropped past it. 0 = no limit.
    http_cache_mb: int = int(os.environ.get("SPAIN_MCP_HTTP_CACHE_MB", "0"))


settings = Settings()
