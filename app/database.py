import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATA_DIR = Path(os.getenv("LOSSLESS_VALIDATOR_DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv(
    "LOSSLESS_VALIDATOR_DATABASE_URL",
    f"sqlite:///{DATA_DIR / 'lossless-validator.db'}",
)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite")
    else {},
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def migrate_schema() -> None:
    """Apply small backwards-compatible SQLite migrations used before 1.0."""
    inspector = inspect(engine)
    if "analysis_jobs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("analysis_jobs")}
    if "batch_id" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE analysis_jobs ADD COLUMN batch_id INTEGER"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_analysis_jobs_batch_id ON analysis_jobs (batch_id)"))
