from app.database import Base, SessionLocal, engine, migrate_schema
from app.main import control_batch
from app.models import AnalysisBatch, AnalysisJob


def test_batch_can_be_paused_resumed_and_cancelled():
    Base.metadata.create_all(bind=engine)
    migrate_schema()
    with SessionLocal() as session:
        batch = AnalysisBatch(name="Artist", source_path="/data/Artist", status="ACTIVE", created_at="now")
        session.add(batch)
        session.flush()
        session.add(AnalysisJob(source_type="FILESYSTEM_BATCH", source_path="/data/Artist/Album", display_name="Album", status="QUEUED", created_at="now", batch_id=batch.id))
        session.commit()
        batch_id = batch.id

    control_batch(batch_id, "pause")
    with SessionLocal() as session:
        assert session.get(AnalysisBatch, batch_id).status == "PAUSED"

    control_batch(batch_id, "resume")
    with SessionLocal() as session:
        assert session.get(AnalysisBatch, batch_id).status == "ACTIVE"

    control_batch(batch_id, "cancel")
    with SessionLocal() as session:
        assert session.get(AnalysisBatch, batch_id).status == "CANCELLED"
        assert session.query(AnalysisJob).filter_by(batch_id=batch_id).one().status == "CANCELLED"
        session.query(AnalysisJob).filter_by(batch_id=batch_id).delete()
        session.query(AnalysisBatch).filter_by(id=batch_id).delete()
        session.commit()
