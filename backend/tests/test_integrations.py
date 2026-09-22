import json
import os

import pytest
from doci.agents import Agents
from doci.config import Settings
from doci.integrations.a2a_server import create_reviewer
from doci.main import create_app
from fastapi.testclient import TestClient

from .conftest import decide, ready_case


def test_a2a_sdk_card_and_independent_review(settings):
    evidence = [
        {
            "chunk_id": "c1",
            "document_id": "d1",
            "filename": "case.txt",
            "page": 1,
            "quote": "The customer application is supplied.",
            "kind": "evidence",
            "score": 1,
        },
        {
            "chunk_id": "c2",
            "document_id": "d2",
            "filename": "policy.txt",
            "page": 1,
            "quote": "Human approval is required.",
            "kind": "policy",
            "score": 1,
        },
    ]
    analysis = Agents(settings).analyze(
        {"title": "Remote review", "description": "Check case", "evidence": evidence}
    )
    with TestClient(create_reviewer(settings)) as client:
        card = client.get("/.well-known/agent-card.json")
        assert card.status_code == 200
        assert card.json()["skills"][0]["id"] == "independent_review"
        response = client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": "test",
                "method": "message/send",
                "params": {
                    "message": {
                        "kind": "message",
                        "role": "user",
                        "messageId": "m1",
                        "parts": [
                            {
                                "kind": "text",
                                "text": json.dumps(
                                    {"evidence": evidence, "analysis": analysis.model_dump()}
                                ),
                            }
                        ],
                    }
                },
            },
        )
        assert response.status_code == 200
        review = json.loads(response.json()["result"]["parts"][0]["text"])
        assert review["verdict"] == "pass"


def test_mcp_action_tool_cannot_create_approval(monkeypatch):
    from doci.integrations import mcp_server

    monkeypatch.setattr(mcp_server, "read_case", lambda _: {"decision": None})
    with pytest.raises(ValueError, match="human approval"):
        mcp_server.retry_approved_action("case-id")


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="Set TEST_POSTGRES_URL to run pgvector/Postgres integration",
)
def test_postgres_checkpoint_and_action(tmp_path):
    url = os.environ["TEST_POSTGRES_URL"]
    checkpoint_url = url.replace("postgresql+psycopg://", "postgresql://")
    # A migration lock regression should fail promptly instead of hanging CI.
    checkpoint_url += (
        "&" if "?" in checkpoint_url else "?"
    ) + "options=-c%20statement_timeout%3D15000"
    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=url,
        checkpoint_url=checkpoint_url,
        langsmith_tracing=False,
    )
    with TestClient(create_app(settings)) as client:
        detail = ready_case(client)
    with TestClient(create_app(settings)) as client:
        assert decide(client, detail).status_code == 202
        result = client.get(f"/api/cases/{detail['id']}").json()
        assert result["status"] == "resolved"
