from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request


@dataclass(frozen=True)
class Actor:
    id: str
    tenant_id: str
    role: str
    name: str


DEMO_ACTORS = {
    "analyst": Actor("demo-analyst", "demo", "analyst", "Local analyst"),
    "approver": Actor("demo-approver", "demo", "approver", "Local approver"),
    "submitter": Actor("demo-submitter", "demo", "submitter", "Local submitter"),
    "reviewer": Actor("demo-reviewer", "demo", "reviewer", "Local reviewer"),
}


def current_actor(request: Request) -> Actor:
    settings = request.app.state.services.settings
    if settings.auth_mode == "demo":
        role = request.headers.get("x-demo-role", "analyst")
        if role not in DEMO_ACTORS:
            raise HTTPException(401, "Unknown demo role")
        return DEMO_ACTORS[role]
    token = request.headers.get("authorization", "")
    if not token.startswith("Bearer "):
        raise HTTPException(401, "An Identity Platform ID token is required")
    try:
        import firebase_admin
        from firebase_admin import auth

        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app(options={"projectId": settings.google_cloud_project})
        claims = auth.verify_id_token(token[7:], check_revoked=True)
        # Trusted custom claims are assigned by an administrator, never by the client.
        tenant = claims.get("tenant_id")
        role = claims.get("role")
        if (
            not isinstance(tenant, str)
            or not tenant
            or role not in {"submitter", "analyst", "reviewer", "approver", "admin"}
        ):
            raise ValueError("Missing authorization claims")
        return Actor(claims["uid"], tenant, role, claims.get("name", "Team member"))
    except Exception as exc:
        raise HTTPException(401, "Invalid token or missing tenant/role claims") from exc


def require_roles(*roles: str):
    def dependency(actor: Annotated[Actor, Depends(current_actor)]):
        if actor.role not in {*roles, "admin"}:
            raise HTTPException(403, "Your role cannot perform this action")
        return actor

    return dependency


def verify_task(request: Request):
    settings = request.app.state.services.settings
    if settings.queue_backend != "cloud_tasks":
        raise HTTPException(404, "Worker endpoint is disabled in local mode")
    token = request.headers.get("authorization", "")
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token

        if not token.startswith("Bearer "):
            raise ValueError("Missing bearer token")
        claims = id_token.verify_oauth2_token(
            token[7:], GoogleRequest(), audience=settings.worker_url
        )
        if claims.get("email") != settings.task_service_account or not claims.get("email_verified"):
            raise ValueError("Invalid service identity")
    except Exception as exc:
        raise HTTPException(401, "Invalid worker identity") from exc
