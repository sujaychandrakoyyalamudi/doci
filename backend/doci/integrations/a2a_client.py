import json

import httpx
from a2a.types import Message, SendMessageResponse, SendMessageSuccessResponse
from a2a.utils import get_message_text

from doci.config import Settings
from doci.models import uid
from doci.schemas import Review


def remote_review(settings: Settings, evidence: list[dict], analysis: dict) -> Review:
    url = settings.reviewer_a2a_url.rstrip("/")
    headers = {}
    if settings.app_env == "production":
        from google.auth.transport.requests import Request
        from google.oauth2.id_token import fetch_id_token

        headers["Authorization"] = f"Bearer {fetch_id_token(Request(), url)}"
    response = httpx.post(
        url + "/",
        json={
            "jsonrpc": "2.0",
            "id": uid(),
            "method": "message/send",
            "params": {
                "message": {
                    "kind": "message",
                    "role": "user",
                    "messageId": uid(),
                    "parts": [
                        {
                            "kind": "text",
                            "text": json.dumps({"evidence": evidence, "analysis": analysis}),
                        }
                    ],
                },
                "configuration": {"blocking": True},
            },
        },
        headers=headers,
        timeout=180,
    )
    response.raise_for_status()
    envelope = SendMessageResponse.model_validate(response.json()).root
    if not isinstance(envelope, SendMessageSuccessResponse) or not isinstance(
        envelope.result, Message
    ):
        raise ValueError("The A2A reviewer did not return a completed review message")
    return Review.model_validate_json(get_message_text(envelope.result))
