"""Forms web helpers."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request
from starlette.datastructures import FormData

from ..mapping_semantics import ValueMapping
from ..projects import ProjectError
from ..workspace import WorkspaceError
from .constants import (
    MAPPING_MAX_FORM_FIELDS,
    MAPPING_MAX_FORM_NAME_LENGTH,
    MAPPING_MAX_FORM_VALUE_LENGTH,
    MAPPING_MAX_JSON_ENTRIES,
    MAPPING_MAX_REQUEST_BYTES,
    VALUE_MATCH_MAX_SOURCE_CHOICES,
)
from .security import require_csrf


def _secure_form(
    request: Request,
    form: FormData,
    allowed_fields: set[str],
) -> None:
    require_csrf(request, _text(form, "csrf_token"))
    unexpected = {key for key, _value in form.multi_items()} - allowed_fields
    if unexpected:
        raise HTTPException(status_code=422, detail="Unexpected form fields")


def _is_json_request(request: Request) -> bool:
    return request.headers.get("content-type", "").partition(";")[0].strip() == (
        "application/json"
    )


async def _mapping_request_form(request: Request) -> FormData:
    body = await _bounded_request_body(
        request,
        maximum_bytes=MAPPING_MAX_REQUEST_BYTES,
    )
    content_type = request.headers.get("content-type", "").partition(";")[0].strip()
    if content_type == "application/json":
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=400,
                detail="Mapping request is not valid JSON",
            ) from error
        if not isinstance(payload, dict) or set(payload) != {"entries"}:
            raise HTTPException(
                status_code=422,
                detail="Mapping request has unexpected properties",
            )
        entries = payload["entries"]
        if not isinstance(entries, list):
            raise HTTPException(
                status_code=422,
                detail="Mapping entries must be a list",
            )
        if len(entries) > MAPPING_MAX_JSON_ENTRIES:
            raise HTTPException(
                status_code=413,
                detail="Mapping request contains too many entries",
            )
        pairs: list[tuple[str, str]] = []
        for item in entries:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Each mapping entry must contain a name and value",
                )
            pairs.append((item[0], item[1]))
        _validate_mapping_form_pairs(pairs)
        return FormData(pairs)
    if content_type == "application/x-www-form-urlencoded":
        try:
            pairs = parse_qsl(
                body.decode("ascii"),
                keep_blank_values=True,
                encoding="utf-8",
                errors="strict",
                max_num_fields=MAPPING_MAX_FORM_FIELDS,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise HTTPException(
                status_code=400,
                detail="Mapping form could not be read safely",
            ) from error
        _validate_mapping_form_pairs(pairs)
        return FormData(pairs)
    raise HTTPException(
        status_code=415,
        detail="Mapping saves require JSON or URL-encoded form data",
    )


async def _bounded_request_body(
    request: Request,
    *,
    maximum_bytes: int,
) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise HTTPException(
                status_code=413,
                detail="Mapping request is too large",
            )
    return bytes(body)


def _validate_mapping_form_pairs(pairs: list[tuple[str, str]]) -> None:
    for name, value in pairs:
        if not name or len(name) > MAPPING_MAX_FORM_NAME_LENGTH:
            raise HTTPException(
                status_code=422,
                detail="Mapping request contains an invalid field name",
            )
        if len(value) > MAPPING_MAX_FORM_VALUE_LENGTH:
            raise HTTPException(
                status_code=413,
                detail="A mapping value is too large",
            )


def _value_mappings_from_form(
    form,
    field_name: str,
) -> tuple[ValueMapping, ...]:
    raw_value = _text(form, field_name)
    if not raw_value:
        return ()
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ValueError("Matched choices could not be read") from error
    if not isinstance(payload, list) or len(payload) > VALUE_MATCH_MAX_SOURCE_CHOICES:
        raise ValueError("Matched choices are invalid")
    mappings: list[ValueMapping] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {
            "source_value",
            "target_value",
        }:
            raise ValueError("Matched choices are invalid")
        source_value = item["source_value"]
        target_value = item["target_value"]
        if not isinstance(source_value, str) or not isinstance(
            target_value,
            str,
        ):
            raise ValueError("Matched choices are invalid")
        mappings.append(ValueMapping(source_value, target_value))
    return tuple(mappings)


def _optional_nonnegative_query_int(value: str | None) -> int | None:
    try:
        parsed = int(value or "")
    except ValueError:
        return None
    return parsed if 0 <= parsed <= 9_223_372_036_854_775_807 else None


def _positive_query_int(value: str | None, *, default: int) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _revision(form: FormData) -> int:
    try:
        return int(_text(form, "revision"))
    except ValueError as error:
        raise ProjectError("Invalid project revision") from error


def _text(form: FormData, name: str) -> str:
    value = form.get(name, "")
    return value if isinstance(value, str) else ""


def _form_values(form: FormData) -> dict[str, str]:
    return {
        key: value
        for key, value in form.items()
        if isinstance(value, str) and key != "csrf_token"
    }


def _split_models(value: str) -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in re.split(r"[,\r\n]+", value)
        if part.strip()
    )


def _submitted_model_scope(form: FormData) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            model
            for value in form.getlist("permitted_models")
            for model in _split_models(str(value))
        )
    )


def _texts(form: FormData, name: str) -> tuple[str, ...]:
    return tuple(
        value
        for value in form.getlist(name)
        if isinstance(value, str) and value
    )


def _checked(form: FormData, name: str) -> bool:
    return _text(form, name) in {"1", "true", "on", "yes"}


def _optional_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise WorkspaceError("Invalid mapping parent version") from error
