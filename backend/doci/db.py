import hashlib
import threading
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from doci.config import Settings
from doci.models import Base


class Database:
    def __init__(self, settings: Settings):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.is_postgres = settings.database_url.startswith("postgresql")
        kwargs = {"pool_pre_ping": True}
        if self.is_postgres:
            kwargs.update(
                pool_size=settings.database_pool_size, max_overflow=settings.database_max_overflow
            )
        if not self.is_postgres:
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(settings.database_url, **kwargs)
        if not self.is_postgres:

            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

        self.session = sessionmaker(self.engine, expire_on_commit=False)
        self._local_lock = threading.RLock()

    def initialize(self):
        if self.is_postgres:
            with self.engine.begin() as connection:
                connection.execute(text("SELECT pg_advisory_xact_lock(71482031)"))
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                Base.metadata.create_all(connection)
        else:
            Base.metadata.create_all(self.engine)

    @contextmanager
    def workflow_lock(self, run_id: str, wait: bool = False):
        """Serialize retries across replicas; local mode intentionally uses one worker."""
        if not self.is_postgres:
            with self._local_lock:
                yield True
            return
        lock_id = int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:8], "big", signed=True)
        # These locks belong to the session, not a transaction. An idle open
        # transaction here blocks LangGraph's CREATE INDEX CONCURRENTLY migrations
        # while the migration waits for this same lock scope to finish.
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            if wait:
                connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_id})
                acquired = True
            else:
                acquired = connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id}
                )
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id})

    def close(self):
        self.engine.dispose()
