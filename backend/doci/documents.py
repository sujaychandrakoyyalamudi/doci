import hashlib
import io
import logging
import re
from pathlib import Path

from fastapi import HTTPException
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pgvector.sqlalchemy import Vector
from sqlalchemy import cast, func, select, update

from doci.auth import Actor
from doci.config import Settings
from doci.db import Database
from doci.models import AuditEvent, Case, Chunk, Document, now, uid
from doci.schemas import Evidence

logger = logging.getLogger(__name__)
EDITABLE = {"draft", "rejected", "escalated", "needs_information"}


class DocumentService:
    def __init__(self, settings: Settings, db: Database):
        self.settings, self.db = settings, db
        self.splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=180)
        self._embeddings = None

    @property
    def embeddings(self):
        if self._embeddings is None:
            from langchain_google_genai import GoogleGenerativeAIEmbeddings

            self._embeddings = GoogleGenerativeAIEmbeddings(
                model=self.settings.embedding_model,
                vertexai=True,
                project=self.settings.google_cloud_project,
                location=self.settings.google_cloud_location,
                output_dimensionality=self.settings.embedding_dimensions,
            )
        return self._embeddings

    def extract(self, data: bytes, filename: str) -> tuple[list[tuple[int, str]], str]:
        suffix = Path(filename).suffix.lower()
        if suffix in {".txt", ".md", ".csv"}:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise HTTPException(422, "Text files must use UTF-8 encoding") from exc
            if "\x00" in text:
                raise HTTPException(422, "Binary content is not a text document")
            pages, content_type = [(1, text)], "text/plain"
        elif suffix == ".pdf" and data.startswith(b"%PDF-"):
            from pypdf import PdfReader

            try:
                reader = PdfReader(io.BytesIO(data))
                if reader.is_encrypted:
                    raise ValueError("Encrypted PDFs are not supported")
                if len(reader.pages) > self.settings.max_document_pages:
                    raise ValueError(f"PDF limit is {self.settings.max_document_pages} pages")
                pages = [(n + 1, page.extract_text() or "") for n, page in enumerate(reader.pages)]
                # Mixed scanned/text PDFs also need OCR; do not silently discard blank pages.
                if any(len(text.strip()) < 15 for _, text in pages):
                    if not self.settings.document_ai_processor:
                        raise ValueError("Scanned pages require a configured Document AI processor")
                    pages = self.ocr(data)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    422,
                    f"PDF could not be extracted: {type(exc).__name__}. Check page limits, encryption, and OCR configuration.",
                ) from exc
            content_type = "application/pdf"
        else:
            raise HTTPException(415, "Upload a valid PDF or UTF-8 TXT, Markdown, or CSV file")
        total_chars = sum(len(text) for _, text in pages)
        if total_chars > self.settings.max_extracted_chars:
            raise HTTPException(413, "Extracted text exceeds the document limit")
        if not any(text.strip() for _, text in pages):
            raise HTTPException(422, "The document contains no readable text")
        return pages, content_type

    def ocr(self, data: bytes) -> list[tuple[int, str]]:
        from google.api_core.client_options import ClientOptions
        from google.cloud import documentai

        client = documentai.DocumentProcessorServiceClient(
            client_options=ClientOptions(
                api_endpoint=f"{self.settings.document_ai_location}-documentai.googleapis.com"
            )
        )
        response = client.process_document(
            request=documentai.ProcessRequest(
                name=self.settings.document_ai_processor,
                raw_document=documentai.RawDocument(content=data, mime_type="application/pdf"),
            ),
            timeout=120,
        )
        document = response.document
        return [
            (
                int(page.page_number),
                "".join(
                    document.text[int(segment.start_index) : int(segment.end_index)]
                    for segment in page.layout.text_anchor.text_segments
                ),
            )
            for page in document.pages
        ]

    def _blob(self, key: str):
        from google.cloud import storage

        return (
            storage.Client(project=self.settings.google_cloud_project)
            .bucket(self.settings.gcs_bucket)
            .blob(key)
        )

    def store(self, key: str, data: bytes, content_type: str):
        if self.settings.storage_backend == "gcs":
            self._blob(key).upload_from_string(
                data, content_type=content_type, if_generation_match=0
            )
        else:
            path = self.settings.data_dir / "documents" / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def delete_blob(self, key: str):
        if self.settings.storage_backend == "gcs":
            self._blob(key).delete()
        else:
            (self.settings.data_dir / "documents" / key).unlink(missing_ok=True)

    def read(self, document: Document) -> bytes:
        if self.settings.storage_backend == "gcs":
            return self._blob(document.storage_key).download_as_bytes()
        return (self.settings.data_dir / "documents" / document.storage_key).read_bytes()

    def upload(self, actor: Actor, filename: str, data: bytes, case_id: str | None) -> Document:
        if not data or len(data) > self.settings.max_upload_bytes:
            raise HTTPException(413, "Upload a nonempty file of at most 15 MB")
        filename = Path(filename.replace("\\", "/")).name[:255]
        # Authorize before parsing, embedding, or writing any content.
        with self.db.session() as session:
            if case_id:
                case = session.scalar(
                    select(Case).where(Case.id == case_id, Case.tenant_id == actor.tenant_id)
                )
                if not case:
                    raise HTTPException(404, "Case not found")
                if case.status not in EDITABLE:
                    raise HTTPException(
                        409, "Evidence is locked while a review is active or resolved"
                    )
                version = case.version
            elif actor.role not in {"reviewer", "admin"}:
                raise HTTPException(403, "Only reviewers and administrators may publish policies")

        pages, content_type = self.extract(data, filename)
        chunks = [
            Chunk(id=uid(), tenant_id=actor.tenant_id, case_id=case_id, page=page, text=text)
            for page, content in pages
            for text in self.splitter.split_text(content)
        ]
        if self.settings.model_provider == "vertex":
            vectors = self.embeddings.embed_documents([chunk.text for chunk in chunks])
            for chunk, vector in zip(chunks, vectors, strict=True):
                chunk.embedding = vector
        doc_id = uid()
        # No caller-provided path components enter a filesystem or object key.
        tenant_prefix = hashlib.sha256(actor.tenant_id.encode()).hexdigest()[:24]
        key = f"{tenant_prefix}/{doc_id}/original"
        document = Document(
            id=doc_id,
            tenant_id=actor.tenant_id,
            case_id=case_id,
            filename=filename,
            content_type=content_type,
            storage_key=key,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            pages=len(pages),
            kind="evidence" if case_id else "policy",
            uploaded_by=actor.id,
        )
        self.store(key, data, content_type)
        try:
            with self.db.session.begin() as session:
                if case_id:
                    changed = session.execute(
                        update(Case)
                        .where(
                            Case.id == case_id,
                            Case.tenant_id == actor.tenant_id,
                            Case.version == version,
                            Case.status.in_(EDITABLE),
                        )
                        .values(version=version + 1, updated_at=now())
                    )
                    if changed.rowcount != 1:
                        raise HTTPException(409, "Case changed during upload; try again")
                session.add(document)
                session.flush()
                for chunk in chunks:
                    chunk.document_id = doc_id
                session.add_all(chunks)
                session.add(
                    AuditEvent(
                        tenant_id=actor.tenant_id,
                        case_id=case_id,
                        actor=actor.id,
                        event="document.uploaded",
                        detail=f"{filename} · {len(chunks)} searchable passages",
                    )
                )
        except Exception:
            try:
                self.delete_blob(key)
            except Exception:
                logger.warning("Orphan blob cleanup failed", extra={"document_id": doc_id})
            raise
        if self.settings.neo4j_uri:
            try:
                from doci.integrations.relationships import index_document

                index_document(self.settings, document, chunks)
            except Exception:
                logger.warning(
                    "Optional relationship index unavailable", extra={"document_id": doc_id}
                )
        return document

    def search(self, tenant_id: str, case_id: str, query: str, limit: int = 10) -> list[dict]:
        """Retrieve separate evidence and policy quotas with scope applied in SQL."""
        terms = set(re.findall(r"[a-z0-9]{3,}", query.lower()))
        query_vector = (
            self.embeddings.embed_query(query) if self.settings.model_provider == "vertex" else None
        )
        related_ids = set()
        if self.settings.neo4j_uri:
            try:
                from doci.integrations.relationships import related_passage_ids

                related_ids = set(related_passage_ids(self.settings, tenant_id, case_id))
            except Exception:
                logger.warning("Relationship retrieval unavailable; using SQL retrieval")
        results = []
        with self.db.session() as session:
            for policy, quota in ((False, max(1, limit * 3 // 5)), (True, max(1, limit * 2 // 5))):
                base = (
                    select(Chunk, Document)
                    .join(Document, Chunk.document_id == Document.id)
                    .where(
                        Chunk.tenant_id == tenant_id,
                        Document.tenant_id == tenant_id,
                        Document.kind == ("policy" if policy else "evidence"),
                        Document.case_id.is_(None) if policy else Document.case_id == case_id,
                    )
                )
                distances = {}
                if query_vector is not None and self.db.is_postgres:
                    distance = cast(Chunk.embedding, Vector(768)).cosine_distance(query_vector)
                    rows = session.execute(
                        base.add_columns(distance)
                        .where(Chunk.embedding.is_not(None))
                        .order_by(distance)
                        .limit(30)
                    ).all()
                    candidates = {row[0].id: (row[0], row[1]) for row in rows}
                    distances = {row[0].id: 1 - float(row[2]) for row in rows}
                    text_rank = func.ts_rank_cd(
                        func.to_tsvector("english", Chunk.text),
                        func.plainto_tsquery("english", query),
                    )
                    for chunk, doc in session.execute(base.order_by(text_rank.desc()).limit(30)):
                        candidates[chunk.id] = (chunk, doc)
                    rows = list(candidates.values())
                else:
                    rows = session.execute(base.limit(5000)).all()
                scored = []
                for chunk, document in rows:
                    words = set(re.findall(r"[a-z0-9]{3,}", chunk.text.lower()))
                    lexical = len(words & terms) / max(len(terms), 1)
                    score = (
                        0.7 * distances.get(chunk.id, 0) + 0.3 * lexical if distances else lexical
                    )
                    # Relationship results only boost SQL-authorized candidates.
                    if chunk.id in related_ids:
                        score += 0.03
                    scored.append((score, chunk, document))
                for score, chunk, document in sorted(scored, key=lambda row: (-row[0], row[1].id))[
                    :quota
                ]:
                    results.append(
                        Evidence(
                            chunk_id=chunk.id,
                            document_id=document.id,
                            filename=document.filename,
                            page=chunk.page,
                            quote=chunk.text,
                            kind=document.kind,
                            score=round(max(0, score), 4),
                        ).model_dump()
                    )
        return results
