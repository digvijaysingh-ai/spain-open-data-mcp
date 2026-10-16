"""Browser smoke test of the atlas page (Playwright), run in CI before each deploy.

    uv run --with playwright pytest -m e2e tests/e2e --site build/site

Serves the built site (page + data) locally and checks what a visitor needs: the map
loads, switching category redraws, the list and table views work, and the credits show.
"""

from __future__ import annotations

import functools
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def site_url(request: pytest.FixtureRequest) -> Iterator[str]:
    root = Path(request.config.getoption("--site"))
    if not (root / "index.html").exists() or not (root / "data" / "index.json").exists():
        pytest.skip(f"no built site at {root}")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def test_atlas_page(site_url: str, tmp_path: Path):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(site_url)
        page.wait_for_selector("body[data-ready='1']", timeout=60_000)
        expect(page.locator("#status")).to_contain_text("Prediction registered, not yet validated")
        options = page.locator("#category option")
        assert options.count() >= 1
        if options.count() > 1:
            page.select_option("#category", index=1)
            expect(page.locator("#status")).to_contain_text("municipalities scored")
        page.click("#view-toggle")
        expect(page.locator("#list")).to_be_visible()
        assert page.locator("#top li").count() >= 1
        page.click("#view-toggle")
        page.click(".table-view summary")
        assert page.locator("#table tbody tr").count() >= 1
        expect(page.locator(".credits")).to_contain_text("OpenStreetMap contributors")
        expect(page.locator(".credits")).to_contain_text("INE")
        page.screenshot(path=str(tmp_path / "atlas.png"))
        mobile = browser.new_page(viewport={"width": 390, "height": 800})
        mobile.goto(site_url)
        mobile.wait_for_selector("body[data-ready='1']", timeout=60_000)
        width = mobile.evaluate("document.documentElement.scrollWidth")
        assert width <= 392, f"horizontal scroll on mobile ({width}px)"
        browser.close()
        assert not errors, errors
