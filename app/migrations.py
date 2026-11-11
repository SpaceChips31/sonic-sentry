"""Additive upgrades for installations created before 0.2; safe at concurrent startup."""
from sqlalchemy import inspect

from app.database import Base


ADDITIONS = {
    "tracks": {"content_hash": "VARCHAR", "review_reason": "VARCHAR"},
    "file_transfers": {"progress_bytes": "INTEGER NOT NULL DEFAULT 0", "total_bytes": "INTEGER NOT NULL DEFAULT 0","trash_entry_id": "VARCHAR", "actor": "VARCHAR NOT NULL DEFAULT 'local'"},
    "release_operations": {"actor": "VARCHAR NOT NULL DEFAULT 'local'"},
    "analysis_jobs": {
        "source_id": "INTEGER", "routing_policy": "VARCHAR NOT NULL DEFAULT 'inherit'",
        "hidden": "BOOLEAN NOT NULL DEFAULT 0", "release_id": "INTEGER", "cancel_requested": "BOOLEAN NOT NULL DEFAULT 0",
        "progress": "INTEGER NOT NULL DEFAULT 0", "progress_total": "INTEGER NOT NULL DEFAULT 0",
        "started_at": "VARCHAR", "finished_at": "VARCHAR",
        "attempts": "INTEGER NOT NULL DEFAULT 0", "next_run_at": "FLOAT",
    },
    "analysis_sources": {
        "scan_interval": "INTEGER", "settle_seconds": "INTEGER", "routing_policy": "VARCHAR NOT NULL DEFAULT 'inherit'", "next_scan_at": "FLOAT",
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

        from app.services.review_reasons import classify
        from sqlalchemy import text
        for row in connection.execute(text("SELECT id,status,integrity_ok,forensic_verdict,forensic_json FROM tracks WHERE review_reason IS NULL")).all():
            reason = classify(row.status, row.integrity_ok, row.forensic_verdict, row.forensic_json)
            connection.execute(text("UPDATE tracks SET review_reason=:reason WHERE id=:id"), {"reason":reason,"id":row.id})
