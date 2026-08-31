from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    download_token: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    queries_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(24), index=True, default="queued")
    completed: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer)
    current_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="Waiting for the laptop processing engine.")
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    workbook: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def utcnow():
    return datetime.now(timezone.utc)


def as_utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class JobStore:
    def __init__(self, database_url):
        if database_url.startswith("postgres://"):
            database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
        elif database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
        self.engine = create_engine(database_url, pool_pre_ping=True)
        Base.metadata.create_all(self.engine)

    def create_job(self, *, run_id, client_id, download_token, filename, queries):
        now = utcnow()
        with Session(self.engine) as session:
            job = Job(
                id=run_id,
                client_id=client_id,
                download_token=download_token,
                filename=filename[:255],
                queries_json=json.dumps(queries, ensure_ascii=False),
                total=len(queries),
                state="queued",
                message="Waiting for the laptop processing engine.",
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.commit()
            return self._public(job)

    def list_jobs(self, client_id):
        with Session(self.engine) as session:
            jobs = session.scalars(select(Job).where(Job.client_id == client_id).order_by(Job.created_at.desc())).all()
            return [self._public(job) for job in jobs]

    def get_job(self, run_id, client_id=None):
        with Session(self.engine) as session:
            job = session.get(Job, run_id)
            if not job or (client_id is not None and job.client_id != client_id):
                return None
            return self._public(job)

    def cancel(self, run_id, client_id):
        with Session(self.engine) as session:
            job = session.get(Job, run_id)
            if not job or job.client_id != client_id:
                return None
            if job.state == "queued":
                job.state = "cancelled"
                job.message = "Collection cancelled before processing started."
            elif job.state == "processing":
                job.cancel_requested = True
                job.message = "Cancellation requested. The laptop worker will stop after the current query."
            job.updated_at = utcnow()
            session.commit()
            return self._public(job)

    def heartbeat(self, worker_id):
        now = utcnow()
        with Session(self.engine) as session:
            heartbeat = session.get(WorkerHeartbeat, worker_id)
            if heartbeat:
                heartbeat.last_seen = now
            else:
                session.add(WorkerHeartbeat(worker_id=worker_id, last_seen=now))
            session.commit()

    def worker_status(self):
        cutoff = utcnow() - timedelta(seconds=45)
        with Session(self.engine) as session:
            recent = session.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.last_seen >= cutoff).limit(1))
            return {"online": recent is not None, "message": "Laptop processing engine is online." if recent else "Laptop processing engine is offline. Queued requests will start when it reconnects."}

    def claim(self, worker_id):
        stale = utcnow() - timedelta(minutes=5)
        with Session(self.engine) as session:
            for job in session.scalars(select(Job).where(Job.state == "processing", Job.updated_at < stale)).all():
                job.state = "queued"
                job.worker_id = None
                job.message = "Worker connection was interrupted. Waiting to resume."
            session.flush()
            query = select(Job).where(Job.state == "queued").order_by(Job.created_at).limit(1)
            if self.engine.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            job = session.scalar(query)
            if not job:
                session.commit()
                return None
            job.state = "processing"
            job.worker_id = worker_id
            job.message = "Laptop processing engine claimed this request."
            job.updated_at = utcnow()
            session.commit()
            return {"run_id": job.id, "filename": job.filename, "queries": json.loads(job.queries_json)}

    def progress(self, run_id, worker_id, *, completed, current_query, message):
        self.heartbeat(worker_id)
        with Session(self.engine) as session:
            job = session.get(Job, run_id)
            if not job or job.worker_id != worker_id or job.state != "processing":
                return None
            job.completed = max(0, min(int(completed), job.total))
            job.current_query = str(current_query or "")[:2000] or None
            job.message = str(message or "Processing...")[:1000]
            job.updated_at = utcnow()
            cancelled = job.cancel_requested
            session.commit()
            return {"cancel_requested": cancelled}

    def complete(self, run_id, worker_id, *, workbook, success_count, failed_count):
        with Session(self.engine) as session:
            job = session.get(Job, run_id)
            if not job or job.worker_id != worker_id or job.state != "processing":
                return False
            if job.cancel_requested:
                job.state = "cancelled"
                job.message = "Collection cancelled."
                job.workbook = None
            else:
                job.state = "completed"
                job.completed = job.total
                job.success_count = int(success_count)
                job.failed_count = int(failed_count)
                job.workbook = workbook
                job.message = "AMEX AIR collection complete."
            job.current_query = None
            job.updated_at = utcnow()
            session.commit()
            return True

    def fail(self, run_id, worker_id, error):
        with Session(self.engine) as session:
            job = session.get(Job, run_id)
            if not job or job.worker_id != worker_id:
                return False
            job.state = "failed"
            job.error = str(error)[:2000]
            job.message = "The laptop processing engine could not complete this request."
            job.current_query = None
            job.updated_at = utcnow()
            session.commit()
            return True

    def workbook(self, run_id, token):
        with Session(self.engine) as session:
            job = session.get(Job, run_id)
            if not job or job.download_token != token or job.state != "completed" or not job.workbook:
                return None
            return job.workbook, job.filename

    def _public(self, job):
        result = {
            "run_id": job.id,
            "filename": job.filename,
            "state": job.state,
            "completed": job.completed,
            "total": job.total,
            "current_query": job.current_query,
            "message": job.message,
            "success_count": job.success_count,
            "failed_count": job.failed_count,
            "error": job.error,
            "cancel_requested": job.cancel_requested,
            "created_at": as_utc(job.created_at).isoformat(),
            "updated_at": as_utc(job.updated_at).isoformat(),
        }
        if job.state == "completed":
            result["download_token"] = job.download_token
        return result