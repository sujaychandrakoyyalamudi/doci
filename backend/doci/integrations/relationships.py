"""Optional Neo4j case-to-document relationships; no LLM-generated Cypher."""

from neo4j import GraphDatabase

from doci.config import Settings
from doci.models import Document


def index_document(settings: Settings, document: Document, chunks: list):
    with GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    ) as driver:
        driver.execute_query(
            """
            MERGE (t:Tenant {id: $tenant})
            MERGE (d:Document {id: $document, tenant_id: $tenant})
            SET d.kind = $kind, d.filename = $filename
            MERGE (t)-[:OWNS]->(d)
            WITH d
            UNWIND $chunks AS item
            MERGE (c:Passage {id: item.id, tenant_id: $tenant})
            SET c.page = item.page
            MERGE (d)-[:CONTAINS]->(c)
        """,
            tenant=document.tenant_id,
            document=document.id,
            filename=document.filename,
            kind=document.kind,
            chunks=[{"id": c.id, "page": c.page} for c in chunks],
        )
        if document.case_id:
            driver.execute_query(
                """
                MATCH (d:Document {id: $document, tenant_id: $tenant})
                MERGE (c:Case {id: $case, tenant_id: $tenant})
                MERGE (c)-[:HAS_EVIDENCE]->(d)
            """,
                tenant=document.tenant_id,
                document=document.id,
                case=document.case_id,
            )


def related_passage_ids(settings: Settings, tenant: str, case_id: str) -> list[str]:
    with GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    ) as driver:
        records, _, _ = driver.execute_query(
            """
            MATCH (c:Case {id: $case, tenant_id: $tenant})-[:HAS_EVIDENCE]->
                  (d:Document {tenant_id: $tenant})-[:CONTAINS]->(p:Passage {tenant_id: $tenant})
            RETURN p.id AS id LIMIT 50
        """,
            tenant=tenant,
            case=case_id,
        )
        return [record["id"] for record in records]
