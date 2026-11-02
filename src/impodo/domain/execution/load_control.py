"""Versioned load policy, durable stopping state and bounded failure windows.

These settings supplement the immutable execution snapshot. Old journals without
settings retain stop-on-rejection semantics. No business values enter controls.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from enum import StrEnum

from impodo.domain.serialization import canonical_json, content_hash

from .odoo_write import OdooWriteRejected
from .write_diagnostics import WriteFailureCode


class RejectionPolicy(StrEnum):
    """Select whether qualified record rejections permit independent writes."""

    STOP = "STOP"
    CONTINUE = "CONTINUE"


class LoadControlState(StrEnum):
    """Persist the decision before the worker starts another transport call."""

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOPPED = "STOPPED"
    FINISHED = "FINISHED"


class LoadHalted(RuntimeError):
    """A durable control decision prevented a new in-flight batch."""


@dataclass(frozen=True, slots=True)
class FailureWindow:
    """Count definitive distinct results for one model and transport phase."""

    model: str
    operation: str
    consecutive: int = 0
    recent: tuple[bool, ...] = ()

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", self.model) is None
            or self.operation not in {"CREATE", "UPDATE", "COMPLETION"}
            or type(self.consecutive) is not int
            or not 0 <= self.consecutive <= 1_000_000_000
            or len(self.recent) > 100
            or any(type(item) is not bool for item in self.recent)
        ):
            raise ValueError("Load failure window is invalid")


@dataclass(frozen=True, slots=True)
class LoadControl:
    """Keep approved limits and the current bounded failure frontier together."""

    policy: RejectionPolicy = RejectionPolicy.STOP
    state: LoadControlState = LoadControlState.RUNNING
    reason: str = ""
    consecutive_limit: int = 10
    window_size: int = 50
    minimum_results: int = 20
    rejection_percent: int = 20
    isolation_limit: int = 25
    isolation_calls: int = 0
    windows: tuple[FailureWindow, ...] = ()
    contract_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy", RejectionPolicy(self.policy))
        object.__setattr__(self, "state", LoadControlState(self.state))
        if (
            any(
                type(value) is not int
                for value in (
                    self.contract_version,
                    self.consecutive_limit,
                    self.window_size,
                    self.minimum_results,
                    self.rejection_percent,
                    self.isolation_limit,
                    self.isolation_calls,
                )
            )
            or self.contract_version != 1
            or not 1 <= self.consecutive_limit <= 100
            or not 1 <= self.minimum_results <= self.window_size <= 100
            or not 1 <= self.rejection_percent <= 100
            or not 1 <= self.isolation_limit <= 100
            or not 0 <= self.isolation_calls <= self.isolation_limit
            or len(self.windows) > 1_000
            or len({(w.model, w.operation) for w in self.windows}) != len(self.windows)
            or any(
                len(w.recent) > self.window_size or w.consecutive < 0
                for w in self.windows
            )
            or self.reason
            not in {
                "",
                "USER_STOP",
                "CONSECUTIVE_REJECTIONS",
                "REJECTION_RATE",
                "ISOLATION_BUDGET",
            }
        ):
            raise ValueError("Load control is invalid")

    @property
    def content_hash(self) -> str:
        """Verify settings and counters independently of execution hashes."""
        return content_hash(asdict(self))

    def to_json(self) -> str:
        """Serialize only bounded technical settings and counts."""
        return canonical_json({**asdict(self), "content_hash": self.content_hash})

    @classmethod
    def from_json(cls, value: str) -> LoadControl:
        """Reject changed control evidence rather than guessing a policy."""
        payload = json.loads(value)
        expected = payload.pop("content_hash")
        payload["windows"] = tuple(
            FailureWindow(**{**w, "recent": tuple(w["recent"])})
            for w in payload["windows"]
        )
        result = cls(**payload)
        if result.content_hash != expected:
            raise ValueError("Load control hash is invalid")
        return result

    def require_running(self) -> None:
        """Prevent new transport while a pause or stop is durable."""
        if self.state is not LoadControlState.RUNNING:
            raise LoadHalted(self.reason or self.state.value)

    def settle(self, model: str, operation: str, *, rejected: bool) -> LoadControl:
        """Count a final row result once; rolled-back batch peers are excluded."""
        return self.settle_many(((model, operation, rejected),))

    def settle_many(self, results: Iterable[tuple[str, str, bool]]) -> LoadControl:
        """Evaluate after the whole settled receipt, including all accepted peers."""
        if self.state not in {LoadControlState.RUNNING, LoadControlState.PAUSED}:
            return self
        windows = {(w.model, w.operation): w for w in self.windows}
        accepted = False
        changed: set[tuple[str, str]] = set()
        for model, operation, rejected in results:
            previous = windows.get((model, operation), FailureWindow(model, operation))
            windows[(model, operation)] = replace(
                previous,
                consecutive=previous.consecutive + 1 if rejected else 0,
                recent=(*previous.recent, rejected)[-self.window_size :],
            )
            changed.add((model, operation))
            accepted = accepted or not rejected
        reason = ""
        if (
            self.policy is RejectionPolicy.CONTINUE
            and self.state is LoadControlState.RUNNING
        ):
            for key in sorted(changed):
                current = windows[key]
                if current.consecutive >= self.consecutive_limit:
                    reason = "CONSECUTIVE_REJECTIONS"
                    break
                if len(current.recent) >= self.minimum_results and sum(
                    current.recent
                ) * 100 >= self.rejection_percent * len(current.recent):
                    reason = "REJECTION_RATE"
                    break
            if (
                not reason
                and not accepted
                and self.isolation_calls >= self.isolation_limit
            ):
                reason = "ISOLATION_BUDGET"
        return replace(
            self,
            windows=tuple(windows[k] for k in sorted(windows)),
            isolation_calls=0 if accepted else self.isolation_calls,
            state=LoadControlState.PAUSED if reason else self.state,
            reason=reason or self.reason,
        )

    def isolate(self) -> LoadControl:
        """Bound additional calls when a batch cannot identify faulty records."""
        self.require_running()
        if self.isolation_calls >= self.isolation_limit:
            raise LoadHalted("ISOLATION_BUDGET")
        return replace(self, isolation_calls=self.isolation_calls + 1)

    def request_stop(self) -> LoadControl:
        """Keep the in-flight call intact while preventing subsequent calls."""
        if self.state in {LoadControlState.STOPPED, LoadControlState.FINISHED}:
            return self
        return replace(self, state=LoadControlState.STOP_REQUESTED, reason="USER_STOP")

    def continue_next_group(self) -> LoadControl:
        """Reset the probe window after a separately authorized recovery check."""
        if self.state is not LoadControlState.PAUSED:
            raise ValueError("This load is not paused")
        return replace(
            self,
            state=LoadControlState.RUNNING,
            reason="",
            isolation_calls=0,
            windows=(),
        )


def can_set_aside(error: OdooWriteRejected) -> bool:
    """Only known data rejections permit isolation or independent continuation."""
    return error.diagnostic is not None and error.diagnostic.code in {
        WriteFailureCode.REQUIRED_VALUE,
        WriteFailureCode.RELATED_RECORD,
        WriteFailureCode.CONSTRAINT,
        WriteFailureCode.COMPANY_MISMATCH,
        WriteFailureCode.VALIDATION,
    }
