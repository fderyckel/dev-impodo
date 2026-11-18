"""Run one fresh authenticated two-editor Match data sample without binding a port."""

import asyncio
import json
import platform
from contextlib import AsyncExitStack
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4

import duckdb
import httpx2 as httpx

from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.web.diagnostics import parse_server_timing
from impodo.web.mapping_catalog_runtime import MappingCatalogSearchCoordinator
from tests.integration.web.test_mapping_command_performance import command_entries
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.mapping_catalog import mapped_catalog, peak_memory_bytes


async def measure(fixture, workspace, dataset, key):
    """Start both editors on the same event loop and exercise real projection work."""
    base = f"/workspaces/{workspace}/mapping"
    cookie = fixture.client.cookies["impodo_session"]
    rows = []
    delays = []
    start = perf_counter()
    ids = [str(uuid4()), str(uuid4())]
    generation_count = 4
    entered = 0
    projecting = 0
    maximum = 0
    available = asyncio.Event()
    first_holds_gate = asyncio.Event()
    original = MappingCatalogSearchCoordinator.run_latest

    async def observe(coordinator, identity, generation, operation, *, work_key=None):
        nonlocal entered
        entered += 1
        if entered == generation_count:
            available.set()

        async def project():
            nonlocal projecting, maximum
            projecting += 1
            maximum = max(maximum, projecting)
            try:
                if identity[-2] == ids[0] and generation == 1:
                    first_holds_gate.set()
                    await asyncio.wait_for(available.wait(), 10)
                return await operation()
            finally:
                projecting -= 1

        return await original(
            coordinator, identity, generation, project, work_key=work_key
        )

    async def request(client, name, method, path, expected, **kwargs):
        before = perf_counter()
        response = await client.request(method, path, **kwargs)
        after = perf_counter()
        rows.append(
            {
                "request": name,
                "start_ms": round((before - start) * 1000, 2),
                "elapsed_ms": round((after - before) * 1000, 2),
                "status": response.status_code,
                "server_ms": parse_server_timing(
                    response.headers.get("server-timing", "")
                ),
            }
        )
        assert response.status_code == expected, (
            name,
            response.status_code,
            response.text[:200],
        )
        return response

    async def monitor():
        while True:
            tick = perf_counter()
            await asyncio.sleep(0.01)
            delays.append(round(max(0, (perf_counter() - tick) * 1000 - 10), 2))

    async with AsyncExitStack() as stack:
        editors = [
            await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=fixture.app),
                    base_url="http://testserver",
                    cookies={"impodo_session": cookie},
                )
            )
            for _ in ids
        ]
        await request(editors[0], "cold_page", "GET", base, 200)
        await request(editors[0], "warm_page", "GET", base, 200)
        for index in range(2):
            response = await request(
                editors[0],
                ["cold_search", "warm_search"][index],
                "GET",
                base + "/field-catalog?field_query=field_0999",
                200,
            )
            if index:
                assert response.headers["x-impodo-catalog-projection"] == "hit"

        def search(editor, generation, expected):
            name = f"editor_{editor + 1}_generation_{generation}"
            return request(
                editors[editor],
                name,
                "GET",
                base + "/field-catalog",
                expected,
                params={
                    "editor_id": ids[editor],
                    "generation": generation,
                    "field_query": f"field_{generation:04d}",
                },
            )

        def save(name, expected):
            entries = command_entries(
                fixture.csrf,
                dataset,
                key,
                version=1,
                action="save_progress",
                operation_id=str(uuid4()),
            )
            return request(
                editors[1],
                name,
                "POST",
                base + "/save",
                expected,
                json={"entries": entries},
                headers={**POST_HEADERS, "X-CSRF-Token": fixture.csrf},
            )

        async with asyncio.TaskGroup() as tasks:
            clock = tasks.create_task(monitor())
            await asyncio.sleep(0)
            try:
                with patch.object(
                    MappingCatalogSearchCoordinator, "run_latest", observe
                ):
                    first = tasks.create_task(search(0, 1, 204))
                    await asyncio.wait_for(first_holds_gate.wait(), 10)
                    saved, healthy, _, current, other = await asyncio.gather(
                        save("save", 200),
                        request(editors[0], "health", "GET", "/health", 200),
                        search(0, 2, 204),
                        search(0, 3, 200),
                        search(1, 1, 200),
                    )
                    await first
                    assert saved.json()["expected_working_draft_version"] == 2
                    assert healthy.json() == {"status": "ok"}
                    assert "field_0003" in current.text and "field_0001" in other.text
                    assert maximum == 1
            finally:
                clock.cancel()
        stale = await save("stale_save", 409)
        assert stale.json()["failure_code"] == "MAPPING_VERSION_CONFLICT"
        assert delays
    return {
        "requests": rows,
        "max_simultaneous_projections": maximum,
        "event_loop_max_delay_ms": max(delays),
        "event_loop_sample_count": len(delays),
        "peak_rss_bytes": peak_memory_bytes(),
    }


def main():
    """Print raw observations and enforce the documented comparable budgets."""
    fixture = ProjectSetupBrowserTestCase()
    fixture.setUp()
    try:
        workspace, dataset, key = mapped_catalog(fixture)
        sample = asyncio.run(measure(fixture, workspace, dataset, key))
    finally:
        fixture.tearDown()
    sample.update(
        build=PROCESS_BUILD_CONTRACT.application_build_id,
        platform=platform.platform(),
        architecture=platform.machine(),
        python=platform.python_version(),
        duckdb=duckdb.__version__,
        mapped_fields=1000,
        editors=2,
    )
    print(json.dumps(sample), flush=True)
    elapsed = {item["request"]: item["elapsed_ms"] for item in sample["requests"]}
    assert elapsed["health"] < 1000
    assert elapsed["cold_page"] < 20000
    assert elapsed["warm_search"] < 5000
    assert elapsed["save"] < 5000 and elapsed["stale_save"] < 5000
    burst = [row for row in sample["requests"] if row["request"].startswith("editor_")]
    assert (
        max(row["start_ms"] + row["elapsed_ms"] for row in burst)
        - min(row["start_ms"] for row in burst)
        < 10000
    )
    assert sample["peak_rss_bytes"] < 1024**3


if __name__ == "__main__":
    main()
