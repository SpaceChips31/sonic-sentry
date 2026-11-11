"""SQLite metadata backups and offline, conservative restore. Audio is never included."""
import argparse
import hashlib
import json
import secrets
import sqlite3
import tempfile
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine

from app.database import DATA_DIR, engine
from app.migrations import initialize_database
from app.services.locks import process_lock
from app.version import APP_VERSION


MAX_DATABASE_BYTES = 4 * 1024**3


@contextmanager
def database_connection(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def database_path() -> Path:
    if engine.dialect.name != "sqlite" or not engine.url.database or engine.url.database == ":memory:":
        raise ValueError("Backup and restore currently support file-based SQLite databases")
    return Path(engine.url.database).resolve()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            value.update(part)
    return value.hexdigest()


def export_backup(destination: Path) -> Path:
    source_path = database_path()
    if not source_path.is_file() or destination.exists():
        raise ValueError("Source database is missing or backup destination already exists")
    with tempfile.TemporaryDirectory(prefix="sonic-backup-") as temporary:
        copy = Path(temporary) / "database.sqlite"
        with database_connection(str(source_path)) as source, database_connection(str(copy)) as target:
            source.backup(target)
        manifest = {"format": 1, "app_version": APP_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                    "database_sha256": digest(copy), "includes": "SQLite metadata only; no audio, reports, environment or session keys"}
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(copy, "database.sqlite")
            archive.writestr("manifest.json", json.dumps(manifest))
    return destination


def restore_backup(archive_path: Path) -> Path:
    destination = database_path()
    if destination.parent != DATA_DIR.resolve():
        raise ValueError("Offline restore requires the database to be directly inside the configured data directory")
    with ExitStack() as stack:
        for name in ("web", "worker", "scanner", "file-workflow"):
            stack.enter_context(process_lock(name))
        with tempfile.TemporaryDirectory(prefix="sonic-restore-") as temporary:
            staged = Path(temporary) / "restored.sqlite"
            with zipfile.ZipFile(archive_path) as archive:
                if sorted(archive.namelist()) != ["database.sqlite", "manifest.json"]:
                    raise ValueError("Unexpected backup archive contents")
                if archive.getinfo("manifest.json").file_size > 65536 or archive.getinfo("database.sqlite").file_size > MAX_DATABASE_BYTES:
                    raise ValueError("Backup exceeds supported size")
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("format") != 1:
                    raise ValueError("Unsupported backup format")
                def series(version):
                    try:
                        return tuple(int(value) for value in version.split(".")[:2])
                    except (ValueError, AttributeError):
                        raise ValueError("Invalid backup version")
                if series(manifest.get("app_version", "")) > series(APP_VERSION):
                    raise ValueError("Upgrade SonicSentry before restoring a newer backup")
                with archive.open("database.sqlite") as source, staged.open("xb") as output:
                    total = 0
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        total += len(block)
                        if total > MAX_DATABASE_BYTES:
                            raise ValueError("Backup exceeds supported size")
                        output.write(block)
            if digest(staged) != manifest.get("database_sha256"):
                raise ValueError("Backup checksum mismatch")
            with database_connection(str(staged)) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup database failed its integrity check")
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"releases", "tracks", "analysis_jobs", "workflow_settings"}.issubset(tables):
                    raise ValueError("Archive is not a SonicSentry database")
            staged_engine = create_engine(f"sqlite:///{staged}")
            try:
                initialize_database(staged_engine)
            finally:
                staged_engine.dispose()
            with database_connection(str(staged)) as connection:
                connection.execute("UPDATE analysis_jobs SET status='CANCELLED',cancel_requested=1,error='Restored from backup; verify files before retrying' WHERE status IN ('QUEUED','ANALYZING','ROUTING')")
                connection.execute("UPDATE analysis_sources SET watch_enabled=0")
                connection.execute("UPDATE file_tasks SET state='CANCELLED',error='Restored metadata; preview again' WHERE state IN ('QUEUED','RUNNING')")
                connection.execute("UPDATE reconcile_scans SET state='FAILED',error='Restored metadata; scan again' WHERE state IN ('QUEUED','RUNNING')")
                connection.execute("UPDATE file_transfers SET state='NEEDS_ATTENTION',error='Restored metadata; reconcile physical files before resuming' WHERE state NOT IN ('COMPLETE','ABANDONED')")
                connection.execute("UPDATE trash_entries SET state='PURGE_FAILED',error='Restored metadata; permanent deletion requires a new confirmation' WHERE state='PURGING'")
            retained = DATA_DIR / f"pre-restore-{secrets.token_hex(8)}.sqlite"
            engine.dispose()
            if destination.exists():
                with database_connection(str(destination)) as source, database_connection(str(retained)) as target:
                    source.backup(target)
                retained.chmod(0o600)
            with database_connection(str(staged)) as source, database_connection(str(destination)) as target:
                source.backup(target)
            destination.chmod(0o600)
            key = DATA_DIR / "session.key"
            temporary_key = DATA_DIR / f".session-{secrets.token_hex(8)}.tmp"
            temporary_key.write_bytes(secrets.token_bytes(32))
            temporary_key.chmod(0o600)
            temporary_key.replace(key)
            return retained


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["backup", "restore"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--confirm")
    args = parser.parse_args()
    if args.command == "backup":
        export_backup(args.path)
        print(f"Metadata backup saved to {args.path}")
    else:
        if args.confirm != "RESTORE":
            parser.error("Stop web and worker, then pass --confirm RESTORE")
        retained = restore_backup(args.path)
        print(f"Metadata restored. Previous database retained at {retained}. Watching is disabled; reconcile files before resuming jobs or transfers.")


if __name__ == "__main__":
    main()
