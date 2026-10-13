"""What the API tests share (#146-#149): signing keys standing in for
Google's, a token forger, settings for the test tenant, the real verifier
over the stand-in keys, and a full set of fake services.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import time
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeGraphStore,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.firebase_auth import (
    FirebaseTokenVerifier,
    SigningKeys,
    SigningKeySource,
)
from compliance_rag.adapters.interfaces import VerifierUnavailable
from compliance_rag.api.deps import ApiServices
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.graph.mappings import MappingSet
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import Settings
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier

PROJECT = "compliance-rag-test"
TENANT = "acme"
MEMBER = "lead@acme.example"
KID = "test-key-1"


def _key_and_cert() -> tuple[rsa.RSAPrivateKey, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test signer")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM).decode()


SIGNING_KEY, CERT = _key_and_cert()
OTHER_KEY, _ = _key_and_cert()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def claims_for(**overrides: Any) -> dict[str, Any]:  # Any: JWT claim values
    now = int(time.time())
    claims: dict[str, Any] = {  # Any: JWT claim values
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": "uid-123",
        "iat": now,
        "exp": now + 3600,
        "auth_time": now - 60,
        "email": MEMBER,
        "email_verified": True,
        "firebase": {"sign_in_provider": "google.com", "identities": {}},
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def forge(
    header: object,
    claims: dict[str, Any],  # Any: JWT claim values
    *,
    key: rsa.RSAPrivateKey = SIGNING_KEY,
    sign: str = "rs256",
) -> str:
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    if sign == "rs256":
        signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    elif sign == "hs256-with-the-certificate":
        signature = hmac.new(CERT.encode(), signing_input.encode(), hashlib.sha256).digest()
    else:
        signature = b""
    return f"{signing_input}.{_b64(signature)}"


def mint(*, key: rsa.RSAPrivateKey = SIGNING_KEY, **overrides: Any) -> str:  # Any: claims
    return forge({"alg": "RS256", "kid": KID, "typ": "JWT"}, claims_for(**overrides), key=key)


class Keys(SigningKeySource):
    """The stand-in for Google's certificates: counts fetches, can fail."""

    def __init__(self, max_age: float = 3600.0) -> None:
        self.fetches = 0
        self.max_age = max_age
        self.failing = False

    async def fetch(self) -> SigningKeys:
        self.fetches += 1
        if self.failing:
            raise VerifierUnavailable("down")
        return SigningKeys(certificates={KID: CERT}, max_age=self.max_age)

    async def aclose(self) -> None:
        pass


def api_settings(**overrides: Any) -> Settings:  # Any: settings field values
    values: dict[str, Any] = {  # Any: settings field values
        "tenant_id": TENANT,
        "firebase_project_id": PROJECT,
        "api_members": f"{MEMBER}, Second@Acme.example",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg


def verifier_for(
    keys: Keys | None = None,
    now: list[float] | None = None,
    **settings: Any,  # Any: settings field values
) -> FirebaseTokenVerifier:
    configured = api_settings(**settings)
    clock = now if now is not None else [time.monotonic()]
    return FirebaseTokenVerifier(
        project_id=configured.firebase_project_id,
        members=configured.members,
        sign_in_providers=configured.sign_in_providers,
        keys=keys or Keys(),
        clock=lambda: clock[0],
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def fake_api_services(**fields: Any) -> ApiServices:  # Any: ApiServices fields
    """Every service a fake, over one fake ledger and clause store; any field
    can be replaced."""
    ledger = fields.pop("ledger", None) or FakeLedgerStore()
    clauses = fields.pop("clauses", None) or FakeClauseStore()
    defaults: dict[str, Any] = {  # Any: ApiServices fields
        "settings": api_settings(),
        "verifier": verifier_for(),
        "ledger": ledger,
        "graph": FakeGraphStore(MappingSet(files=()), clause_validity={}),
        "clauses": clauses,
        "pipeline": AssessmentPipeline(
            RetrievalPipeline(FakeRetrievalStore(ledger=ledger), FakeReranker()),
            FakeGenerator(),
            Verifier(clauses, ledger, CanonicalCitationVerifier()),
            ledger,
        ),
        "synthetic": SyntheticRegistry.load(),
    }
    return ApiServices(**{**defaults, **fields})
