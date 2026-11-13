"""Capture saved-data choices from an authenticated fictional local fixture.

Run with the quality environment and Playwright. The fixture creates clean
Contacts, an immutable saved version, and a destination profile without
contacting Odoo or opening the running user's Impodo data.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from impodo.domain.odoo_saved_data import SavedOdooRestoreProfile
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web.test_saved_odoo_archive_lifecycle import (
    SavedOdooArchiveLifecycleTests,
)


def capture(
    output_directory: Path,
    *,
    browser_channel: str = "msedge",
    browser_executable: Path | None = None,
) -> tuple[Path, ...]:
    """Write current 1440 by 1024 views without source or destination calls."""

    fixture = SavedOdooArchiveLifecycleTests(methodName="runTest")
    fixture.setUp()
    try:
        definition, _version, _publication = fixture._captured_version(
            19, connection_mode="LOCAL"
        )
        service = fixture.app.state.context.saved_odoo_data
        if service is None:
            raise RuntimeError("The fictional fixture did not configure saved data")
        now = datetime.now(UTC)
        profile = SavedOdooRestoreProfile(
            profile_id=str(uuid4()),
            project_id=definition.project_id,
            saved_data_id=definition.saved_data_id,
            label="Test database · Company North",
            revision=1,
            connection_mode="REMOTE",
            base_url="https://test.example.test",
            database="odoo_test",
            company_name="Company North",
            credential_workspace_id=None,
            settings_json=json.dumps(
                {"keys": {"res.partner": ["name"]}, "rules": {}, "defaults": {}}
            ),
            created_at=now,
            updated_at=now,
        )
        service.repository.save_restore_profile(profile, expected_revision=None)
        cookie = fixture.client.cookies.get("impodo_session")
        if not cookie:
            raise RuntimeError("The fictional fixture did not establish a session")
        route = f"/projects/{definition.project_id}/saved-odoo-data?selected={definition.saved_data_id}&profile={profile.profile_id}"
        output_directory.mkdir(parents=True, exist_ok=True)
        results = []
        for marker, name in (
            ("Capture configuration", "saved-odoo-data.png"),
            ("Restore a saved version", "saved-odoo-restore.png"),
        ):
            output = output_directory / name
            _capture_route(
                fixture.app,
                session_cookie=cookie,
                route=route,
                marker=marker,
                output=output,
                browser_channel="" if browser_channel == "bundled" else browser_channel,
                browser_executable=browser_executable,
                selector=None,
            )
            results.append(output)
        return tuple(results)
    finally:
        fixture.tearDown()


def _capture_route(
    app,
    *,
    session_cookie,
    route,
    marker,
    output,
    browser_channel,
    browser_executable,
    selector,
) -> None:
    """Capture current content using an installed channel or explicit browser."""

    from playwright.sync_api import expect, sync_playwright

    server, thread, port = _start_server(app)
    try:
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = (
                playwright.chromium.launch(
                    executable_path=str(browser_executable), headless=True
                )
                if browser_executable is not None
                else playwright.chromium.launch(channel=browser_channel, headless=True)
            )
            context = browser.new_context(
                viewport={"width": 1440, "height": 1024},
                device_scale_factor=1,
                locale="en-GB",
            )
            context.add_cookies(
                [
                    {
                        "name": "impodo_session",
                        "value": session_cookie,
                        "url": base_url,
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            )
            page = context.new_page()
            response = page.goto(base_url + route, wait_until="networkidle")
            if response is None or response.status != 200:
                raise RuntimeError("The fictional saved-data page did not load")
            heading = page.get_by_text(marker, exact=False).first
            expect(heading).to_be_visible()
            heading.scroll_into_view_if_needed()
            page.evaluate("window.scrollBy(0, -120)")
            page.screenshot(path=str(output), full_page=False)
            context.close()
            browser.close()
    finally:
        _stop_server(server, thread)


def main() -> None:
    """Expose the reproducible fictional documentation capture command."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory", type=Path, default=REPOSITORY_ROOT / "docs/images/user"
    )
    parser.add_argument("--browser-channel", default="msedge")
    parser.add_argument("--browser-executable", type=Path)
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Keep an isolated fixture open for native browser capture.",
    )
    arguments = parser.parse_args()
    if arguments.serve:
        serve()
        return
    for path in capture(
        arguments.output_directory,
        browser_channel=arguments.browser_channel,
        browser_executable=arguments.browser_executable,
    ):
        print(path)


def serve() -> None:
    """Serve fictional records until interrupted, without launching a browser."""

    fixture = SavedOdooArchiveLifecycleTests(methodName="runTest")
    fixture.setUp()
    server = thread = None
    try:
        definition, _version, _publication = fixture._captured_version(
            19, connection_mode="LOCAL"
        )
        server, thread, port = _start_server(fixture.app)
        print(f"http://127.0.0.1:{port}/launch?token=launch-secret", flush=True)
        print(
            f"http://127.0.0.1:{port}/projects/{definition.project_id}/saved-odoo-data?selected={definition.saved_data_id}",
            flush=True,
        )
        while thread.is_alive():
            thread.join(timeout=1)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None and thread is not None:
            _stop_server(server, thread)
        fixture.tearDown()


if __name__ == "__main__":
    main()
