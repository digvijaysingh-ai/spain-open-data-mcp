from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from spain_open_data_mcp.core.http import HttpClient
from spain_open_data_mcp.sources.ine.datasets import IneData
from spain_open_data_mcp.sources.ine.tempus import TempusClient
from tests.fixtures.ine_mock import IneFixtures


@pytest.fixture
def ine_fixtures() -> IneFixtures:
    return IneFixtures()


@pytest.fixture
async def ine(tmp_path: Path, ine_fixtures: IneFixtures) -> AsyncIterator[IneData]:
    http = HttpClient(tmp_path / "http.sqlite", transport=ine_fixtures.transport(), offline=False)
    yield IneData(TempusClient(http))
    await http.aclose()
