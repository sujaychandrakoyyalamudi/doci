from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["local", "test", "production"] = "local"
    model_provider: Literal["demo", "vertex"] = "demo"
    auth_mode: Literal["demo", "firebase"] = "demo"
    firebase_api_key: str = ""
    firebase_auth_domain: str = ""
    synthetic_workspace: bool = False
    database_url: str = "sqlite:///.data/doci.db"
    database_pool_size: int = 5
    database_max_overflow: int = 10
    checkpoint_url: str = ".data/checkpoints.db"
    data_dir: Path = Path(".data")
    storage_backend: Literal["local", "gcs"] = "local"
    queue_backend: Literal["local", "cloud_tasks"] = "local"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"
    gemini_model: str = "gemini-3.5-flash"
    gemini_location: str = "global"
    embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = 768
    gcs_bucket: str = ""
    document_ai_processor: str = ""
    document_ai_location: str = "us"
    cloud_tasks_queue: str = "doci-review"
    worker_url: str = ""
    task_service_account: str = ""
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "doci"
    langsmith_hide_inputs: bool = True
    langsmith_hide_outputs: bool = True
    neo4j_uri: str = ""
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    reviewer_a2a_url: str = ""
    max_upload_bytes: int = 15 * 1024 * 1024
    max_document_pages: int = 100
    max_extracted_chars: int = 1_000_000
    max_revisions: int = 2

    @model_validator(mode="after")
    def safe_production(self):
        if self.embedding_dimensions != 768:
            raise ValueError("The vector schema uses 768 dimensions; migrate before changing it.")
        if self.app_env == "production":
            if self.auth_mode != "firebase" or self.model_provider != "vertex":
                raise ValueError("Production requires Firebase authentication and Vertex AI.")
            if self.storage_backend != "gcs" or self.queue_backend != "cloud_tasks":
                raise ValueError("Production requires GCS and Cloud Tasks.")
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("Production requires Cloud SQL/PostgreSQL.")
            if not self.checkpoint_url.startswith("postgresql://"):
                raise ValueError("Production requires durable PostgreSQL checkpoints.")
            required = (
                self.google_cloud_project,
                self.gcs_bucket,
                self.worker_url,
                self.task_service_account,
            )
            if not all(required):
                raise ValueError(
                    "Production GCP project, bucket, worker URL and task identity are required."
                )
        return self
