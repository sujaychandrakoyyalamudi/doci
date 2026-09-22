"""Operator-run batch jobs for Cloud Run Jobs. No public HTTP endpoint."""

import argparse

from sqlalchemy import select

from doci.config import Settings
from doci.db import Database
from doci.documents import DocumentService
from doci.models import Chunk


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["reindex"])
    args = parser.parse_args()
    settings = Settings()
    if settings.model_provider != "vertex":
        raise SystemExit(
            "Reindexing requires MODEL_PROVIDER=vertex; demo retrieval uses lexical search."
        )
    db = Database(settings)
    documents = DocumentService(settings, db)
    cursor = ""
    total = 0
    try:
        if args.command == "reindex":
            while True:
                with db.session() as session:
                    chunks = list(
                        session.scalars(
                            select(Chunk).where(Chunk.id > cursor).order_by(Chunk.id).limit(50)
                        )
                    )
                if not chunks:
                    break
                embeddings = documents.embeddings.embed_documents([chunk.text for chunk in chunks])
                with db.session.begin() as session:
                    for chunk, vector in zip(chunks, embeddings, strict=True):
                        session.get(Chunk, chunk.id).embedding = vector
                cursor = chunks[-1].id
                total += len(chunks)
            print(f"Reindexed {total} passages using {settings.embedding_model} (768 dimensions).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
