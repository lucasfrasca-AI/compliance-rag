"""What every upload route shares (#297, #303): the limits, reading a bounded body, reading it as
one JSON shape, and the checks on a text document's fields.

A member's document (`checks.py`) and an admin's policy (`admin_policies.py`) are the same kind of
request: untrusted text in a JSON body, held to one size on the wire and one on the document, in a
format the route accepts. Each used to carry its own copy of the reader, and two copies of a rule
about the bytes a request may hold are two places for it to drift. This module is the one copy.

**Parametrised by the refusal, not by the route.** Everything here raises `UploadRefused`, a status
with an error code and words (and, for a 422, the validator's list of where and what). A route
makes it its own: `checks.py` answers it as the `HTTPException` it always raised (`.http()`), and
the policy routes subclass it with what their audit rows need (`policy_audit.PolicyRefused`).
Nothing here knows which route called it, so the one thing a route says for itself is its noun
("document", "policy") in the words of a refusal.

Order, as every upload route applies it, each before anything is written:
1. `read_body`: at most `MAX_REQUEST_BYTES` of the raw body, counted as it streams in, never
   parsed past the limit; a declared length over it is refused before a byte is read (413);
2. `parse_body`: JSON of the one shape the route's model says, no other key (422, where and what,
   never the input);
3. `check_document_size`: the document's UTF-8 bytes, counted before it is cut or parsed (413);
4. `format_of`: the file's extension picks the format and the route says which it accepts (415).
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ValidationError

from compliance_rag.corpus.models import validate_title
from compliance_rag.corpus.submitted_parser import SubmittedFormat, format_of_filename

MAX_REQUEST_BYTES = 1_048_576
"""The whole request body, 1 MiB: what is read from the wire at most. Over it,
the request is refused as it streams in. JSON can spend up to six bytes on one
character (`\\u0000`), so this is room for a document at its limit with the
envelope, and a document of control characters over a sixth of its size is
refused here instead."""
MAX_DOCUMENT_BYTES = 204_800
"""The document's UTF-8 bytes, 200 KiB: a long contract or policy (about 35,000
words). Checked on the raw bytes, before anything is cut into passages."""
MAX_FILENAME_CHARS = 255
MAX_TITLE_CHARS = 200


class UploadRefused(Exception):
    """An upload a route refuses, in the status and words it is answered with."""

    def __init__(
        self,
        status: int,
        error: str,
        message: str,
        *,
        detail: object = None,
        where: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.detail = detail
        """The JSON body when it is not `{error, message}` (a validation error's list)."""
        self.where = where
        """For a validation error, which fields (`body.title, body.content`): what an audit row
        may say of it, since the input itself never is."""

    def http(self) -> HTTPException:
        body = (
            self.detail
            if self.detail is not None
            else {"error": self.error, "message": self.message}
        )
        return HTTPException(self.status, body)


def too_large_request(noun: str) -> UploadRefused:
    return UploadRefused(
        413,
        "too_large",
        f"the request is over {MAX_REQUEST_BYTES:,} bytes; a {noun} may be up to "
        f"{MAX_DOCUMENT_BYTES:,} bytes",
    )


async def read_body(request: Request, *, noun: str) -> bytes:
    """The request's bytes, never more than `MAX_REQUEST_BYTES` of them: a declared length over
    the limit is refused before any is read, and an undeclared or understated one is refused as
    it streams past the limit. `noun` is what the route takes ("document", "policy"), for the
    words of the refusal. A declared length that is not ASCII digits (`"\\xb2".isdigit()` is true
    and `int("\\xb2")` raises) is no declared length."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        raise too_large_request(noun)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_REQUEST_BYTES:
            raise too_large_request(noun)
    return bytes(body)


def invalid_body(error: ValidationError | None) -> UploadRefused:
    """422 in the shape the app's validator handler gives (`app.py`): where and what, never the
    input. `None` is a body that is not JSON at all."""
    if error is None:
        detail = [{"loc": ["body"], "msg": "the body is not valid JSON", "type": "json_invalid"}]
    else:
        detail = [
            {"loc": ["body", *e["loc"]], "msg": str(e["msg"]), "type": e["type"]}
            for e in error.errors()
        ]
    where = ", ".join(".".join(str(part) for part in entry["loc"]) for entry in detail)
    return UploadRefused(422, "invalid_body", "the body is not valid", detail=detail, where=where)


def parse_body[M: BaseModel](model: type[M], body: bytes) -> M:
    """`body` as JSON of `model`'s one shape, or `UploadRefused` (422)."""
    try:
        raw: Any = json.loads(body)  # Any: untrusted JSON, validated next
        return model.model_validate(raw)
    except ValidationError as error:
        raise invalid_body(error) from None
    except (ValueError, RecursionError):  # not JSON: bad syntax, bad UTF-8, too deep
        raise invalid_body(None) from None


def check_document_size(size: int, *, noun: str) -> None:
    """`size`, a document's UTF-8 bytes, within `MAX_DOCUMENT_BYTES`, else 413."""
    if size > MAX_DOCUMENT_BYTES:
        raise UploadRefused(
            413,
            "too_large",
            f"the {noun} is {size:,} bytes; the limit is {MAX_DOCUMENT_BYTES:,}",
        )


def format_of(
    filename: str, *, accepted: Collection[SubmittedFormat], message: str
) -> SubmittedFormat:
    """The format the file's name says, if the route accepts it; else 415 with `message`."""
    try:
        fmt = format_of_filename(filename)
    except ValueError:
        raise UploadRefused(415, "unsupported_format", message) from None
    if fmt not in accepted:
        raise UploadRefused(415, "unsupported_format", message)
    return fmt


# ---- validators for the fields of an upload model ----------------------------------------


def one_line(value: str) -> str:
    """One line of text with a word in it, no control or format character: so it can't reshape a
    page, a prompt marker or a log line (`validate_title`, the rule for titles)."""
    try:
        validate_title(value)
    except ValueError:
        raise ValueError("must be one line of text, with no control characters") from None
    return value


def check_file_name(value: str) -> str:
    """A name, not a path: its extension picks the format and it is shown, so it is one line of
    text with no separator."""
    if "/" in value or "\\" in value:
        raise ValueError("a file name has no path separator")
    validate_title(value)  # one line, no control or format characters
    return value


def check_text(value: str) -> str:
    """A document's text: no NUL, and a form UTF-8 can hold (a lone surrogate is valid JSON and
    has none)."""
    if "\x00" in value:
        raise ValueError("text may not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("text must be valid Unicode") from None
    return value
