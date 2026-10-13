"""Uvicorn entrypoint, in factory mode so importing this module builds
nothing: `uvicorn --factory compliance_rag.api.main:build_app` (#146). Builds
the API from `Settings` with the real Firebase verifier; tests build it with
`create_app` and fakes instead.
"""

from __future__ import annotations

from fastapi import FastAPI

from compliance_rag.api.app import ApiServices, create_app
from compliance_rag.api.auth import FirebaseCerts, FirebaseTokenVerifier
from compliance_rag.settings import Settings, get_settings


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    verifier = FirebaseTokenVerifier(
        project_id=settings.firebase_project_id,
        members=settings.members,
        fetch_certs=FirebaseCerts(),
    )
    return create_app(ApiServices(settings=settings, verifier=verifier))
