"""Reconcile physical destination folders with release records without changing files."""
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Release, TrashEntry
from app.services.files import contains_flac
from app.services.settings import workflow_settings


KINDS = {"quarantine": "Quarantine", "rejected": "Rejected", "staging": "Picard staging", "trash": "Trash"}


def destination_inventory(kind: str, query: str = "", *, show_all: bool = False) -> dict:
    if kind not in KINDS:
        raise ValueError("Unknown destination")
    value = workflow_settings()[kind]
    root = Path(value).resolve() if value else None
    rows, warning = {}, None
    with SessionLocal() as session:
        releases = list(session.scalars(select(Release)))
        entries = {entry.release_id: entry for entry in session.scalars(select(TrashEntry).where(
            TrashEntry.state.in_({"PENDING", "TRASHED", "PURGING", "PURGE_FAILED"})).order_by(TrashEntry.created_at))}
        purged = set(session.scalars(select(TrashEntry.trash_path).where(TrashEntry.state == "PURGED")))
    if root and root.is_dir():
        try:
            for path in root.iterdir():
                if path.name.startswith(".") or path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    continue
                if path.is_dir():
                    rows[str(path.resolve())] = {"path": str(path), "name": path.name, "release": None,
                                              "exists": True, "entry": None}
        except OSError as exc:
            warning = str(exc)
    else:
        warning = "Destination folder is missing or not configured"
    for release in releases:
        path = Path(release.source_path).resolve()
        if kind == "trash" and str(path) in purged:
            continue
        entry = entries.get(release.id)
        belongs = root and path != root and path.is_relative_to(root)
        if belongs or kind == "trash" and entry:
            row = rows.setdefault(str(path), {"path": str(path), "name": release.title, "exists": path.is_dir()})
            row.update(release=release, entry=entry)
    if not show_all:
        rows = {key: row for key, row in rows.items() if contains_flac(Path(row["path"]))}
    values = sorted(rows.values(), key=lambda row: row["name"].casefold())
    if query.strip():
        values = [row for row in values if query.casefold().strip() in f'{row["name"]} {row["path"]}'.casefold()]
    return {"root": str(root) if root else "", "rows": values, "warning": warning, "kind": kind,
            "title": KINDS[kind], "query": query, "show_all": show_all}
