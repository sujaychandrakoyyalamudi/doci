import pytest
from doci.auth import Actor, current_actor
from doci.config import Settings
from pydantic import ValidationError

from .conftest import create_case, decide, ready_case, upload_evidence, upload_policy


def test_role_enforcement(client):
    detail = ready_case(client)
    body = {
        "decision": "approve",
        "comment": "I cannot approve this.",
        "proposal_hash": detail["run"]["proposal_hash"],
    }
    assert client.post(f"/api/runs/{detail['run']['id']}/decision", json=body).status_code == 403
    assert (
        client.post(
            "/api/documents", files={"file": ("policy.txt", b"Unauthorized policy")}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/cases/{detail['id']}/reviews", headers={"X-Demo-Role": "submitter"}
        ).status_code
        == 403
    )
    assert client.get("/api/cases", headers={"X-Demo-Role": "admin"}).status_code == 401


def test_creator_cannot_approve_even_with_approver_role(client, app):
    detail = ready_case(client)
    app.dependency_overrides[current_actor] = lambda: Actor(
        "demo-analyst", "demo", "approver", "Same person"
    )
    assert decide(client, detail).status_code == 403


def test_tenant_isolation_across_every_resource(client, app):
    detail = ready_case(client)
    doc_id = detail["documents"][0]["id"]
    app.dependency_overrides[current_actor] = lambda: Actor(
        "outsider", "other-tenant", "admin", "Other tenant"
    )
    assert client.get("/api/cases").json() == []
    assert client.get("/api/documents").json() == []
    assert client.get("/api/activity").json() == []
    for path in (
        f"/api/cases/{detail['id']}",
        f"/api/documents/{doc_id}/content",
        f"/api/cases/{detail['id']}/search?q=application",
    ):
        assert client.get(path).status_code == 404
    assert decide(client, detail).status_code == 404
    assert client.post(f"/api/runs/{detail['run']['id']}/retry").status_code == 404
    assert client.post(f"/api/cases/{detail['id']}/reviews").status_code == 404
    assert (
        client.post(
            "/api/documents",
            data={"case_id": detail["id"]},
            files={"file": ("a.txt", b"unauthorized")},
        ).status_code
        == 404
    )


def test_search_never_reads_another_case(client):
    first = create_case(client, "First customer")
    second = create_case(client, "Second customer")
    upload_evidence(client, first, "First customer unique text: papaya reference.")
    upload_evidence(client, second, "Second customer SECRET mango reference.")
    upload_policy(client)
    results = client.get(f"/api/cases/{first}/search?q=SECRET mango").json()
    assert all("SECRET" not in item["quote"] for item in results)
    assert {r["kind"] for r in results} == {"evidence", "policy"}


@pytest.mark.parametrize(
    "filename,body,status",
    [
        ("bad.pdf", b"not pdf", 415),
        ("executable.exe", b"MZtest", 415),
        ("empty.txt", b"", 413),
        ("binary.txt", b"a\x00b", 422),
        ("nonutf.txt", b"\xff\xfe", 422),
    ],
)
def test_invalid_document_uploads(client, filename, body, status):
    case_id = create_case(client)
    response = client.post(
        "/api/documents", data={"case_id": case_id}, files={"file": (filename, body)}
    )
    assert response.status_code == status
    assert client.get("/api/documents").json() == []


def test_storage_paths_are_not_caller_controlled(client, app):
    case_id = create_case(client)
    response = client.post(
        "/api/documents",
        data={"case_id": case_id},
        files={"file": ("../../outside.txt", b"Safe document body")},
    )
    assert response.status_code == 201
    assert response.json()["filename"] == "outside.txt"
    assert "storage_key" not in response.json()
    assert not (app.state.services.settings.data_dir / "outside.txt").exists()
    content = client.get(f"/api/documents/{response.json()['id']}/content")
    assert content.content == b"Safe document body"
    assert content.headers["x-content-type-options"] == "nosniff"


def test_internal_worker_is_disabled_locally(client):
    assert client.post("/api/internal/runs/anything").status_code == 404


def test_production_refuses_demo_and_ephemeral_storage():
    with pytest.raises(ValidationError, match="Firebase"):
        Settings(app_env="production")
    with pytest.raises(ValidationError, match="GCS"):
        Settings(app_env="production", auth_mode="firebase", model_provider="vertex")


def test_firebase_mode_does_not_trust_demo_headers(settings):
    from doci.main import create_app
    from fastapi.testclient import TestClient

    settings.auth_mode = "firebase"
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/cases", headers={"X-Demo-Role": "approver"}).status_code == 401
        assert client.get("/api/config").status_code == 200


def test_upload_limit_is_enforced_before_multipart_parsing(client):
    response = client.post(
        "/api/documents",
        content=b"too large",
        headers={
            "Content-Length": str(20 * 1024 * 1024),
            "Content-Type": "application/octet-stream",
        },
    )
    assert response.status_code == 413


def test_streamed_request_size_is_bounded(settings):
    from doci.main import create_app
    from fastapi.testclient import TestClient

    settings.max_upload_bytes = 100
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/cases", content=iter([b"x" * 70000]), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413
