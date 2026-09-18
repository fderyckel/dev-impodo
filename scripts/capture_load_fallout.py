"""Capture the documented post-load fallout outcome from fictional data."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import socket
import sys
from threading import Thread
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import uvicorn

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.reconciliation_detail import (
    ReconciliationDetailArtifact,
    ReconciliationFieldDifference,
)
from impodo.web.security import LoopbackSecurityMiddleware
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase


DEFAULT_OUTPUT = REPOSITORY_ROOT / "docs/images/user/18-load-fallout.png"


def _start_server(app) -> tuple[uvicorn.Server, Thread, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    expected_host = f"127.0.0.1:{port}"
    for middleware in app.user_middleware:
        if middleware.cls is LoopbackSecurityMiddleware:
            middleware.kwargs["expected_host"] = expected_host
    app.middleware_stack = None
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="impodo-fallout-screenshot-server",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("The isolated screenshot server did not start")
    return server, thread, port


def _stop_server(server: uvicorn.Server, thread: Thread) -> None:
    server.should_exit = True
    thread.join(timeout=15)
    if thread.is_alive():
        raise RuntimeError("The isolated screenshot server did not stop")


def _fictional_report(workspace_id: str, run_id: str):
    hashes = ("sha256:" + "1" * 64, "sha256:" + "2" * 64)
    rows = tuple(
        ReconciliationRow(
            row_id=f"product-{index}",
            dataset="Products",
            source_row=index + 1,
            target_model="product.template",
            operation="CREATE",
            execution_status="COMMITTED",
            status=(
                ReconciliationRowStatus.DIFFERENT
                if index <= 3
                else ReconciliationRowStatus.VERIFIED
            ),
            odoo_id=100 + index,
            differing_fields=(
                ("weight", "description")
                if index == 1
                else (("weight",) if index <= 3 else ())
            ),
            message="Odoo returned the final stored values",
        )
        for index in range(1, 6)
    )
    report = ReconciliationRun(
        reconciliation_id=str(uuid4()),
        workspace_id=workspace_id,
        execution_run_id=run_id,
        snapshot_hash=hashes[0],
        target_hash=hashes[1],
        target_database="Migration rehearsal",
        status=ReconciliationRunStatus.FALLOUT,
        verified_at=datetime.now(timezone.utc),
        verified_by="Local operator",
        unchanged_count=0,
        rows=rows,
    )
    differences = []
    for row in rows[:3]:
        differences.append(
            ReconciliationFieldDifference(
                row_id=row.row_id,
                dataset=row.dataset,
                source_row=row.source_row,
                source_trace_id=f"trace-{row.source_row}",
                target_model=row.target_model,
                operation=row.operation,
                odoo_id=row.odoo_id or 0,
                field="weight",
                expected_value=Decimal("0.003"),
                observed_value=0.0,
                field_type="float",
                target_digits=(16, 2),
                reason_code="TARGET_NUMERIC_PRECISION_LOSS",
            )
        )
    first = rows[0]
    differences.append(
        ReconciliationFieldDifference(
            row_id=first.row_id,
            dataset=first.dataset,
            source_row=first.source_row,
            source_trace_id=f"trace-{first.source_row}",
            target_model=first.target_model,
            operation=first.operation,
            odoo_id=first.odoo_id or 0,
            field="description",
            expected_value=" Product description",
            observed_value="Product description",
            field_type="html",
            reason_code="HTML_CONTENT_DIFFERENT",
        )
    )
    detail = ReconciliationDetailArtifact(
        reconciliation_id=report.reconciliation_id,
        workspace_id=workspace_id,
        execution_run_id=run_id,
        snapshot_hash=report.snapshot_hash,
        target_hash=report.target_hash,
        differences=tuple(differences),
    )
    return report, detail


def capture(output: Path, *, browser_channel: str) -> None:
    from playwright.sync_api import expect, sync_playwright

    output.parent.mkdir(parents=True, exist_ok=True)
    fixture = ProjectSetupBrowserTestCase(methodName="runTest")
    fixture.setUp()
    server = None
    thread = None
    try:
        workspace = fixture.workspaces.create(
            name="Product migration",
            source_system="Excel",
        )
        run_id = str(uuid4())
        report, detail = _fictional_report(workspace.workspace_id, run_id)
        current_run = SimpleNamespace(
            run_id=run_id,
            rows=report.rows,
            committed_count=5,
            failed_count=0,
            blocked_count=0,
            partially_applied_count=0,
            unknown_count=0,
        )
        preview = SimpleNamespace(
            snapshot=SimpleNamespace(
                counts={"CREATE": 5, "UPDATE": 0, "UNCHANGED": 0},
                target_database="Migration rehearsal",
                target_odoo_version="19.0",
                semantic_hash=report.snapshot_hash,
                target_hash=report.target_hash,
            ),
            datasets=(),
            current_run=current_run,
            can_load=False,
        )
        context = fixture.app.state.context
        session_cookie = fixture.client.cookies.get("impodo_session")
        if not session_cookie:
            raise RuntimeError("The isolated setup did not create a session")
        fixture.client.close()
        with ExitStack() as patches:
            patches.enter_context(
                patch.object(type(context.execution), "current_preview", return_value=preview)
            )
            patches.enter_context(
                patch.object(type(context.reconciliation), "current", return_value=report)
            )
            patches.enter_context(
                patch.object(
                    type(context.reconciliation),
                    "current_detail_available",
                    return_value=True,
                )
            )
            patches.enter_context(
                patch.object(
                    type(context.reconciliation),
                    "current_detail",
                    return_value=detail,
                )
            )
            server, thread, port = _start_server(fixture.app)
            base_url = f"http://127.0.0.1:{port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    channel=browser_channel,
                    headless=True,
                )
                browser_context = browser.new_context(
                    viewport={"width": 1440, "height": 1024},
                    device_scale_factor=1,
                    locale="en-GB",
                )
                browser_context.add_cookies(
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
                page = browser_context.new_page()
                page.goto(
                    f"{base_url}/workspaces/{workspace.workspace_id}/load/outcome",
                    wait_until="networkidle",
                )
                expect(page.get_by_text("What needs review")).to_be_visible()
                expect(page.get_by_text("See the different values")).to_be_visible()
                expect(page.get_by_label("Find a record or value")).to_be_visible()
                expect(
                    page.get_by_text("Download highlighted source workbook (.xlsx)")
                ).to_be_visible()
                page.screenshot(path=str(output), full_page=True)
                browser_context.close()
                browser.close()
            _stop_server(server, thread)
            server = None
            thread = None
    finally:
        if server is not None and thread is not None:
            _stop_server(server, thread)
        fixture.tearDown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--browser-channel", default="msedge")
    arguments = parser.parse_args()
    capture(arguments.output.resolve(), browser_channel=arguments.browser_channel)


if __name__ == "__main__":
    main()
