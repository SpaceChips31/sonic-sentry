from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
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
