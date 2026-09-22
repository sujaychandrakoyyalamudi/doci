import pytest
from doci.config import Settings
from doci.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def settings(tmp_path):
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path}/data.db",
        checkpoint_url=str(tmp_path / "checkpoints.db"),
        langsmith_tracing=False,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def create_case(client, title="Document completeness review"):
    response = client.post(
        "/api/cases",
        json={
            "title": title,
            "description": "Review the customer application and check documentation completeness.",
            "category": "Customer onboarding",
            "priority": "medium",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload_evidence(client, case_id, content="Customer application and registration are supplied."):
    response = client.post(
        "/api/documents",
        data={"case_id": case_id},
        files={"file": ("application.txt", content.encode(), "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload_policy(client):
    response = client.post(
        "/api/documents",
        headers={"X-Demo-Role": "reviewer"},
        files={
            "file": (
                "policy.txt",
                b"Document review policy. Review each source and require independent human approval.",
                "text/plain",
            )
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def ready_case(client):
    case_id = create_case(client)
    upload_evidence(client, case_id)
    upload_policy(client)
    response = client.post(f"/api/cases/{case_id}/reviews")
    assert response.status_code == 202, response.text
    detail = client.get(f"/api/cases/{case_id}").json()
    assert detail["status"] == "awaiting_approval", detail
    return detail


def decide(
    client, detail, decision="approve", comment="Reviewed and verified the source documents."
):
    return client.post(
        f"/api/runs/{detail['run']['id']}/decision",
        headers={"X-Demo-Role": "approver"},
        json={
            "decision": decision,
            "comment": comment,
            "proposal_hash": detail["run"]["proposal_hash"],
        },
    )
