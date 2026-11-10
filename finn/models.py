"""SQLAlchemy 2 data model (spec §4.1). Postgres (Neon) in dev and prod; SQLite only for unit tests.

Postgres-only features degrade on SQLite: JSONB -> JSON, vector(768) -> JSON text, and the tsvector
columns are added by the Alembic migration as generated columns (not mapped here, queried with SQL).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

EMBED_DIMS = 768

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Embedding(TypeDecorator):
    """pgvector on Postgres; a JSON list on SQLite (vector search is skipped there)."""
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector
            return dialect.type_descriptor(Vector(EMBED_DIMS))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        return None if value is None else list(value)

    def process_result_value(self, value, dialect):
        return None if value is None else list(value)


def utcnow_col(**kw):
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kw)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSONType, list: JSONType}


# ---------- companies and market data ----------

class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    nse_symbol: Mapped[str] = mapped_column(String(32), unique=True)
    bse_code: Mapped[str | None] = mapped_column(String(12), unique=True)
    yahoo_symbol: Mapped[str | None] = mapped_column(String(40))
    isin: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    sector: Mapped[str | None] = mapped_column(String(120))
    industry: Mapped[str | None] = mapped_column(String(160))
    is_portfolio: Mapped[bool] = mapped_column(Boolean, default=False)
    peer_of: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)  # e.g. swap reason
    # BSE's own structured results snapshot (latest two quarters + last FY, revenue and net profit, ₹ crore):
    # an independent cross-check on PDF extraction and the trailing revenue used to size orders.
    bse_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Price(Base):
    __tablename__ = "prices"
    # Unique on (symbol, date), not (company_id, date): index rows (^NSEI) have no company and NULLs never collide.
    __table_args__ = (UniqueConstraint("symbol", "date", name="uq_prices_symbol_date"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))  # NULL = market index row
    symbol: Mapped[str] = mapped_column(String(40), index=True)               # yahoo symbol, incl. ^NSEI
    date: Mapped[date] = mapped_column(Date)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)


class CalendarEvent(Base):
    """Week-ahead catalysts: results dates, board meetings, AGMs, record/ex dates (from BSE)."""
    __tablename__ = "calendar_events"
    __table_args__ = (UniqueConstraint("company_id", "kind", "event_date", "title"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    kind: Mapped[str] = mapped_column(String(32))  # results | board_meeting | agm | record_date | ex_date
    event_date: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = utcnow_col()


# ---------- filings (push) ----------

class Filing(Base):
    __tablename__ = "filings"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    exchange: Mapped[str] = mapped_column(String(8), default="BSE")
    exchange_id: Mapped[str] = mapped_column(String(80))
    filed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)  # UTC
    category_raw: Mapped[str | None] = mapped_column(String(120))
    subcategory_raw: Mapped[str | None] = mapped_column(String(160))
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)  # exchange's own summary text (BSE "MORE")
    pdf_url: Mapped[str | None] = mapped_column(Text)
    xbrl_url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("filings.id"))  # revised filing
    status: Mapped[str] = mapped_column(String(16), default="new")  # new | processed | failed
    error: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = utcnow_col()
    company: Mapped[Company] = relationship()
    enrichment: Mapped["Enrichment | None"] = relationship(back_populates="filing", uselist=False)


class Enrichment(Base):
    __tablename__ = "enrichments"
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    type_source: Mapped[str] = mapped_column(String(8), default="rule")  # rule | llm
    score: Mapped[float] = mapped_column(Float, default=0)
    score_breakdown: Mapped[dict] = mapped_column(JSONType, default=dict)
    bucket: Mapped[str] = mapped_column(String(16), index=True)  # attention | worth_a_look | routine
    headline: Mapped[str | None] = mapped_column(Text)
    one_liner: Mapped[str | None] = mapped_column(Text)
    thesis_impact: Mapped[str | None] = mapped_column(String(16))  # supports | watch | threatens | none
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONType)  # results table, people, governance flags
    confidence: Mapped[str | None] = mapped_column(String(8))
    model: Mapped[str | None] = mapped_column(String(60))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = utcnow_col(onupdate=func.now())
    filing: Mapped[Filing] = relationship(back_populates="enrichment")


# ---------- thesis guardian ----------

class Thesis(Base):
    __tablename__ = "theses"
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), primary_key=True)
    statement: Mapped[str] = mapped_column(Text)
    kpis: Mapped[list] = mapped_column(JSONType, default=list)  # [{metric, op, threshold, unit}]
    breakers: Mapped[list] = mapped_column(JSONType, default=list)
    source: Mapped[str] = mapped_column(String(8), default="draft")  # user | draft
    citations: Mapped[list] = mapped_column(JSONType, default=list)
    updated_at: Mapped[datetime] = utcnow_col(onupdate=func.now())


class ThesisStatus(Base):
    __tablename__ = "thesis_status"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    status: Mapped[str] = mapped_column(String(10))  # intact | watch | at_risk
    reason: Mapped[str] = mapped_column(Text)
    filing_id: Mapped[int | None] = mapped_column(ForeignKey("filings.id"))
    at: Mapped[datetime] = utcnow_col()


# ---------- alerts and push ----------

class AlertRule(Base):
    __tablename__ = "alert_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), unique=True)
    label: Mapped[str] = mapped_column(String(120))
    params: Mapped[dict] = mapped_column(JSONType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("rule_id", "company_id", "triggered_on"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id"))
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    filing_id: Mapped[int | None] = mapped_column(ForeignKey("filings.id"))
    triggered_on: Mapped[date] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(200))
    line: Mapped[str] = mapped_column(Text)
    deep_link: Mapped[str] = mapped_column(String(200))
    detail: Mapped[dict | None] = mapped_column(JSONType)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = utcnow_col()


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    keys: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = utcnow_col()
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------- documents (pull) ----------

class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # transcript | annual_report | results
    period: Mapped[str | None] = mapped_column(String(16))  # "Q2 FY26" / "FY25"
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    pages: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="listed")  # listed | parsed | unparseable | failed
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("company_id", "url"),)


class Page(Base):
    __tablename__ = "pages"
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    page_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_from: Mapped[int] = mapped_column(Integer)
    page_to: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str | None] = mapped_column(String(160))
    role: Mapped[str | None] = mapped_column(String(16))  # management | analyst | moderator
    section: Mapped[str | None] = mapped_column(String(160))
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(Embedding, nullable=True)


class Extraction(Base):
    __tablename__ = "extractions"
    __table_args__ = (UniqueConstraint("document_id", "schema", "prompt_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    schema: Mapped[str] = mapped_column(String(40))
    prompt_version: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSONType)
    verified_ratio: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = utcnow_col()


class Promise(Base):
    __tablename__ = "promises"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    said_in: Mapped[str] = mapped_column(String(16))  # period of the call
    speaker: Mapped[str | None] = mapped_column(String(160))
    metric: Mapped[str] = mapped_column(String(200))
    target_low: Mapped[float | None] = mapped_column(Float)
    target_high: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))
    horizon: Mapped[str | None] = mapped_column(String(40))
    quote: Mapped[str] = mapped_column(Text)
    page: Mapped[int] = mapped_column(Integer)


class Actual(Base):
    __tablename__ = "actuals"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(String(200))
    period: Mapped[str] = mapped_column(String(16))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))
    quote: Mapped[str] = mapped_column(Text)
    page: Mapped[int] = mapped_column(Integer)


class LedgerRow(Base):
    __tablename__ = "ledger_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    promise_id: Mapped[int] = mapped_column(ForeignKey("promises.id", ondelete="CASCADE"))
    actual_id: Mapped[int | None] = mapped_column(ForeignKey("actuals.id", ondelete="SET NULL"))
    verdict: Mapped[str] = mapped_column(String(16))  # delivered | partial | missed | pending | unverifiable
    detail: Mapped[dict | None] = mapped_column(JSONType)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # core | deep:<recipe>
    data: Mapped[dict] = mapped_column(JSONType)
    receipt: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = utcnow_col()


class ResearchJob(Base):
    __tablename__ = "research_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(40), default="core")
    stage: Mapped[str] = mapped_column(String(60))
    progress: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(12), default="running")  # running | done | failed
    error: Mapped[str | None] = mapped_column(Text)
    report_id: Mapped[int | None] = mapped_column(ForeignKey("reports.id"))
    created_at: Mapped[datetime] = utcnow_col()
    updated_at: Mapped[datetime] = utcnow_col(onupdate=func.now())


# ---------- experience ----------

class JournalEntry(Base):
    __tablename__ = "journal"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    filing_id: Mapped[int | None] = mapped_column(ForeignKey("filings.id"))
    action: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = utcnow_col()


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"))
    signal: Mapped[str] = mapped_column(String(16))  # useful | not_useful | higher
    at: Mapped[datetime] = utcnow_col()


class UserState(Base):
    __tablename__ = "user_state"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prefs: Mapped[dict] = mapped_column(JSONType, default=dict)


# ---------- LLM bookkeeping and runs ----------

class LLMCache(Base):
    __tablename__ = "llm_cache"
    key_sha: Mapped[str] = mapped_column(String(64), primary_key=True)
    task: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(60))
    response: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = utcnow_col()


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = utcnow_col()
    task: Mapped[str] = mapped_column(String(40), index=True)
    prompt_id: Mapped[str | None] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(60))
    tier: Mapped[str] = mapped_column(String(8))
    in_tokens: Mapped[int] = mapped_column(Integer, default=0)
    out_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    budget_key: Mapped[str | None] = mapped_column(String(60), index=True)  # e.g. report job id


class LLMQuota(Base):
    """Models that hit a daily free-tier quota today, so the router skips them."""
    __tablename__ = "llm_quota"
    model: Mapped[str] = mapped_column(String(60), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)


class IngestRun(Base):
    __tablename__ = "ingest_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    added: Mapped[int] = mapped_column(Integer, default=0)
    seen: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = utcnow_col()


Index("ix_filings_company_filed", Filing.company_id, Filing.filed_at)
Index("ix_prices_symbol_date", Price.symbol, Price.date)
