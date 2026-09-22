"""A stdio MCP server that calls the authorized API and never accesses the database."""

import os
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("Doci evidence, delegation, and approved actions")


def request(method: str, path: str):
    base = os.environ.get("DOCI_API_URL", "http://127.0.0.1:8000")
    token = os.environ.get("DOCI_API_TOKEN")
    headers = (
        {"Authorization": f"Bearer {token}"}
        if token
        else {"X-Demo-Role": os.environ.get("DOCI_MCP_DEMO_ROLE", "analyst")}
    )
    response = httpx.request(method, f"{base.rstrip('/')}/api{path}", headers=headers, timeout=120)
    response.raise_for_status()
    return response.json()


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def read_case(case_id: str) -> dict:
    """Read a case, its cited evidence, agent reviews, and human decision."""
    return request("GET", f"/cases/{quote(case_id, safe='')}")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def search_evidence(case_id: str, query: str) -> list[dict]:
    """Search documents in this case and the caller's published policy library."""
    return request("GET", f"/cases/{quote(case_id, safe='')}/search?q={quote(query, safe='')}")


@server.tool()
def request_review(case_id: str) -> dict:
    """Request evidence analysis and independent review. This does not approve actions."""
    return request("POST", f"/cases/{quote(case_id, safe='')}/reviews")


@server.tool(annotations=ToolAnnotations(idempotentHint=True))
def retry_approved_action(case_id: str) -> dict:
    """Retry an interrupted, already human-approved disposition. Cannot create an approval."""
    case = read_case(case_id)
    if not case.get("decision") or case["decision"]["decision"] != "approve":
        raise ValueError("A recorded human approval is required")
    if not case.get("run"):
        raise ValueError("Review not found")
    return request("POST", f"/runs/{case['run']['id']}/retry")


if __name__ == "__main__":
    server.run(transport="stdio")
