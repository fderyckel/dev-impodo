"""Typed binary codec and AES-256-GCM envelope for Odoo provenance.

The current Odoo policy caps one capture at 10,000 rows.  The narrow origin
columns are therefore assembled from page-sized frames into one bounded binary
payload, encoded once, hashed once logically, and encrypted once.  Business
values are deliberately absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
import struct
from typing import Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..application.protected_evidence_codecs import (
    EncodedOdooProvenance as PortEncodedOdooProvenance,
)
from ..domain.odoo_provenance import (
    OdooCaptureOriginHeader,
    OdooOriginBatch,
    OdooProvenanceBinding,
    OdooProvenanceError,
)
from ..domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY


_ENVELOPE_MAGIC = b"IPRVCP01"
_PLAIN_MAGIC = b"IPODOO01"
_CODEC_VERSION = 1
_NONCE_BYTES = 12
_BATCH_MARKER = 1
_END_MARKER = 0
_MISSING_TIMESTAMP = -(2**63)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ProtectedOdooProvenanceError(OdooProvenanceError):
    """Raised when encrypted provenance cannot be published or verified."""


@dataclass(frozen=True, slots=True)
class EncodedOdooProvenance:
    """One bounded encrypted candidate and its two required artifact roots."""

    encrypted_bytes: bytes
    logical_hash: str
    artifact_hash: str
    row_count: int


def encode_capture_provenance(
    *,
    binding: OdooProvenanceBinding,
    header: OdooCaptureOriginHeader,
    batches: Iterable[OdooOriginBatch],
    key: bytes,
) -> EncodedOdooProvenance:
    """Encode each typed batch once, then authenticate and encrypt the payload."""

    _require_key(key)
    initial = struct.pack(
        ">8sIQ",
        _PLAIN_MAGIC,
        _CODEC_VERSION,
        header.high_water_id,
    )
    payload = bytearray(initial)
    logical_digest = sha256(initial)
    row_count = 0
    next_ordinal = 1
    previous_id = 0
    for batch in batches:
        if batch.first_row_ordinal != next_ordinal:
            raise ProtectedOdooProvenanceError(
                "Odoo origin batches must be contiguous and start at one"
            )
        if batch.odoo_ids[0] <= previous_id:
            raise ProtectedOdooProvenanceError(
                "Odoo origin IDs must be strictly increasing across batches"
            )
        count = batch.row_count
        batch_header = struct.pack(
            ">BIQ",
            _BATCH_MARKER,
            count,
            batch.first_row_ordinal,
        )
        encoded_ids = struct.pack(f">{count}Q", *batch.odoo_ids)
        timestamps = tuple(_timestamp_micros(value) for value in batch.write_dates)
        encoded_dates = struct.pack(f">{count}q", *timestamps)
        for encoded_column in (batch_header, encoded_ids, encoded_dates):
            payload.extend(encoded_column)
            logical_digest.update(encoded_column)
        row_count += count
        if row_count > CURRENT_ODOO_SOURCE_POLICY.max_rows:
            raise ProtectedOdooProvenanceError(
                "Odoo provenance exceeds the current capture row limit"
            )
        next_ordinal += count
        previous_id = batch.odoo_ids[-1]
    if (row_count == 0 and header.high_water_id != 0) or (
        row_count > 0 and header.high_water_id < previous_id
    ):
        raise ProtectedOdooProvenanceError(
            "Odoo capture high-water ID does not cover the captured origins"
        )
    end_marker = struct.pack(">BQ", _END_MARKER, row_count)
    payload.extend(end_marker)
    logical_digest.update(end_marker)
    plaintext = bytes(payload)
    logical_hash = "sha256:" + logical_digest.hexdigest()
    nonce = os.urandom(_NONCE_BYTES)
    encrypted = _ENVELOPE_MAGIC + nonce + AESGCM(key).encrypt(
        nonce,
        plaintext,
        binding.authenticated_bytes(),
    )
    return EncodedOdooProvenance(
        encrypted_bytes=encrypted,
        logical_hash=logical_hash,
        artifact_hash=_hash_bytes(encrypted),
        row_count=row_count,
    )


def decode_capture_provenance(
    *,
    binding: OdooProvenanceBinding,
    encrypted_bytes: bytes,
    expected_logical_hash: str,
    expected_artifact_hash: str,
    expected_row_count: int,
    key: bytes,
) -> tuple[OdooCaptureOriginHeader, tuple[OdooOriginBatch, ...]]:
    """Verify exact bytes and AEAD binding before returning bounded columns."""

    _require_key(key)
    if len(encrypted_bytes) > _maximum_encrypted_bytes():
        raise ProtectedOdooProvenanceError("Encrypted Odoo provenance is oversized")
    if _hash_bytes(encrypted_bytes) != expected_artifact_hash:
        raise ProtectedOdooProvenanceError(
            "Encrypted Odoo provenance failed artifact hash verification"
        )
    if not encrypted_bytes.startswith(_ENVELOPE_MAGIC):
        raise ProtectedOdooProvenanceError("Encrypted Odoo provenance header is invalid")
    nonce_start = len(_ENVELOPE_MAGIC)
    nonce_end = nonce_start + _NONCE_BYTES
    if len(encrypted_bytes) <= nonce_end + 16:
        raise ProtectedOdooProvenanceError("Encrypted Odoo provenance is truncated")
    nonce = encrypted_bytes[nonce_start:nonce_end]
    try:
        plaintext = AESGCM(key).decrypt(
            nonce,
            encrypted_bytes[nonce_end:],
            binding.authenticated_bytes(),
        )
    except InvalidTag as error:
        raise ProtectedOdooProvenanceError(
            "Encrypted Odoo provenance authentication failed"
        ) from error
    if _hash_bytes(plaintext) != expected_logical_hash:
        raise ProtectedOdooProvenanceError(
            "Odoo provenance failed logical hash verification"
        )
    return _decode_plaintext(plaintext, expected_row_count=expected_row_count)


class ProtectedOdooProvenanceCodec:
    """Implement the application provenance codec port with AES-GCM envelopes."""

    def encode_capture(
        self,
        *,
        binding: OdooProvenanceBinding,
        header: OdooCaptureOriginHeader,
        batches: Iterable[OdooOriginBatch],
        key: bytes,
    ) -> PortEncodedOdooProvenance:
        encoded = encode_capture_provenance(
            binding=binding,
            header=header,
            batches=batches,
            key=key,
        )
        return PortEncodedOdooProvenance(
            encrypted_bytes=encoded.encrypted_bytes,
            logical_hash=encoded.logical_hash,
            artifact_hash=encoded.artifact_hash,
            row_count=encoded.row_count,
        )

    def decode_capture(
        self,
        *,
        binding: OdooProvenanceBinding,
        encrypted_bytes: bytes,
        expected_logical_hash: str,
        expected_artifact_hash: str,
        expected_row_count: int,
        key: bytes,
    ) -> tuple[OdooCaptureOriginHeader, tuple[OdooOriginBatch, ...]]:
        return decode_capture_provenance(
            binding=binding,
            encrypted_bytes=encrypted_bytes,
            expected_logical_hash=expected_logical_hash,
            expected_artifact_hash=expected_artifact_hash,
            expected_row_count=expected_row_count,
            key=key,
        )


def _decode_plaintext(
    plaintext: bytes,
    *,
    expected_row_count: int,
) -> tuple[OdooCaptureOriginHeader, tuple[OdooOriginBatch, ...]]:
    header_size = struct.calcsize(">8sIQ")
    if len(plaintext) < header_size + struct.calcsize(">BQ"):
        raise ProtectedOdooProvenanceError("Odoo provenance payload is truncated")
    magic, version, high_water_id = struct.unpack_from(">8sIQ", plaintext, 0)
    if magic != _PLAIN_MAGIC or version != _CODEC_VERSION:
        raise ProtectedOdooProvenanceError("Odoo provenance payload version is invalid")
    offset = header_size
    batches: list[OdooOriginBatch] = []
    row_count = 0
    while True:
        if offset >= len(plaintext):
            raise ProtectedOdooProvenanceError("Odoo provenance end marker is missing")
        marker = plaintext[offset]
        if marker == _END_MARKER:
            end_size = struct.calcsize(">BQ")
            if offset + end_size != len(plaintext):
                raise ProtectedOdooProvenanceError(
                    "Odoo provenance payload has trailing bytes"
                )
            _, recorded_count = struct.unpack_from(">BQ", plaintext, offset)
            if recorded_count != row_count or row_count != expected_row_count:
                raise ProtectedOdooProvenanceError(
                    "Odoo provenance row count is inconsistent"
                )
            break
        if marker != _BATCH_MARKER:
            raise ProtectedOdooProvenanceError("Odoo provenance batch marker is invalid")
        batch_header_size = struct.calcsize(">BIQ")
        if offset + batch_header_size > len(plaintext):
            raise ProtectedOdooProvenanceError("Odoo provenance batch is truncated")
        _, count, first_ordinal = struct.unpack_from(">BIQ", plaintext, offset)
        if not 1 <= count <= CURRENT_ODOO_SOURCE_POLICY.page_size:
            raise ProtectedOdooProvenanceError("Odoo provenance batch size is invalid")
        offset += batch_header_size
        ids_size = count * 8
        dates_size = count * 8
        if offset + ids_size + dates_size > len(plaintext):
            raise ProtectedOdooProvenanceError("Odoo provenance columns are truncated")
        ids = struct.unpack_from(f">{count}Q", plaintext, offset)
        offset += ids_size
        timestamps = struct.unpack_from(f">{count}q", plaintext, offset)
        offset += dates_size
        batches.append(
            OdooOriginBatch(
                first_row_ordinal=first_ordinal,
                odoo_ids=tuple(ids),
                write_dates=tuple(_datetime_from_micros(value) for value in timestamps),
            )
        )
        row_count += count
        if row_count > CURRENT_ODOO_SOURCE_POLICY.max_rows:
            raise ProtectedOdooProvenanceError(
                "Odoo provenance exceeds the current capture row limit"
            )
    return OdooCaptureOriginHeader(high_water_id=high_water_id), tuple(batches)


def _timestamp_micros(value: datetime | None) -> int:
    if value is None:
        return _MISSING_TIMESTAMP
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _datetime_from_micros(value: int) -> datetime | None:
    if value == _MISSING_TIMESTAMP:
        return None
    return _EPOCH + timedelta(microseconds=value)


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _require_key(key: bytes) -> None:
    if len(key) != 32:
        raise ProtectedOdooProvenanceError(
            "Odoo provenance encryption key must be 256 bits"
        )


def _maximum_encrypted_bytes() -> int:
    # Fixed-width origin columns plus bounded frame/header overhead. Keep a
    # conservative ceiling so malformed files are rejected before decryption.
    rows = CURRENT_ODOO_SOURCE_POLICY.max_rows
    batches = (rows + CURRENT_ODOO_SOURCE_POLICY.page_size - 1) // CURRENT_ODOO_SOURCE_POLICY.page_size
    return 64 + rows * 16 + batches * 32
