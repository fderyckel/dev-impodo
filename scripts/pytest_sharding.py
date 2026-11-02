"""Partition portable CI tests by stable module paths without extra dependencies.

Load explicitly with ``pytest -p scripts.pytest_sharding --shard-count 4
--shard-index 1``. Every test module belongs to exactly one shard, including
all parameterized cases and unittest subtests. Ordinary pytest runs stay whole.
"""

from hashlib import sha256

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Expose one-based shard arguments only when this plugin is loaded."""
    group = parser.getgroup("impodo-sharding")
    group.addoption("--shard-count", type=int, default=1)
    group.addoption("--shard-index", type=int, default=1)


def pytest_configure(config: pytest.Config) -> None:
    """Fail invalid allocations before collecting or running any tests."""
    count, index = config.getoption("shard_count"), config.getoption("shard_index")
    if count < 1 or not 1 <= index <= count:
        raise pytest.UsageError(
            "Require --shard-count >= 1 and 1 <= --shard-index <= --shard-count"
        )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Select complete modules; item names and collection order cannot move them."""
    count, index = config.getoption("shard_count"), config.getoption("shard_index")
    if count == 1:
        return
    selected, deselected = [], []
    for item in items:
        module = item.path.relative_to(config.rootpath).as_posix()
        shard = (
            int.from_bytes(sha256(module.encode("utf-8")).digest()[:8], "big") % count
            + 1
        )
        (selected if shard == index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
