"""
SQLAlchemy models + engine/session setup for the Governance Hub's
compliance-tracking database (SQLite, persisted on a PVC at DATABASE_PATH).

Two tables:
    TrackedRepo -- one row per repo an agent has ever been triggered
                   against, plus its latest known compliance state (does
                   this repo have an architecture doc? an OpenAPI spec?
                   did the last architectural-compliance scan find any
                   violations?).
    AgentRun    -- one row per agent invocation, created when the
                   Governance Hub itself triggers a "Fix it" remediation
                   and/or updated when doc-gen-agent / openapi-gen-agent /
                   compliance-scan-agent report a completed run via
                   POST /api/runs. compliance-scan-agent additionally
                   attaches its full Markdown report as report_text,
                   since (unlike the other two agents) it never opens a
                   PR -- the report itself is the deliverable.

Repos are discovered purely by agent activity (see app.py's /api/runs and
/api/repos/{owner}/{repo}/remediate handlers) -- there is no background
GitHub-org scan. This is a small, synchronous SQLAlchemy setup (no async
driver): the Hub's query volume is low (a handful of governance-team
browsers polling a dashboard), so a plain sync Session per request is
simpler than wiring up an async engine, matching the simplicity level of
the rest of this demo.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

logger = logging.getLogger(__name__)

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
    last_compliance_scan_run = Column(DateTime, nullable=True)

    # Set from the most recent compliance-scan-agent run's report (see
    # app.py's report_run -- a simple "does the report contain a FAIL
    # row" check, since the report's own Markdown table is the source of
    # truth for severity/detail, not this column). `None` means "never
    # scanned yet", distinct from `False` ("scanned, no violations found").
    compliance_scan_has_violations = Column(Boolean, nullable=True)

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
        """Missing-artifact and compliance-scan violations.
        `has_openapi_spec is False` only after at least one
        openapi-gen-agent run has actually attempted generation for this
        repo (see app.py) -- a repo that's simply never been checked for
        an OpenAPI spec isn't flagged as violating that rule, since most
        repos aren't HTTP API services. `compliance_scan_has_violations`
        follows the same "only after an actual run" rule: it's None until
        compliance-scan-agent has reported at least once."""
        missing = []
        if not self.has_architecture_doc:
            missing.append("architecture_doc")
        if self.has_openapi_spec is False:
            missing.append("openapi_spec")
        if self.compliance_scan_has_violations:
            missing.append("compliance_scan")
        return missing


class AgentRun(Base):
    """
    One run of doc-gen-agent, openapi-gen-agent, or compliance-scan-agent
    against a tracked repo.

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
    agent_type = Column(String, nullable=False)  # "doc-gen" | "openapi-gen" | "compliance-scan"
    status = Column(String, nullable=False)  # "accepted" | "completed" | "failed" | "skipped"
    started_at = Column(DateTime, default=_utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    pr_url = Column(String, nullable=True)
    pr_number = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    triggered_by = Column(String, default="invoke", nullable=False)  # "invoke" | "ui" | "governance-hub"
    # Full Markdown compliance report, set only for agent_type ==
    # "compliance-scan" runs (see app.py's report_run) -- this agent
    # never opens a PR, so the report itself (rendered on the repo detail
    # page) is the only artifact a completed run produces.
    report_text = Column(Text, nullable=True)

    repo = relationship("TrackedRepo", back_populates="runs")


def _migrate_missing_columns() -> None:
    """
    Base.metadata.create_all() (called right before this in init_db())
    only creates tables that don't exist yet -- it never adds columns to
    a table that's already on disk. governance-hub's SQLite file is
    persisted on a PVC across pod restarts/image upgrades, so any column
    added to a model after the table was first created (e.g.
    TrackedRepo.last_compliance_scan_run/compliance_scan_has_violations
    and AgentRun.report_text, added for compliance-scan-agent) would
    otherwise be silently missing from the real table, crashing every
    query that selects it with "no such column" (see
    sqlalchemy.exc.OperationalError). This repo has no Alembic/migration
    framework -- kept simple for the demo -- so this straightforward
    ALTER TABLE ADD COLUMN step runs instead, once per startup. Safe to
    run every time: it only adds a column if PRAGMA table_info doesn't
    already report it, and never touches existing data.
    """
    with engine.begin() as conn:
        for table in Base.metadata.tables.values():
            existing_columns = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")
            }
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}")
                logger.info("DB migration: added missing column %s.%s (%s)", table.name, column.name, col_type)


def init_db() -> None:
    directory = os.path.dirname(DATABASE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    Base.metadata.create_all(engine)
    _migrate_missing_columns()


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
