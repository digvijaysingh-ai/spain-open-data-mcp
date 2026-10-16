"""Data files published as GitHub release assets: found, downloaded, checked and refreshed.

Used for the OpenStreetMap snapshot and the tract geometry. Each file has a release per
schema version (e.g. osm-snapshot-v1), a gzip of the SQLite file and a JSON manifest with
its SHA-256. Lookup order:

1. An environment variable with a path to the file (the hosted server bakes files in).
2. <cache dir>/<subdir>/<file>, downloaded on first use and verified against the manifest.
   The manifest is re-read at most once a day; a newer file replaces the local one. If the
   check fails, the local copy is used and reported as stale. A local copy that fails its
   integrity check is deleted and downloaded once more.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

import httpx

from ..config import settings
from .errors import UpstreamError

log = logging.getLogger(__name__)

CHECK_EVERY_S = 24 * 3600


class AssetCorrupt(Exception):
    """The file is unreadable, truncated or not the expected kind of file."""


class AssetFile(Protocol):
    path: Path
    stale_reason: str | None

    def close(self) -> None: ...


F = TypeVar("F", bound=AssetFile)


@dataclass(frozen=True)
class AssetSpec:
    label: str  # "OpenStreetMap snapshot", for messages
    db_name: str
    manifest_name: str
    schema_version: int
    release_url: str
    env_var: str
    subdir: str
    build_script: str


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gunzip(src: Path, dest: Path) -> None:
    with gzip.open(src, "rb") as fin, dest.open("wb") as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)


class ReleaseStore(Generic[F]):
    """Finds, downloads and refreshes one release asset. One per process."""

    def __init__(
        self,
        spec: AssetSpec,
        opener: Callable[[Path, str | None], F],
        directory: Path | None = None,
        *,
        fixed_path: Path | None = None,
        release_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        offline: bool | None = None,
    ) -> None:
        self.spec = spec
        self.opener = opener
        env_path = os.environ.get(spec.env_var)
        self.fixed_path = fixed_path or (Path(env_path) if env_path else None)
        self.directory = directory or settings.cache_dir / spec.subdir
        self.release_url = release_url or spec.release_url
        self.transport = transport
        self.offline = settings.offline if offline is None else offline
        self._file: F | None = None
        self._checked_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def db_path(self) -> Path:
        return self.directory / self.spec.db_name

    async def get(self) -> F:
        s = self.spec
        async with self._lock:
            if self.fixed_path:
                if self._file is None:
                    if not self.fixed_path.exists():
                        raise UpstreamError(
                            problem=f"The {s.label} is missing.",
                            cause=f"{s.env_var} points to {self.fixed_path}, which does not exist.",
                            fix=f"Fix the path, or unset {s.env_var} to download the published file.",
                        )
                    try:
                        self._file = self.opener(self.fixed_path, None)
                    except AssetCorrupt as e:
                        raise UpstreamError(
                            problem=f"The {s.label} file is damaged.",
                            cause=f"{self.fixed_path}: {e}.",
                            fix=f"Rebuild it with {s.build_script}, or unset {s.env_var} to download the published file.",
                        ) from e
                return self._file
            if self._file is None or time.time() - self._checked_at > CHECK_EVERY_S:
                try:
                    await self._refresh()
                except AssetCorrupt as e:
                    # Damaged local copy (interrupted write, full disk): start again, once.
                    log.warning("%s %s is damaged (%s); downloading it again", s.label, self.db_path, e)
                    self._discard()
                    try:
                        await self._refresh()
                    except AssetCorrupt as e2:
                        self._discard()
                        raise UpstreamError(
                            problem=f"The {s.label} is damaged, and so was a fresh download.",
                            cause=str(e2),
                            fix=f"Check free disk space in the cache directory and try again, or set {s.env_var} "
                            "to a file you built.",
                        ) from e2
            assert self._file is not None
            return self._file

    def _discard(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        for name in (self.spec.db_name, self.spec.manifest_name, "checked_at"):
            (self.directory / name).unlink(missing_ok=True)

    async def _refresh(self) -> None:
        have = self.db_path.exists()
        marker = self.directory / "checked_at"
        if have and self.offline:
            self._open(None)
            return
        if have and marker.exists() and time.time() - marker.stat().st_mtime < CHECK_EVERY_S:
            self._checked_at = marker.stat().st_mtime
            self._open(None)
            return
        try:
            await self._download_if_newer()
            self.directory.mkdir(parents=True, exist_ok=True)
            marker.touch()
            self._checked_at = time.time()
            self._open(None)
        except (httpx.HTTPError, OSError, ValueError, UpstreamError) as e:
            if not have:
                if isinstance(e, UpstreamError):
                    raise
                raise UpstreamError(
                    problem=f"The {self.spec.label} could not be downloaded.",
                    cause=f"{type(e).__name__}: {e}",
                    fix=f"Try again in a minute. If it keeps failing, download {self.spec.db_name}.gz "
                    f"from {self.release_url}, unzip it and set {self.spec.env_var} to its path.",
                ) from e
            log.warning("%s refresh failed, using local copy: %s", self.spec.label, e)
            self._checked_at = time.time()
            self._open(f"the check for a newer file failed ({type(e).__name__})")

    def _open(self, stale_reason: str | None) -> None:
        if self._file is not None and self._file.path == self.db_path:
            self._file.stale_reason = stale_reason
            return
        self._file = self.opener(self.db_path, stale_reason)

    async def _download_if_newer(self) -> None:
        s = self.spec
        if self.offline:
            raise UpstreamError(
                problem=f"The {s.label} is not on this machine.",
                cause="Offline mode (SPAIN_MCP_OFFLINE=1) does not download it.",
                fix=f"Unset SPAIN_MCP_OFFLINE once to download it, or set {s.env_var}.",
            )
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
            timeout=httpx.Timeout(settings.http_timeout_s, read=300),
            transport=self.transport,
        ) as client:
            r = await client.get(self.release_url + s.manifest_name)
            r.raise_for_status()
            manifest = json.loads(r.text)
            if int(manifest.get("schema_version", 0)) != s.schema_version:
                raise UpstreamError(
                    problem=f"The published {s.label} has a different format.",
                    cause=f"It is schema {manifest.get('schema_version')}; this server reads schema {s.schema_version}.",
                    fix="Upgrade spain-open-data-mcp to the latest version.",
                )
            local = self.directory / s.manifest_name
            if (
                self.db_path.exists()
                and local.exists()
                and json.loads(local.read_text(encoding="utf-8")).get("sha256") == manifest["sha256"]
            ):
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = self.directory / (s.db_name + ".part")
            gz = self.directory / (s.db_name + ".gz.part")
            log.info("downloading %s (%s)", s.label, manifest.get("built_at"))
            async with client.stream("GET", self.release_url + manifest["file"] + ".gz") as resp:
                resp.raise_for_status()
                with gz.open("wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 20):
                        f.write(chunk)
            await asyncio.to_thread(_gunzip, gz, tmp)
            gz.unlink()
            digest = await asyncio.to_thread(sha256, tmp)
            if digest != manifest["sha256"]:
                tmp.unlink()
                raise ValueError(f"downloaded {s.label} does not match the manifest checksum")
            if self._file is not None:
                self._file.close()
                self._file = None
            tmp.replace(self.db_path)
            local.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
