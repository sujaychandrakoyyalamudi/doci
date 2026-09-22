"""Import an operator-supplied dataset into an isolated evaluation workspace.

No document records are bundled. Input comes from a private local JSON file or
an access-controlled GCS object. Imports never create human decisions.
"""

import argparse
import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from doci.auth import Actor
from doci.config import Settings
from doci.db import Database
from doci.documents import DocumentService
from doci.models import AuditEvent, Case, Document
from doci.repository import Repository
from doci.schemas import CreateCase
from doci.workflow import Workflow

MAX_DATASET_BYTES = 5 * 1024 * 1024
BANNER = "SYNTHETIC TEST DATA — All organizations, people, amounts, references, and events are fictional.\n"


def valid_filename(value: str) -> str:
    if not value or len(value) > 255 or any(c in value for c in ("/", "\\", "\x00")):
        raise ValueError("Use a document filename without directory components")
    return value


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str
    content: str = Field(min_length=1, max_length=1_000_000)

    _filename = field_validator("filename")(valid_filename)


class CaseInput(CreateCase):
    # Preserve document bytes so repeated imports retain the same checksums.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    documents: list[tuple[str, str]] = Field(default_factory=list, max_length=50)

    @field_validator("documents")
    @classmethod
    def unique_documents(cls, documents):
        names = set()
        for filename, content in documents:
            valid_filename(filename)
            if filename in names or not content.strip() or len(content) > 1_000_000:
                raise ValueError("Documents need unique filenames and nonempty bounded content")
            names.add(filename)
        return documents


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    classification: Literal["synthetic"]
    as_of: date | None = None
    policies: list[PolicyInput] = Field(default_factory=list, max_length=50)
    cases: list[CaseInput] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_keys(self):
        if len({case.key for case in self.cases}) != len(self.cases):
            raise ValueError("Case keys must be unique within a dataset")
        if len({policy.filename for policy in self.policies}) != len(self.policies):
            raise ValueError("Policy filenames must be unique within a dataset")
        return self


def read_dataset(location: str) -> Dataset:
    parsed = urlparse(location)
    if parsed.scheme == "gs":
        if not parsed.netloc or not parsed.path.strip("/") or parsed.query or parsed.fragment:
            raise ValueError("Use gs://bucket/object for a private dataset")
        from google.cloud import storage

        blob = storage.Client().bucket(parsed.netloc).blob(parsed.path.lstrip("/"))
        data = blob.download_as_bytes(end=MAX_DATASET_BYTES)
    elif not parsed.scheme:
        with Path(location).open("rb") as source:
            data = source.read(MAX_DATASET_BYTES + 1)
    else:
        raise ValueError("Datasets must be local files or access-controlled GCS objects")
    if len(data) > MAX_DATASET_BYTES:
        raise ValueError("Dataset exceeds the 5 MiB input limit")
    return Dataset.model_validate_json(data)


def stable_id(tenant: str, version: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"doci:{version}:{tenant}:{key}"))


def load_testing_data(
    settings: Settings, tenant: str, dataset: Dataset, review_count: int = 0
) -> dict:
    if not tenant.startswith("test-"):
        raise ValueError("Evaluation imports require an isolated tenant beginning with test-")
    if not 0 <= review_count <= len(dataset.cases):
        raise ValueError("review_count must be between zero and the number of cases")
    version = dataset.dataset
    db = Database(settings)
    db.initialize()
    documents, repository = DocumentService(settings, db), Repository(db)
    submitter = Actor(f"fixture:{version}:submitter", tenant, "submitter", "Evaluation submitter")
    reviewer = Actor(
        f"fixture:{version}:policy-publisher", tenant, "reviewer", "Evaluation policy publisher"
    )
    analyst = Actor(f"fixture:{version}:analyst", tenant, "analyst", "Evaluation analyst")
    summary = {
        "dataset": version,
        "tenant": tenant,
        "cases_created": 0,
        "documents_created": 0,
        "reviews_started": 0,
        "case_ids": [],
    }

    def ensure_document(filename: str, content: str, case_id: str | None):
        if not filename.endswith(".csv") and not content.startswith(BANNER):
            content = BANNER + content
        body = content.encode()
        with db.session() as session:
            existing = session.scalar(
                select(Document).where(
                    Document.tenant_id == tenant,
                    Document.case_id == case_id,
                    Document.filename == filename,
                )
            )
            if existing:
                if existing.sha256 != hashlib.sha256(body).hexdigest():
                    raise ValueError("Document changed; publish a new dataset version")
                return
        documents.upload(reviewer if case_id is None else submitter, filename, body, case_id)
        summary["documents_created"] += 1

    try:
        with db.workflow_lock(f"fixtures:{tenant}:{version}", wait=True):
            for policy in dataset.policies:
                ensure_document(policy.filename, policy.content, None)
            for index, item in enumerate(dataset.cases, 1):
                case_id = stable_id(tenant, version, item.key)
                with db.session.begin() as session:
                    if not session.get(Case, case_id):
                        prefix = hashlib.sha256(f"{tenant}:{version}".encode()).hexdigest()[:8]
                        session.add(
                            Case(
                                id=case_id,
                                tenant_id=tenant,
                                reference=f"TST-{prefix.upper()}-{index:03d}",
                                title=item.title,
                                description=item.description,
                                category=item.category,
                                priority=item.priority,
                                created_by=submitter.id,
                                assigned_to="Review team",
                            )
                        )
                        session.add(
                            AuditEvent(
                                tenant_id=tenant,
                                case_id=case_id,
                                actor=submitter.id,
                                event="fixture.case_created",
                                detail=f"Evaluation import: {version}/{item.key}",
                                dedupe_key=f"fixture:{case_id}",
                            )
                        )
                        summary["cases_created"] += 1
                for filename, content in item.documents:
                    ensure_document(filename, content, case_id)
                summary["case_ids"].append(case_id)
            if review_count:
                workflow = Workflow(settings, db, documents)
                workflow.initialize()
                for case_id in summary["case_ids"][:review_count]:
                    with db.session() as session:
                        existing_run = session.get(Case, case_id).current_run_id
                    run_id = (
                        existing_run
                        or repository.create_run(analyst, case_id, settings.model_provider).id
                    )
                    workflow.execute(run_id, raise_errors=True)
                    summary["reviews_started"] += int(not existing_run)
            return summary
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=os.getenv("SEED_DATASET_URI", ""))
    parser.add_argument("--tenant", default="test-workspace")
    parser.add_argument("--review-count", type=int, default=0)
    parser.add_argument("--schema", action="store_true", help="Print the JSON input schema")
    args = parser.parse_args()
    if args.schema:
        print(json.dumps(Dataset.model_json_schema(), indent=2))
        return
    if not args.dataset:
        parser.error("Supply --dataset or SEED_DATASET_URI; no datasets are bundled")
    dataset = read_dataset(args.dataset)
    result = load_testing_data(Settings(), args.tenant, dataset, args.review_count)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
