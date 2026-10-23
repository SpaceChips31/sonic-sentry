from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Release(Base):
    __tablename__ = "releases"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_path: Mapped[str] = mapped_column(String, unique=True, index=True)
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, index=True)
    analysed_at: Mapped[str | None] = mapped_column(String, nullable=True)

    total_tracks: Mapped[int] = mapped_column(Integer, default=0)
    pass_tracks: Mapped[int] = mapped_column(Integer, default=0)
    quarantine_tracks: Mapped[int] = mapped_column(Integer, default=0)
    rejected_tracks: Mapped[int] = mapped_column(Integer, default=0)

    tracks: Mapped[list[Track]] = relationship(
        back_populates="release",
        cascade="all, delete-orphan",
        order_by="Track.path",
    )
    operations: Mapped[list[ReleaseOperation]] = relationship(
        back_populates="release",
        cascade="all, delete-orphan",
        order_by="ReleaseOperation.id.desc()",
    )


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("releases.id", ondelete="CASCADE"),
        index=True,
    )

    path: Mapped[str] = mapped_column(String)
    display_path: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, index=True)
    integrity_ok: Mapped[int] = mapped_column(Integer)
    forensic_verdict: Mapped[str | None] = mapped_column(String, nullable=True)
    human_review: Mapped[str] = mapped_column(String, default="NONE")
    forensic_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    release: Mapped[Release] = relationship(back_populates="tracks")



class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String)
    source_path: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="QUEUED", index=True)
    created_at: Mapped[str] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    release_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)


class ReleaseOperation(Base):
    __tablename__ = "release_operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("releases.id", ondelete="CASCADE"),
        index=True,
    )
    action: Mapped[str] = mapped_column(String, index=True)
    source_path: Mapped[str] = mapped_column(Text)
    destination_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)

    release: Mapped[Release] = relationship(back_populates="operations")


class AnalysisSource(Base):
    __tablename__ = "analysis_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String, default="FILESYSTEM")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    watch_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_scan_at: Mapped[str | None] = mapped_column(String, nullable=True)
    watch_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkflowSettings(Base):
    __tablename__ = "workflow_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[str] = mapped_column(Text)


class FileTransfer(Base):
    __tablename__ = "file_transfers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    release_id: Mapped[int] = mapped_column(Integer, index=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_path: Mapped[str] = mapped_column(Text)
    destination_path: Mapped[str] = mapped_column(Text)
    temporary_path: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, index=True)
    manual_pass: Mapped[bool] = mapped_column(Boolean, default=False)
    target_kind: Mapped[str] = mapped_column(String)
    manifest_json: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)


class SourceObservation(Base):
    __tablename__ = "source_observations"
    __table_args__ = (UniqueConstraint("source_id", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)
    path: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String)
    stable_since: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String, default="WAITING")
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AnalyzedContent(Base):
    __tablename__ = "analyzed_contents"

    content_hash: Mapped[str] = mapped_column(String, primary_key=True)
    release_id: Mapped[int] = mapped_column(Integer)
