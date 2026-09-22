"""Stateless independent reviewer using the official A2A SDK (message/send).

Run on localhost for development. GCP deployments require Cloud Run IAM; never
grant allUsers on this service. No tasks, documents, or conversation history are retained.
"""

import asyncio
import json
import os

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    InvalidParamsError,
    TaskNotCancelableError,
)
from a2a.utils import new_agent_text_message
from a2a.utils.errors import ServerError
from pydantic import BaseModel, Field, ValidationError

from doci.agents import Agents, citation_errors
from doci.config import Settings
from doci.observability import configure
from doci.schemas import Analysis, Evidence, Review


class ReviewRequest(BaseModel):
    evidence: list[Evidence] = Field(max_length=10)
    analysis: Analysis


class ReviewerExecutor(AgentExecutor):
    def __init__(self, agents: Agents):
        self.agents = agents

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        try:
            data = ReviewRequest.model_validate_json(context.get_user_input())
        except ValidationError as exc:
            raise ServerError(
                error=InvalidParamsError(
                    message="Expected a valid evidence/analysis review payload"
                )
            ) from exc
        evidence = [item.model_dump() for item in data.evidence]
        errors = citation_errors(data.analysis, evidence)
        if errors:
            review = Review(
                verdict="revise", reasoning="The citations need correction.", concerns=errors
            )
        else:
            # Invoke the independent chain directly to prevent recursive A2A delegation.
            review = await asyncio.to_thread(
                self.agents.reviewer.invoke,
                {
                    "payload": json.dumps(
                        {"evidence": evidence, "analysis": data.analysis.model_dump()}
                    )
                },
            )
        await event_queue.enqueue_event(new_agent_text_message(review.model_dump_json()))

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        raise ServerError(error=TaskNotCancelableError())


def create_reviewer(settings: Settings | None = None):
    # This service has no persistence or application-user authentication; Cloud Run
    # authenticates its service-to-service caller before a request reaches it.
    settings = settings or Settings(app_env="local", reviewer_a2a_url="")
    configure(settings)
    card = AgentCard(
        name="Doci Independent Reviewer",
        version="0.1.0",
        description="Checks source support and case recommendations; cannot approve or execute actions.",
        url=os.environ.get("A2A_PUBLIC_URL", "http://127.0.0.1:8001/"),
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="independent_review",
                name="Independent evidence review",
                description="Review a JSON evidence/analysis payload and return pass, revise, or escalate.",
                tags=["review", "evidence"],
                examples=["Review the cited passages supporting this proposed disposition."],
            )
        ],
    )
    handler = DefaultRequestHandler(
        agent_executor=ReviewerExecutor(Agents(settings)), task_store=InMemoryTaskStore()
    )
    return A2AStarletteApplication(
        agent_card=card, http_handler=handler, max_content_length=100_000
    ).build()


app = create_reviewer()
