"""The `AccountDirectory` contract (issue #319): one definition, run against
`FakeAccountDirectory` and against `IdentityToolkitAccounts` over an httpx mock
transport that answers as the Identity Toolkit API's `accounts:lookup` does
(`users[].localId`, `.createdAt`, `.disabled`, `.validSince` as a string of seconds; a false
`disabled` left out; `users` left out for an account that isn't there). No test
calls Google.
"""

from __future__ import annotations

import json

import httpx

from compliance_rag.adapters.fakes import FakeAccountDirectory
from compliance_rag.adapters.firebase_auth import IdentityToolkitAccounts
from compliance_rag.adapters.interfaces import AccountDirectory, AccountStatus

ENABLED, DISABLED, REVOKED, MISSING = "uid-enabled", "uid-disabled", "uid-revoked", "uid-missing"
REVOKED_AT = 1_700_000_000
CREATED = "1600000000000"


async def assert_account_directory_contract(directory: AccountDirectory) -> None:
    """Needs `ENABLED`, `DISABLED`, `REVOKED` (tokens revoked at `REVOKED_AT`)
    and no `MISSING`."""
    assert await directory.lookup(ENABLED) == AccountStatus(disabled=False, valid_since=0)
    assert await directory.lookup(DISABLED) == AccountStatus(disabled=True, valid_since=0)
    assert await directory.lookup(REVOKED) == AccountStatus(disabled=False, valid_since=REVOKED_AT)
    assert await directory.lookup(MISSING) is None  # deleted: not an error, not an account
    assert await directory.lookup(ENABLED) == await directory.lookup(ENABLED)  # a read


def identity_toolkit_double() -> httpx.MockTransport:
    accounts: dict[str, dict[str, object]] = {
        ENABLED: {"localId": ENABLED, "createdAt": CREATED, "email": "someone@acme.example"},
        DISABLED: {"localId": DISABLED, "createdAt": CREATED, "disabled": True},
        REVOKED: {"localId": REVOKED, "createdAt": CREATED, "validSince": str(REVOKED_AT)},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        wanted: list[str] = json.loads(request.content)["localId"]
        users = [accounts[uid] for uid in wanted if uid in accounts]
        if not users:
            return httpx.Response(200, json={"kind": "identitytoolkit#GetAccountInfoResponse"})
        return httpx.Response(
            200, json={"kind": "identitytoolkit#GetAccountInfoResponse", "users": users}
        )

    return httpx.MockTransport(respond)


async def _token() -> str:
    return "an-access-token"


async def test_the_fake_directory_satisfies_the_contract() -> None:
    fake = FakeAccountDirectory()
    fake.register(ENABLED)
    fake.register(DISABLED, disabled=True)
    fake.register(REVOKED, valid_since=REVOKED_AT)
    await assert_account_directory_contract(fake)


async def test_the_identity_toolkit_adapter_satisfies_the_contract() -> None:
    directory = IdentityToolkitAccounts(
        project_id="compliance-rag-test",
        access_token=_token,
        client=httpx.AsyncClient(transport=identity_toolkit_double()),
    )
    await assert_account_directory_contract(directory)
    await directory.aclose()


async def test_an_unregistered_account_in_the_strict_fake_does_not_exist() -> None:
    assert await FakeAccountDirectory().lookup("anyone") is None
    assert await FakeAccountDirectory.all_enabled().lookup("anyone") == AccountStatus(False, 0)
