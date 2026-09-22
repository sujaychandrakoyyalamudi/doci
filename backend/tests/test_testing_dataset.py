import json

import pytest
from doci.db import Database
from doci.main import create_app
from doci.models import Action, Case, Decision, Document, Run
from doci.seed_testing import CaseInput, Dataset, PolicyInput, load_testing_data, read_dataset
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select


@pytest.fixture
def dataset():
    # Minimal transient inputs exercise loader behavior without shipping document datasets.
    return Dataset(
        dataset="unit-v1",
        classification="synthetic",
        policies=[
            PolicyInput(filename="policy.txt", content="Require cited evidence and approval.")
        ],
        cases=[
            CaseInput(
                key="one",
                title="Document completeness",
                description="Review the available supporting record.",
                documents=[("record.txt", "A supporting record is present.\n")],
            )
        ],
    )


def test_external_dataset_import_is_idempotent(settings, dataset):
    result = load_testing_data(settings, "test-workspace", dataset)
    assert result["cases_created"] == 1
    assert result["documents_created"] == 2
    repeated = load_testing_data(settings, "test-workspace", dataset)
    assert repeated["cases_created"] == repeated["documents_created"] == 0
    assert repeated["case_ids"] == result["case_ids"]
    db = Database(settings)
    try:
        with db.session() as session:
            assert session.scalar(select(func.count()).select_from(Case)) == 1
            assert session.scalar(select(func.count()).select_from(Decision)) == 0
            assert session.scalar(select(func.count()).select_from(Action)) == 0
            assert session.scalar(select(func.count()).select_from(Run)) == 0
    finally:
        db.close()


def test_import_requires_an_isolated_tenant(settings, dataset):
    with pytest.raises(ValueError, match="isolated tenant"):
        load_testing_data(settings, "operational-workspace", dataset)


def test_import_review_never_creates_a_human_decision(settings, dataset):
    result = load_testing_data(settings, "test-review", dataset, review_count=1)
    assert result["reviews_started"] == 1
    assert (
        load_testing_data(settings, "test-review", dataset, review_count=1)["reviews_started"] == 0
    )
    db = Database(settings)
    try:
        with db.session() as session:
            assert session.scalar(select(func.count()).select_from(Decision)) == 0
            assert session.scalar(select(func.count()).select_from(Action)) == 0
            assert session.scalar(select(Run.status)) == "awaiting_approval"
    finally:
        db.close()


def test_import_refuses_changed_existing_documents(settings, dataset):
    load_testing_data(settings, "test-protected", dataset)
    db = Database(settings)
    with db.session.begin() as session:
        session.scalar(select(Document).where(Document.kind == "policy")).sha256 = "0" * 64
    db.close()
    with pytest.raises(ValueError, match="new dataset version"):
        load_testing_data(settings, "test-protected", dataset)


def test_dataset_reader_preserves_bytes_and_rejects_ambiguous_keys(tmp_path, dataset):
    path = tmp_path / "input.json"
    path.write_text(dataset.model_dump_json())
    assert read_dataset(str(path)).cases[0].documents[0][1].endswith("\n")
    body = dataset.model_dump(mode="json")
    body["cases"].append(body["cases"][0])
    path.write_text(json.dumps(body))
    with pytest.raises(ValidationError, match="unique"):
        read_dataset(str(path))


def test_dataset_reader_rejects_nonlocal_urls_and_path_filenames(tmp_path, dataset):
    with pytest.raises(ValueError, match="local files"):
        read_dataset("https://example.invalid/records.json")
    body = dataset.model_dump(mode="json")
    body["cases"][0]["documents"][0][0] = "../../record.txt"
    path = tmp_path / "input.json"
    path.write_text(json.dumps(body))
    with pytest.raises(ValidationError, match="directory components"):
        read_dataset(str(path))


def test_application_starts_without_bundled_records(settings):
    settings.app_env = "local"
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/cases").json() == []
        assert client.get("/api/documents").json() == []
