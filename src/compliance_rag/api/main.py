"""Uvicorn entrypoint, in factory mode so importing this module builds
nothing: `uvicorn --factory compliance_rag.api.main:build_app` (#146). Builds
the API from `Settings` with telemetry configured (the one sink, D-16) and the
real Firebase verifier; tests build it with `create_app` instead.
"""

from __future__ import annotations

from fastapi import FastAPI

from compliance_rag import telemetry
from compliance_rag.adapters.firebase_auth import FirebaseTokenVerifier, GoogleSigningKeys
from compliance_rag.api.app import ApiServices, create_app
from compliance_rag.settings import Settings, get_settings


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    verifier = FirebaseTokenVerifier(
        project_id=settings.firebase_project_id,
        members=settings.members,
        sign_in_providers=settings.sign_in_providers,
        keys=GoogleSigningKeys(),
    )
    telemetry.configure(settings)
    return create_app(ApiServices(settings=settings, verifier=verifier))
