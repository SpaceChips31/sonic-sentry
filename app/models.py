from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
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


class ApplicationSetting(Base):
    __tablename__ = "application_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
