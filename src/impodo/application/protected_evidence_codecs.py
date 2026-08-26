"""Application ports for protected Odoo evidence encoding and verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from ..domain.odoo_provenance import (
    OdooCaptureOriginHeader,
    OdooOriginBatch,
    OdooProvenanceBinding,
)


@dataclass(frozen=True, slots=True)
class EncodedOdooProvenance:
    """Encrypted provenance bytes and the artifact roots that bind them."""

    encrypted_bytes: bytes
    logical_hash: str
    artifact_hash: str
    row_count: int


@dataclass(frozen=True, slots=True)
class EncodedOdooComparison:
    """Encrypted comparison bytes and the artifact roots that bind them."""

    encrypted_bytes: bytes
    logical_hash: str
    artifact_hash: str


class OdooProvenanceCodec(Protocol):
    """Encode and verify bounded origins for one authorized Odoo capture."""

    def encode_capture(
        self,
        *,
        binding: OdooProvenanceBinding,
        header: OdooCaptureOriginHeader,
        batches: Iterable[OdooOriginBatch],
        key: bytes,
    ) -> EncodedOdooProvenance: ...

    def decode_capture(
        self,
        *,
        binding: OdooProvenanceBinding,
        encrypted_bytes: bytes,
        expected_logical_hash: str,
        expected_artifact_hash: str,
        expected_row_count: int,
        key: bytes,
    ) -> tuple[OdooCaptureOriginHeader, tuple[OdooOriginBatch, ...]]: ...


class OdooComparisonCodec(Protocol):
    """Encode and verify one protected comparison under supplied bindings."""

    def encode_comparison(
        self,
        plaintext: bytes,
        *,
        authenticated_binding: bytes,
        key: bytes,
    ) -> EncodedOdooComparison: ...

    def decode_comparison(
        self,
        encrypted_bytes: bytes,
        *,
        authenticated_binding: bytes,
        expected_logical_hash: str,
        expected_artifact_hash: str,
        key: bytes,
    ) -> bytes: ...
