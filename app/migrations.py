"""Additive upgrades for installations created before 0.2; safe at concurrent startup."""
from sqlalchemy import inspect

from app.database import Base


ADDITIONS = {
    "analysis_jobs": {
        "hidden": "BOOLEAN NOT NULL DEFAULT 0", "release_id": "INTEGER", "cancel_requested": "BOOLEAN NOT NULL DEFAULT 0",
        "progress": "INTEGER NOT NULL DEFAULT 0", "progress_total": "INTEGER NOT NULL DEFAULT 0",
        "started_at": "VARCHAR", "finished_at": "VARCHAR",
    },
    "analysis_sources": {
        "watch_enabled": "BOOLEAN NOT NULL DEFAULT 0", "last_scan_at": "VARCHAR", "watch_error": "TEXT",
    },
}


def initialize_database(engine):
    # Models must be registered before metadata is inspected.
    from app import models  # noqa: F401
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        Base.metadata.create_all(connection)
        for table, additions in ADDITIONS.items():
            present = {column["name"] for column in inspect(connection).get_columns(table)}
            for column, ddl in additions.items():
                if column not in present:
                    connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')
