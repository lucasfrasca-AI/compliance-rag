"""The Identity Toolkit account lookup (issue #319), against an httpx mock
transport: what it asks, how it authorises, and that every way it can fail is a
`VerifierUnavailable` and never an account. No test calls Google.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.firebase_auth import (
    IDENTITY_TOOLKIT_SCOPE,
    IdentityToolkitAccounts,
)
from compliance_rag.adapters.interfaces import AccountStatus, VerifierUnavailable

PROJECT = "compliance-rag-test"
UID = "uid-secret-looking-123"
URL = f"https://identitytoolkit.googleapis.com/v1/projects/{PROJECT}/accounts:lookup"


async def _token() -> str:
    return "ya29.an-access-token"


def _directory(handler: httpx.MockTransport) -> IdentityToolkitAccounts:
    return IdentityToolkitAccounts(
        project_id=PROJECT, access_token=_token, client=httpx.AsyncClient(transport=handler)
    )


def _answering(response: httpx.Response) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _: response)


async def test_it_posts_the_uid_to_the_projects_lookup_with_the_runtime_identitys_token() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"users": [{"localId": UID, "validSince": "1700000000"}]})

    status = await _directory(httpx.MockTransport(respond)).lookup(UID)
    assert status == AccountStatus(disabled=False, valid_since=1_700_000_000)
    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", URL)
    assert json.loads(request.content) == {"localId": [UID]}
    assert request.headers["authorization"] == "Bearer ya29.an-access-token"
    assert "key" not in request.url.params  # no API key
    assert "x-goog-api-key" not in request.headers


def test_the_scope_is_one_google_documents_for_the_lookup() -> None:
    assert IDENTITY_TOOLKIT_SCOPE in {
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/identitytoolkit",
    }


@pytest.mark.parametrize("project", ["", "a/b", "a?x=1", "a b", "../x", "-a"])
def test_it_refuses_a_project_id_that_is_not_one(project: str) -> None:
    with pytest.raises(ValueError, match="FIREBASE_PROJECT_ID"):
        IdentityToolkitAccounts(project_id=project, access_token=_token)


def _timeout(_: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow")


def _refused(_: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no route")


FAILURES = [
    pytest.param(httpx.MockTransport(_timeout), id="timeout"),
    pytest.param(httpx.MockTransport(_refused), id="connection-refused"),
    pytest.param(_answering(httpx.Response(500)), id="server-error"),
    pytest.param(_answering(httpx.Response(503, json={"error": {"code": 503}})), id="unavailable"),
    pytest.param(
        _answering(httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})),
        id="permission-denied",
    ),
    pytest.param(_answering(httpx.Response(401)), id="unauthenticated"),
    pytest.param(_answering(httpx.Response(429)), id="rate-limited"),
    pytest.param(_answering(httpx.Response(302, headers={"Location": "/x"})), id="redirect"),
    pytest.param(_answering(httpx.Response(200, text="not json")), id="malformed-json"),
    pytest.param(_answering(httpx.Response(200, text="")), id="empty-body"),
    pytest.param(_answering(httpx.Response(200, json=["users"])), id="body-is-a-list"),
    pytest.param(_answering(httpx.Response(200, json={"users": "uid"})), id="users-is-a-string"),
    pytest.param(_answering(httpx.Response(200, json={"users": [UID]})), id="user-is-a-string"),
    pytest.param(
        _answering(httpx.Response(200, json={"users": [{"localId": "someone-else"}]})),
        id="another-accounts-record",
    ),
    pytest.param(_answering(httpx.Response(200, json={"users": [{}]})), id="no-local-id"),
    pytest.param(
        _answering(httpx.Response(200, json={"users": [{"localId": UID}, {"localId": UID}]})),
        id="two-records",
    ),
    pytest.param(
        _answering(httpx.Response(200, json={"users": [{"localId": UID, "disabled": "false"}]})),
        id="disabled-as-a-string",
    ),
    pytest.param(
        _answering(httpx.Response(200, json={"users": [{"localId": UID, "disabled": None}]})),
        id="disabled-null",
    ),
    pytest.param(
        _answering(httpx.Response(200, json={"users": [{"localId": UID, "validSince": 1}]})),
        id="valid-since-as-a-number",
    ),
    pytest.param(
        _answering(httpx.Response(200, json={"users": [{"localId": UID, "validSince": "-1"}]})),
        id="valid-since-negative",
    ),
    pytest.param(
        _answering(httpx.Response(200, json={"users": [{"localId": UID, "validSince": "soon"}]})),
        id="valid-since-not-a-number",
    ),
]


@pytest.mark.parametrize("transport", FAILURES)
async def test_every_failure_is_verifier_unavailable_and_never_an_account(
    transport: httpx.MockTransport,
) -> None:
    with pytest.raises(VerifierUnavailable):
        await _directory(transport).lookup(UID)


async def test_a_token_source_that_fails_is_verifier_unavailable() -> None:
    async def no_credentials() -> str:
        raise RuntimeError("could not find default credentials")

    directory = IdentityToolkitAccounts(
        project_id=PROJECT,
        access_token=no_credentials,
        client=httpx.AsyncClient(transport=_answering(httpx.Response(200, json={"users": []}))),
    )
    with pytest.raises(VerifierUnavailable):
        await directory.lookup(UID)


async def test_the_lookup_puts_only_the_outcome_and_disabled_on_a_span_and_nothing_in_a_log(
    span_exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    body = {"users": [{"localId": UID, "email": "private@acme.example", "disabled": True}]}
    await _directory(_answering(httpx.Response(200, json=body))).lookup(UID)
    with pytest.raises(VerifierUnavailable):
        await _directory(_answering(httpx.Response(500, text="private@acme.example"))).lookup(UID)

    recorded = [s for s in span_exporter.get_finished_spans() if s.name == "auth.lookup_account"]
    assert [dict(s.attributes or {}) for s in recorded] == [
        {"auth.lookup_outcome": "found", "auth.account_disabled": True},
        {"auth.lookup_outcome": "failed"},
    ]
    everything = " ".join(
        [r.getMessage() for r in caplog.records]
        + [
            str(v)
            for s in span_exporter.get_finished_spans()
            for v in (s.attributes or {}).values()
        ]
        + [str(s.status.description) for s in span_exporter.get_finished_spans()]
    )
    assert UID not in everything and "acme.example" not in everything


class _Credentials:
    def __init__(self) -> None:
        self.valid = False
        self.token: str | None = None
        self.refreshes = 0

    def refresh(self, request: object) -> None:
        self.refreshes += 1
        self.valid, self.token = True, "ya29.from-adc"


async def test_the_default_token_source_is_application_default_credentials_and_refreshes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.auth

    from compliance_rag.adapters.firebase_auth import adc_access_token

    credentials = _Credentials()

    def fake_default(*, scopes: list[str]) -> tuple[_Credentials, str]:
        asked.append(scopes)
        return credentials, "the-project"

    asked: list[list[str]] = []
    monkeypatch.setattr(google.auth, "default", fake_default)
    token = adc_access_token()
    assert [await token(), await token()] == ["ya29.from-adc"] * 2
    assert asked == [[IDENTITY_TOOLKIT_SCOPE]]  # no key file, no API key: ADC with one scope
    assert credentials.refreshes == 1  # valid the second time
