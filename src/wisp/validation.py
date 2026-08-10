"""Validation helpers for security-sensitive configuration."""

from typing import cast

from pydantic import ValidationError
from pydantic_core import InitErrorDetails


def redact_validation_error_inputs(
    exc: ValidationError,
    *,
    field: str | None = None,
) -> ValidationError:
    """Return an equivalent error with inputs redacted at the selected field."""

    errors = exc.errors(include_url=False)
    matched = False
    for error in errors:
        location = error.get("loc", ())
        if field is None or (location and location[0] == field):
            error["input"] = "<redacted>"
            matched = True
    if not matched:
        return exc
    redacted = cast(list[InitErrorDetails], errors)
    return ValidationError.from_exception_data(exc.title, redacted)
