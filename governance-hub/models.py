"""
SQLAlchemy models + engine/session setup for the Governance Hub's
compliance-tracking database (SQLite, persisted on a PVC at DATABASE_PATH).

Two tables:
    TrackedRepo -- one row per repo an agent has ever been triggered
                   against, plus its latest known compliance state (does
                   this repo have an architecture doc? an OpenAPI spec?).
    AgentRun    -- one row per agent invocation, created when the
                   Governance Hub itself triggers a "Fix it" remediation
                   and/or updated when doc-gen-agent / openapi-gen-agent
                   report a completed run via POST /api/runs.

Repos are discovered purely by agent activity (see app.py's /api/runs and
/api/repos/{owner}/{repo}/remediate handlers) -- there is no background
GitHub-org scan. This is a small, synchronous SQLAlchemy setup (no async
driver): the Hub's query volume is low (a handful of governance-team
browsers polling a dashboard), so a plain sync Session per request is
simpler than wiring up an async engine, matching the simplicity level of
the rest of this demo.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/governance.db")

engine = create_engine(
    f"sqlite:///{DATABASE_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TrackedRepo(Base):
    """
    One row per repo that has ever had an agent triggered against it.

    `has_architecture_doc` / `has_openapi_spec` reflect the most recent
    compliance check (see compliance.py's run_compliance_check -- a cheap
    GitHub file-tree scan, not model inference) and may lag reality until
    the next check. `None` means "never checked yet", distinct from
    `False` ("checked, and it's missing").
    """

    __tablename__ = "tracked_repos"

    id = Column(Integer, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    repo = Column(String, nullable=False, index=True)
    repo_url = Column(String, nullable=False, unique=True, index=True)
    first_seen = Column(DateTime, default=_utcnow, nullable=False)

    has_architecture_doc = Column(Boolean, nullable=True)
    has_openapi_spec = Column(Boolean, nullable=True)
    last_compliance_check = Column(DateTime, nullable=True)

    last_doc_gen_run = Column(DateTime, nullable=True)
    last_openapi_gen_run = Column(DateTime, nullable=True)

    doc_pr_url = Column(String, nullable=True)
    doc_pr_number = Column(Integer, nullable=True)
    doc_pr_status = Column(String, nullable=True)  # "open" | "merged" | "closed"

    openapi_pr_url = Column(String, nullable=True)
    openapi_pr_number = Column(Integer, nullable=True)
    openapi_pr_status = Column(String, nullable=True)

    runs = relationship(
        "AgentRun", back_populates="repo", order_by="desc(AgentRun.started_at)",
        cascade="all, delete-orphan",
    )

    @property
    def compliant(self) -> bool:
        """A repo is "compliant" once it has an architecture doc on its
        default branch -- an open PR proposing one doesn't count, since
        governance is about what's actually merged and visible, not what's
        pending review. (OpenAPI compliance is informational only, since
        not every repo is an API service -- see `violations`.)"""
        return bool(self.has_architecture_doc)

    @property
    def violations(self) -> list[str]:
        """Missing-artifact violations. `has_openapi_spec is False` only
        after at least one openapi-gen-agent run has actually attempted
        generation for this repo (see app.py) -- a repo that's simply
        never been checked for an OpenAPI spec isn't flagged as violating
        that rule, since most repos aren't HTTP API services."""
        missing = []
        if not self.has_architecture_doc:
            missing.append("architecture_doc")
        if self.has_openapi_spec is False:
            missing.append("openapi_spec")
        return missing


class AgentRun(Base):
    """
    One run of doc-gen-agent or openapi-gen-agent against a tracked repo.

    Created either by app.py's remediate handler (status="accepted",
    triggered_by="governance-hub") right after firing POST /invoke, or by
    the agent itself reporting completion via POST /api/runs
    (triggered_by="invoke" for CI/CD-triggered runs the Hub didn't
    initiate) -- the /api/runs handler updates the matching accepted row
    by job_id if one exists, otherwise inserts a new row.
    """

    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True)
    job_id = Column(String, nullable=False, index=True)
    repo_id = Column(Integer, ForeignKey("tracked_repos.id"), nullable=False)
    agent_type = Column(String, nullable=False)  # "doc-gen" | "openapi-gen"
    status = Column(String, nullable=False)  # "accepted" | "completed" | "failed" | "skipped"
    started_at = Column(DateTime, default=_utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    pr_url = Column(String, nullable=True)
    pr_number = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    triggered_by = Column(String, default="invoke", nullable=False)  # "invoke" | "ui" | "governance-hub"

    repo = relationship("TrackedRepo", back_populates="runs")


def init_db() -> None:
    directory = os.path.dirname(DATABASE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return SessionLocal()


def get_or_create_repo(session: Session, repo_url: str, owner: str, repo: str) -> TrackedRepo:
    """Idempotently register a repo as tracked -- this is the only way a
    repo appears in the Governance Hub (agent-run-driven discovery, no
    background org scan)."""
    tracked = session.query(TrackedRepo).filter_by(repo_url=repo_url).one_or_none()
    if tracked is None:
        tracked = TrackedRepo(owner=owner, repo=repo, repo_url=repo_url)
        session.add(tracked)
        session.commit()
        session.refresh(tracked)
    return tracked
