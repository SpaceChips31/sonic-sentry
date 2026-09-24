from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import select

from app.config import SOURCE_ROOTS
from app.database import SessionLocal
from app.models import AnalysisSource


SOURCE_BASE = Path(os.getenv("LOSSLESS_SOURCE_BASE", "/data")).resolve()
SOURCE_KINDS = {"FILESYSTEM", "SLSKD", "QBITTORRENT", "NZBGET", "OTHER"}
ENV_SOURCE_PATHS = {
    str(Path(item).resolve())
    for item in os.environ.get("LOSSLESS_SOURCE_ROOTS", "").split(os.pathsep)
    if item.strip()
} if "LOSSLESS_SOURCE_ROOTS" in os.environ else set()


class SourceConfigurationError(ValueError):
    pass


def validate_source_path(value: str) -> Path:
    path = Path(value).resolve()
    try:
        path.relative_to(SOURCE_BASE)
    except ValueError as exc:
        raise SourceConfigurationError(
            f"Il percorso deve trovarsi sotto {SOURCE_BASE}"
        ) from exc
    if not path.is_dir():
        raise SourceConfigurationError("La directory non esiste nel container")
    return path


def infer_source(name: str, path: Path) -> tuple[str, str]:
    text = f"{name} {path}".casefold()
    if "soulseek" in text or "slskd" in text:
        return "slskd", "SLSKD"
    if "qbittorrent" in text or "torrent" in text:
        return "qBittorrent", "QBITTORRENT"
    if "nzbget" in text or "usenet" in text:
        return "NZBGet", "NZBGET"
    if "legacy" in text:
        return "Filesystem", "FILESYSTEM"
    return name or path.name or str(path), "FILESYSTEM"


def seed_analysis_sources() -> None:
    candidates = list(SOURCE_ROOTS)
    candidates.extend(
        Path(item) for item in (
            "/data/downloads/soulseek",
            "/data/downloads/qbittorrent",
            "/data/downloads/torrents",
            "/data/downloads/nzbget",
            "/data/downloads/usenet",
            "/data/music/legacy",
        )
    )

    with SessionLocal() as session:
        existing = {
            source.path: source
            for source in session.scalars(select(AnalysisSource)).all()
        }
        seen = set(existing)
        for candidate in candidates:
            path = candidate.resolve()
            path_text = str(path)
            if not path.is_dir():
                continue
            if path_text in existing:
                if path_text in ENV_SOURCE_PATHS:
                    existing[path_text].locked = True
                continue
            name, kind = infer_source(path.name, path)
            session.add(AnalysisSource(
                name=name,
                path=path_text,
                kind=kind,
                enabled=True,
                locked=path_text in ENV_SOURCE_PATHS,
            ))
            seen.add(path_text)
        session.commit()


def list_sources(*, enabled_only: bool = False) -> list[AnalysisSource]:
    with SessionLocal() as session:
        statement = select(AnalysisSource).order_by(AnalysisSource.name, AnalysisSource.id)
        if enabled_only:
            statement = statement.where(AnalysisSource.enabled.is_(True))
        return list(session.scalars(statement).all())


def add_source(name: str, path: str, kind: str) -> None:
    clean_name = name.strip()
    clean_kind = kind.strip().upper()
    if not clean_name:
        raise SourceConfigurationError("Il nome della sorgente è obbligatorio")
    if clean_kind not in SOURCE_KINDS:
        raise SourceConfigurationError("Tipo di sorgente non valido")
    resolved = validate_source_path(path)
    with SessionLocal() as session:
        if session.scalar(select(AnalysisSource).where(AnalysisSource.path == str(resolved))):
            raise SourceConfigurationError("Questo percorso è già configurato")
        session.add(AnalysisSource(name=clean_name, path=str(resolved), kind=clean_kind, enabled=True))
        session.commit()
