"""Evaluate review chains using a private, operator-supplied JSON dataset."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from doci.agents import Agents, citation_errors
from doci.config import Settings
from doci.observability import configure
from doci.schemas import Evidence


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10_000)
    evidence: list[Evidence] = Field(max_length=100)


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["pass", "revise", "escalate"]


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: Inputs
    outputs: Reference


def read_dataset(path: Path) -> list[dict]:
    with path.open("rb") as source:
        data = source.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise ValueError("Evaluation dataset exceeds the 5 MiB limit")
    cases = TypeAdapter(list[EvaluationCase]).validate_json(data)
    if not cases:
        raise ValueError("An evaluation dataset must contain at least one case")
    return [case.model_dump() for case in cases]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, help="Private JSON input file")
    parser.add_argument("--schema", action="store_true", help="Print the JSON input schema")
    parser.add_argument(
        "--langsmith", action="store_true", help="Upload these records to LangSmith"
    )
    parser.add_argument("--vertex", action="store_true", help="Use the configured Vertex AI model")
    args = parser.parse_args()
    if args.schema:
        print(json.dumps(TypeAdapter(list[EvaluationCase]).json_schema(), indent=2))
        return
    if not args.dataset:
        parser.error("Supply --dataset; no evaluation records are bundled")
    cases = read_dataset(args.dataset)
    settings = Settings(
        app_env="test",
        model_provider="vertex" if args.vertex else "demo",
        langsmith_tracing=args.langsmith,
    )
    telemetry = configure(settings)
    agents = Agents(settings)

    def target(inputs):
        analysis = agents.analyze(inputs)
        review = agents.review(inputs["evidence"], analysis.model_dump())
        return {
            "verdict": review.verdict,
            "valid_citations": not citation_errors(analysis, inputs["evidence"]),
        }

    def correct_verdict(outputs, reference_outputs):
        return {
            "key": "review_verdict",
            "score": outputs["verdict"] == reference_outputs["verdict"],
        }

    if args.langsmith:
        from langsmith.evaluation import evaluate

        digest = hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest()[:12]
        name = f"doci-review-{digest}"
        client = telemetry.client
        try:
            if not client.has_dataset(dataset_name=name):
                dataset = client.create_dataset(
                    name, description="Operator-supplied review evaluation"
                )
                client.create_examples(dataset_id=dataset.id, examples=cases)
            evaluate(
                target,
                data=name,
                evaluators=[correct_verdict],
                experiment_prefix="doci-review",
                client=client,
            )
        finally:
            telemetry.shutdown()
    else:
        # Report numeric indexes, not document text or case titles.
        results = [
            {"case": index, **target(row["inputs"]), "expected": row["outputs"]["verdict"]}
            for index, row in enumerate(cases, 1)
        ]
        print(json.dumps(results, indent=2))
        if any(row["verdict"] != row["expected"] or not row["valid_citations"] for row in results):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
