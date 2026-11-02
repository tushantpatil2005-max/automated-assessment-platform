"""
Automated Assessment & Evaluation Platform (single-file version)
================================================================
Multi-tenant API for coding assessments. Tenants create assessments, candidates submit Python,
and submissions run in a locked-down Docker sandbox against visible and hidden test cases.
Scores combine automatic test results with an optional manual rubric grade.

Stack: FastAPI, SQLAlchemy 2.0 (PostgreSQL), Redis (cache), Docker (sandbox), Pydantic v2.

Install:   pip install "fastapi>=0.110" "uvicorn[standard]" "sqlalchemy>=2.0.25" psycopg2-binary \
                       "redis>=5" "docker>=7" "pydantic[email]>=2.5" "pydantic-settings>=2.1"
Run:       uvicorn main:app            (or: python main.py)
Test:      pip install httpx && python -m unittest test_main -v

Needs PostgreSQL, Redis and a reachable Docker daemon. Configure through environment variables or a
.env file (see Settings). Set ADMIN_API_KEY to enable tenant provisioning (POST /api/v1/tenants);
tenants then authenticate with the returned key in the X-API-Key header.

Layout of this file (top to bottom): settings, security, scoring, sandbox, evaluator, cache, database,
models, schemas, services, routes, application factory.

Production notes: mounting the Docker socket gives this process host-level control; use gVisor or a
separate execution service for hostile code. Evaluation runs in-process via BackgroundTasks; move it to a
queue worker and add recovery for submissions left in "running" after a crash. Not included: rate
limiting, key rotation, audit logging, migrations (use Alembic; AUTO_CREATE_TABLES is for development).
"""
from __future__ import annotations

import copy
import enum
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Iterable, Iterator, List, Literal, Optional, Protocol, Tuple
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import (JSON, Boolean, DateTime, Enum as SAEnum, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, Uuid, create_engine, select, text, update)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, declared_attr, mapped_column, relationship,
                            selectinload, sessionmaker)
from sqlalchemy.pool import StaticPool

__version__ = "1.0.0"
logger = logging.getLogger("assessment_platform")


# ============================================================================
# Settings
# ============================================================================

class Settings(BaseSettings):
model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    PROJECT_NAME: str = "Automated Assessment & Evaluation Platform"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development"
    # Convenience for local runs and tests. Use migrations (e.g. Alembic) in production.
    AUTO_CREATE_TABLES: bool = False

    # Admin key for tenant provisioning. Empty (the default) disables the endpoint.
    ADMIN_API_KEY: SecretStr = SecretStr("")

    # Database. Set DATABASE_URL to override the individual POSTGRES_* values.
    DATABASE_URL: Optional[str] = None
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: SecretStr = SecretStr("postgres")
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "assessment_platform"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # Cache: "redis", "memory" or "none". The cache only holds assessments; failures never break requests.
    CACHE_BACKEND: str = "redis"
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    CACHE_TTL_SECONDS: int = 300

    # Sandbox
    SANDBOX_IMAGE: str = "python:3.11-slim"
    SANDBOX_MEM_LIMIT: str = "128m"
    SANDBOX_CPUS: float = 0.5
    SANDBOX_PIDS_LIMIT: int = 64
    SANDBOX_TIMEOUT_SECONDS: int = 10
    SANDBOX_NETWORK_DISABLED: bool = True
    SANDBOX_MAX_OUTPUT_BYTES: int = 65_536
    # Directory for per-run files. Must be the same path on the host and in this process
    # when the API itself runs in a container and starts sibling containers.
    SANDBOX_WORKDIR: Optional[str] = None

    # Scoring: weight of test-case correctness once a manual rubric grade exists.
    CORRECTNESS_WEIGHT: float = 0.7

    @property
    def sqlalchemy_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD.get_secret_value()}"
            f"@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


@lru_cache()
def get_settings() -> Settings:
    return Settings()


# ============================================================================
# API-key helpers
# ============================================================================

def hash_api_key(raw_key: str) -> str:
    """SHA-256 is appropriate here: keys are 256-bit random values, so no slow hash is needed."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> Tuple[str, str]:
 """Return (raw_key, hash). The raw key is shown once; only the hash is stored."""
    raw = f"apk_{secrets.token_urlsafe(32)}"
    return raw, hash_api_key(raw)


def verify_admin_key(provided: str, expected: str) -> bool:
    """Constant-time comparison. An empty expected key always fails (feature disabled)."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


# ============================================================================
# Scoring and redaction rules
# ============================================================================

def normalize_output(text: str) -> str:
    """Compare outputs ignoring line-ending style and trailing whitespace."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def outputs_match(actual: str, expected: str) -> bool:
    return normalize_output(actual) == normalize_output(expected)


def correctness_percent(passed: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(passed / total * 100, 2)


def rubric_percent(scores: Mapping[str, float], weights: Mapping[str, int]) -> float:
    """Weighted average of per-criterion scores (each 0 to 100). Missing criteria count as 0."""
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return 0.0
    return round(sum(scores.get(cid, 0.0) * w for cid, w in weights.items()) / total_weight, 2)


def total_score(correctness: float, rubric: Optional[float], correctness_weight: float = 0.7) -> float:
    """Percentage 0 to 100. Without a rubric grade the total is the correctness score alone,
    so a perfect solution is not capped below 100."""
    if rubric is None:
        return round(correctness, 2)
    return round(correctness * correctness_weight + rubric * (1 - correctness_weight), 2)


def points(total_percent: float, max_score: int) -> float:
    return round(total_percent / 100 * max_score, 2)


def redact_assessment(data: Dict[str, Any], include_hidden: bool) -> Dict[str, Any]:
    """Return a copy of an assessment dict with hidden test cases removed unless include_hidden."""
    out = copy.deepcopy(data)
    if not include_hidden:
        for question in out.get("questions", []):
            question["test_cases"] = [tc for tc in question.get("test_cases", []) if not tc.get("is_hidden")]
    return out


def redact_breakdown(breakdown: Dict[str, Any], include_hidden: bool) -> Dict[str, Any]:
    """Hide details of hidden test cases (only the pass/fail flag remains)."""
    out = copy.deepcopy(breakdown)
    if include_hidden:
        return out
    results: List[Dict[str, Any]] = out.get("results", [])
    out["results"] = [
        {"test_case_index": r["test_case_index"], "hidden": True, "passed": r["passed"]} if r.get("hidden") else r
        for r in results
    ]
    return out


# ============================================================================
# Sandboxed execution of untrusted code
# ============================================================================

class SandboxError(Exception):
  """The sandbox could not run the code (infrastructure problem)."""


class SandboxTimeout(SandboxError):
    """The code exceeded the time limit."""


@dataclass(frozen=True)
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float
    truncated: bool = False
    oom_killed: bool = False


class Sandbox(Protocol):
    def run_python(self, code: str, stdin: str = "", timeout: Optional[int] = None) -> SandboxResult: ...


def read_capped(chunks: Iterable[bytes], cap: int) -> tuple[str, bool]:
    """Join byte chunks up to `cap` bytes. Returns (text, truncated)."""
    buf, size = bytearray(), 0
    for chunk in chunks:
        remaining = cap - size
        if len(chunk) > remaining:
            buf += chunk[:remaining]
            return buf.decode("utf-8", errors="replace"), True
        buf += chunk
        size += len(chunk)
    return buf.decode("utf-8", errors="replace"), False


class DockerSandbox:
    def __init__(self, settings) -> None:
        self._s = settings
        self._client = None

    def _docker(self):
        if self._client is None:
            import docker  # imported lazily so the API starts without Docker installed
            self._client = docker.from_env()
        return self._client

    def run_python(self, code: str, stdin: str = "", timeout: Optional[int] = None) -> SandboxResult:
        import docker
        from docker.errors import APIError, ContainerError
        from docker.types import LogConfig

        s = self._s
        timeout = timeout or s.SANDBOX_TIMEOUT_SECONDS
        start = time.monotonic()
        container = None
        with tempfile.TemporaryDirectory(prefix="sandbox-", dir=s.SANDBOX_WORKDIR) as tmp:
            os.chmod(tmp, 0o755)                      # the container runs as an unprivileged user
            for name, content in (("solution.py", code), ("stdin.txt", stdin or "")):
                path = os.path.join(tmp, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                os.chmod(path, 0o644)
            try:
                container = self._docker().containers.create(
                    image=s.SANDBOX_IMAGE,
                    command=["sh", "-c", "exec python -I -B /sandbox/solution.py < /sandbox/stdin.txt"],
                    volumes={tmp: {"bind": "/sandbox", "mode": "ro"}},
                    working_dir="/sandbox",
                    network_disabled=s.SANDBOX_NETWORK_DISABLED,
                    mem_limit=s.SANDBOX_MEM_LIMIT,
                    memswap_limit=s.SANDBOX_MEM_LIMIT,
                    nano_cpus=int(s.SANDBOX_CPUS * 1e9),
                    pids_limit=s.SANDBOX_PIDS_LIMIT,
                    read_only=True,
                    tmpfs={"/tmp": "size=16m,noexec"},
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                    user="65534:65534",
                    log_config=LogConfig(type=LogConfig.types.JSON, config={"max-size": "1m", "max-file": "1"}),
                    environment={"PYTHONDONTWRITEBYTECODE": "1"},
                )
                container.start()
                try:
                    status = container.wait(timeout=timeout)
                except Exception as exc:               # requests raises on the wait deadline
                    try:
                        container.kill()
                    except APIError:
                        pass
                    raise SandboxTimeout(f"Execution exceeded {timeout}s") from exc

                container.reload()
                oom = bool(container.attrs.get("State", {}).get("OOMKilled", False))
                cap = s.SANDBOX_MAX_OUTPUT_BYTES
                out, out_cut = read_capped(container.logs(stdout=True, stderr=False, stream=True), cap)
                err, err_cut = read_capped(container.logs(stdout=False, stderr=True, stream=True), cap)
                return SandboxResult(out, err, int(status.get("StatusCode", 1)),
                                     (time.monotonic() - start) * 1000, out_cut or err_cut, oom)
            except SandboxError:
                raise
            except (ContainerError, APIError, docker.errors.DockerException) as exc:
                raise SandboxError(str(exc)) from exc
            finally:
                if container is not None:
                    try:
                        container.remove(force=True)
                    except Exception:                   # best-effort cleanup
                        logger.warning("Failed to remove sandbox container", exc_info=True)


# ============================================================================
# Submission evaluation
# ============================================================================

STDERR_TAIL = 2000


def evaluate_submission(sandbox: Sandbox, code: str, test_cases: List[Dict[str, Any]],
                        timeout: Optional[int] = None) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    passed = 0
    for index, case in enumerate(test_cases):
entry: Dict[str, Any] = {"test_case_index": index, "hidden": bool(case.get("is_hidden", False))}
        try:
            run = sandbox.run_python(code, stdin=str(case.get("input", "")), timeout=timeout)
        except SandboxTimeout:
            entry.update(passed=False, error="timeout")
        except SandboxError:
            logger.exception("Sandbox failure on test case %s", index)   # detail stays in logs, not in the API
            entry.update(passed=False, error="sandbox_error")
        else:
            ok = (run.exit_code == 0 and not run.oom_killed and not run.truncated
                  and outputs_match(run.stdout, str(case.get("expected_output", ""))))
            entry.update(passed=ok, exit_code=run.exit_code, duration_ms=round(run.duration_ms, 1),
                         oom_killed=run.oom_killed, output_truncated=run.truncated,
                         stderr="" if ok else run.stderr[-STDERR_TAIL:])
            passed += int(ok)
        results.append(entry)
    return {"passed": passed, "total": len(test_cases),
            "correctness_score": correctness_percent(passed, len(test_cases)), "results": results}


# ============================================================================
# Cache
# ============================================================================

class Cache(Protocol):
    def get(self, key: str) -> Optional[Any]: ...
    def set(self, key: str, value: Any, ttl: int) -> None: ...
    def delete(self, key: str) -> None: ...


class NullCache:
    def get(self, key): return None
    def set(self, key, value, ttl): return None
    def delete(self, key): return None


class InMemoryCache:
    """Process-local cache for development and tests."""
    def __init__(self) -> None:
        self._data: Dict[str, Tuple[float, str]] = {}

    def get(self, key):
        item = self._data.get(key)
        if not item or item[0] < time.monotonic():
            self._data.pop(key, None)
            return None
        return json.loads(item[1])

    def set(self, key, value, ttl):
        self._data[key] = (time.monotonic() + ttl, json.dumps(value, default=str))

    def delete(self, key):
        self._data.pop(key, None)


class RedisCache:
    def __init__(self, host: str, port: int) -> None:
        import redis  # lazy import
        self._redis = redis
        self._client = redis.Redis(connection_pool=redis.ConnectionPool(host=host, port=port, db=0, decode_responses=True))

    def get(self, key):
        try:
            raw = self._client.get(key)
            return json.loads(raw) if raw is not None else None
        except self._redis.RedisError:
            logger.warning("Cache read failed", exc_info=True)
            return None

    def set(self, key, value, ttl):
        try:
            self._client.setex(key, ttl, json.dumps(value, default=str))
        except self._redis.RedisError:
            logger.warning("Cache write failed", exc_info=True)

    def delete(self, key):
        try:
            self._client.delete(key)
        except self._redis.RedisError:
            logger.warning("Cache delete failed", exc_info=True)


def build_cache(settings) -> Cache:
    if settings.CACHE_BACKEND == "redis":
        return RedisCache(settings.REDIS_HOST, settings.REDIS_PORT)
    if settings.CACHE_BACKEND == "memory":
        return InMemoryCache()
    return NullCache()


# ============================================================================
# Database engine and sessions
# ============================================================================

def build_engine(settings) -> Engine:
    url = settings.sqlalchemy_url
    if url.startswith("sqlite"):   # used by the test-suite
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    return create_engine(url, pool_pre_ping=True, pool_size=settings.DB_POOL_SIZE,
                         max_overflow=settings.DB_MAX_OVERFLOW)


def build_session_factory(engine: Engine) -> "sessionmaker[Session]":
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ============================================================================
# ORM models
# ============================================================================

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampedBase(Base):
__abstract__ = True
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class TenantOwned(TimestampedBase):
    __abstract__ = True

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)


class Tenant(TimestampedBase):
    __tablename__ = "tenants"
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)


class Candidate(TenantOwned):
    __tablename__ = "candidates"
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_candidates_tenant_email"),)


class Assessment(TenantOwned):
    __tablename__ = "assessments"
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(50), default="python", nullable=False)
    time_limit_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    questions: Mapped[List["Question"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan", order_by="Question.position")
    __table_args__ = (Index("ix_assessments_tenant_title", "tenant_id", "title"),)


class Question(TenantOwned):
    __tablename__ = "questions"
    assessment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"), index=True, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    starter_code: Mapped[str] = mapped_column(Text, default="", nullable=False)
    test_cases: Mapped[List[Dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)
    max_score: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    assessment: Mapped[Assessment] = relationship(back_populates="questions")
    rubric_criteria: Mapped[List["RubricCriterion"]] = relationship(
        back_populates="question", cascade="all, delete-orphan")


class RubricCriterion(TenantOwned):
    __tablename__ = "rubric_criteria"
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    weight: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    question: Mapped[Question] = relationship(back_populates="rubric_criteria")


class SubmissionStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    EVALUATED = "evaluated"
    FAILED = "failed"


class Submission(TenantOwned):
    __tablename__ = "submissions"
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True, nullable=False)
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[SubmissionStatus] = mapped_column(
        SAEnum(SubmissionStatus, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e]),
        default=SubmissionStatus.PENDING, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    evaluation: Mapped[Optional["Evaluation"]] = relationship(
        back_populates="submission", uselist=False, cascade="all, delete-orphan")
    __table_args__ = (Index("ix_submissions_tenant_status", "tenant_id", "status"),)


class Evaluation(TenantOwned):
    __tablename__ = "evaluations"
    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    correctness_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    rubric_score: Mapped[Optional[float]] = mapped_column(Float)            # None until manually graded
    rubric_scores: Mapped[Optional[Dict[str, float]]] = mapped_column(JSONType)
    total_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)   # percent, 0 to 100
    points_awarded: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_score: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    breakdown: Mapped[Dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    submission: Mapped[Submission] = relationship(back_populates="evaluation")


# ============================================================================
# API schemas
# ============================================================================

class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Timestamped(ORMModel):
    id: UUID
    created_at: datetime
    updated_at: datetime


# ---- tenants ---------------------------------------------------------------
class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{1,98}$")


class TenantProvisioned(ORMModel):
    id: UUID
    name: str
    slug: str
    api_key: str = Field(..., description="Shown once. Store it securely; only its hash is kept.")


# ---- candidates ------------------------------------------------------------
class CandidateCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=255)


class CandidateOut(Timestamped):
    tenant_id: UUID
    email: EmailStr
    full_name: str


# ---- assessments -----------------------------------------------------------
class TestCaseSpec(BaseModel):
    __test__ = False  # not a pytest test class
    input: str = Field("", max_length=100_000)
    expected_output: str = Field(..., max_length=100_000)
    is_hidden: bool = False


class RubricCriterionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    weight: int = Field(..., gt=0, le=100)


class RubricCriterionOut(Timestamped):
    question_id: UUID
    name: str
    description: Optional[str] = None
    weight: int


class QuestionCreate(BaseModel):
    prompt: str = Field(..., min_length=1)
    starter_code: str = ""
    test_cases: List[TestCaseSpec] = Field(..., min_length=1, max_length=100)
    max_score: int = Field(100, gt=0, le=10_000)
    rubric_criteria: List[RubricCriterionCreate] = Field(default_factory=list, max_length=20)


class QuestionOut(Timestamped):
    assessment_id: UUID
    position: int
    prompt: str
    starter_code: str
    test_cases: List[TestCaseSpec]
    max_score: int
    rubric_criteria: List[RubricCriterionOut] = Field(default_factory=list)


class AssessmentCreate(BaseModel):
title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    language: Literal["python"] = "python"
    time_limit_seconds: int = Field(3600, gt=0, le=86_400)
    questions: List[QuestionCreate] = Field(default_factory=list, max_length=100)


class AssessmentSummary(Timestamped):
    tenant_id: UUID
    title: str
    description: Optional[str] = None
    language: str
    time_limit_seconds: int


class AssessmentOut(AssessmentSummary):
    questions: List[QuestionOut] = Field(default_factory=list)


# ---- submissions and evaluations ---------------------------------------------
class SubmissionCreate(BaseModel):
    candidate_id: UUID
    question_id: UUID
    code: str = Field(..., min_length=1, max_length=100_000)


class SubmissionOut(Timestamped):
    tenant_id: UUID
    candidate_id: UUID
    question_id: UUID
    status: SubmissionStatus
    error_message: Optional[str] = None


class EvaluationOut(Timestamped):
    tenant_id: UUID
    submission_id: UUID
    correctness_score: float
    rubric_score: Optional[float] = None
    total_score: float
    points_awarded: float
    max_score: int
    breakdown: Dict[str, Any]


class RubricGradeIn(BaseModel):
    scores: Dict[UUID, float] = Field(..., min_length=1, description="criterion id -> score from 0 to 100")

    @field_validator("scores")
    @classmethod
    def scores_in_range(cls, v):
        if any(not 0 <= s <= 100 for s in v.values()):
            raise ValueError("Each score must be between 0 and 100")
        return v


# ============================================================================
# Business logic
# ============================================================================

class DomainError(Exception):
    pass


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


class Invalid(DomainError):
    pass


# ---- tenants ---------------------------------------------------------------
def provision_tenant(db: Session, name: str, slug: str) -> Tuple[Tenant, str]:
    raw_key, key_hash = generate_api_key()
    tenant = Tenant(name=name, slug=slug, api_key_hash=key_hash)
    db.add(tenant)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict("A tenant with this name or slug already exists")
    return tenant, raw_key


def authenticate_tenant(db: Session, raw_key: str) -> Optional[Tenant]:
    tenant = db.scalar(select(Tenant).where(Tenant.api_key_hash == hash_api_key(raw_key)))
    return tenant if tenant and tenant.is_active else None


# ---- candidates ------------------------------------------------------------
def create_candidate(db: Session, tenant_id: UUID, payload: CandidateCreate) -> Candidate:
    candidate = Candidate(tenant_id=tenant_id, email=str(payload.email).lower(), full_name=payload.full_name)
    db.add(candidate)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict("A candidate with this email already exists")
    return candidate


def list_candidates(db: Session, tenant_id: UUID, limit: int, offset: int) -> List[Candidate]:
    stmt = select(Candidate).where(Candidate.tenant_id == tenant_id).order_by(Candidate.created_at).limit(limit).offset(offset)
    return list(db.scalars(stmt))


# ---- assessments -----------------------------------------------------------
def _cache_key(tenant_id: UUID, assessment_id: UUID) -> str:
    return f"tenant:{tenant_id}:assessment:{assessment_id}"


def create_assessment(db: Session, tenant_id: UUID, payload: AssessmentCreate) -> Assessment:
    assessment = Assessment(tenant_id=tenant_id, title=payload.title, description=payload.description,
                            language=payload.language, time_limit_seconds=payload.time_limit_seconds)
    for position, q in enumerate(payload.questions):
        assessment.questions.append(Question(
            tenant_id=tenant_id, position=position, prompt=q.prompt, starter_code=q.starter_code,
            test_cases=[tc.model_dump() for tc in q.test_cases], max_score=q.max_score,
            rubric_criteria=[RubricCriterion(tenant_id=tenant_id, name=rc.name, description=rc.description,
                                             weight=rc.weight) for rc in q.rubric_criteria]))
    db.add(assessment)
    db.commit()
    return assessment


def get_assessment_data(db: Session, cache: Cache, tenant_id: UUID, assessment_id: UUID, ttl: int) -> Dict[str, Any]:
    """Full assessment (including hidden test cases) as a dict. Redaction happens in the API layer."""
    key = _cache_key(tenant_id, assessment_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    stmt = (select(Assessment)
            .options(selectinload(Assessment.questions).selectinload(Question.rubric_criteria))
            .where(Assessment.id == assessment_id, Assessment.tenant_id == tenant_id))
    assessment = db.scalar(stmt)
    if assessment is None:
        raise NotFound("Assessment not found")
    data = AssessmentOut.model_validate(assessment).model_dump(mode="json")
    cache.set(key, data, ttl)
    return data


def list_assessments(db: Session, tenant_id: UUID, limit: int, offset: int) -> List[Assessment]:
    stmt = select(Assessment).where(Assessment.tenant_id == tenant_id).order_by(Assessment.created_at).limit(limit).offset(offset)
    return list(db.scalars(stmt))


# ---- submissions -----------------------------------------------------------
def create_submission(db: Session, tenant_id: UUID, payload: SubmissionCreate) -> Submission:
    candidate = db.scalar(select(Candidate.id).where(Candidate.id == payload.candidate_id, Candidate.tenant_id == tenant_id))
    question = db.scalar(select(Question.id).where(Question.id == payload.question_id, Question.tenant_id == tenant_id))
    if candidate is None or question is None:     # same message either way: do not reveal other tenants' ids
        raise NotFound("Candidate or question not found")
    submission = Submission(tenant_id=tenant_id, candidate_id=payload.candidate_id, question_id=payload.question_id,
                            code=payload.code, status=SubmissionStatus.PENDING)
    db.add(submission)
    db.commit()
    return submission


def get_submission(db: Session, tenant_id: UUID, submission_id: UUID) -> Submission:
    submission = db.scalar(select(Submission).where(Submission.id == submission_id, Submission.tenant_id == tenant_id))
    if submission is None:
        raise NotFound("Submission not found")
    return submission


def process_submission(session_factory: "sessionmaker[Session]", sandbox: Sandbox, tenant_id: UUID,
                       submission_id: UUID) -> None:
  """Background job. Opens its own session; the request's session is already closed by then.
    Claims the submission atomically (pending to running) so duplicate runs are harmless."""
    with session_factory() as db:
        claimed = db.execute(
            update(Submission)
            .where(Submission.id == submission_id, Submission.tenant_id == tenant_id,
                   Submission.status == SubmissionStatus.PENDING)
            .values(status=SubmissionStatus.RUNNING))
        db.commit()
        if claimed.rowcount != 1:
            logger.info("Submission %s already claimed or missing; skipping", submission_id)
            return
        try:
            submission = db.scalar(select(Submission).where(Submission.id == submission_id))
            question = db.scalar(select(Question).where(Question.id == submission.question_id,
                                                        Question.tenant_id == tenant_id))
            if question is None:
                raise NotFound("Question not found")
            result = evaluate_submission(sandbox, submission.code, question.test_cases)
            total = total_score(result["correctness_score"], None)
            db.add(Evaluation(
                tenant_id=tenant_id, submission_id=submission.id, correctness_score=result["correctness_score"],
                total_score=total, points_awarded=points(total, question.max_score),
                max_score=question.max_score, breakdown=result))
            submission.status = SubmissionStatus.EVALUATED
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Evaluation failed for submission %s", submission_id)
            db.execute(update(Submission).where(Submission.id == submission_id)
                       .values(status=SubmissionStatus.FAILED, error_message="Evaluation failed; see server logs"))
            db.commit()


def get_evaluation(db: Session, tenant_id: UUID, submission_id: UUID) -> Evaluation:
    evaluation = db.scalar(select(Evaluation).where(Evaluation.submission_id == submission_id,
                                                    Evaluation.tenant_id == tenant_id))
    if evaluation is None:
        raise NotFound("Evaluation not found")
    return evaluation


def grade_rubric(db: Session, tenant_id: UUID, submission_id: UUID, scores: Dict[UUID, float],
                 correctness_weight: float) -> Evaluation:
    """Record a manual rubric grade and recompute the total. Every criterion must be scored."""
    evaluation = get_evaluation(db, tenant_id, submission_id)
    submission = get_submission(db, tenant_id, submission_id)
    criteria = list(db.scalars(select(RubricCriterion).where(
        RubricCriterion.question_id == submission.question_id, RubricCriterion.tenant_id == tenant_id)))
    if not criteria:
        raise Invalid("This question has no rubric criteria")
    if set(scores) != {c.id for c in criteria}:
        raise Invalid("Provide a score for every rubric criterion, and only for them")
    str_scores = {str(k): float(v) for k, v in scores.items()}
    rubric = rubric_percent(str_scores, {str(c.id): c.weight for c in criteria})
    total = total_score(evaluation.correctness_score, rubric, correctness_weight)
    evaluation.rubric_scores = str_scores
    evaluation.rubric_score = rubric
    evaluation.total_score = total
    evaluation.points_awarded = points(total, evaluation.max_score)
    db.commit()
    return evaluation


# ============================================================================
# HTTP routes
# ============================================================================

router = APIRouter()


# ---- dependencies ----------------------------------------------------------
def get_db(request: Request) -> Iterator[Session]:
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def get_current_tenant(x_api_key: str = Header(..., alias="X-API-Key"), db: Session = Depends(get_db)) -> Tenant:
    tenant = authenticate_tenant(db, x_api_key)
    if tenant is None:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    return tenant


def require_admin(request: Request, x_admin_key: str = Header("", alias="X-Admin-Key")) -> None:
    expected = request.app.state.settings.ADMIN_API_KEY.get_secret_value()
    if not expected:
        raise HTTPException(status_code=403, detail="Tenant provisioning is disabled")
    if not verify_admin_key(x_admin_key, expected):
        raise HTTPException(status_code=401, detail="Invalid admin key")


# ---- tenants ---------------------------------------------------------------
@router.post("/tenants", response_model=TenantProvisioned, status_code=201, tags=["tenants"],
             dependencies=[Depends(require_admin)])
def create_tenant_endpoint(payload: TenantCreate, db: Session = Depends(get_db)):
    tenant, raw_key = provision_tenant(db, payload.name, payload.slug)
    return TenantProvisioned(id=tenant.id, name=tenant.name, slug=tenant.slug, api_key=raw_key)


# ---- candidates ------------------------------------------------------------
@router.post("/candidates", response_model=CandidateOut, status_code=201, tags=["candidates"])
def create_candidate_endpoint(payload: CandidateCreate, tenant: Tenant = Depends(get_current_tenant),
                     db: Session = Depends(get_db)):
    return create_candidate(db, tenant.id, payload)


@router.get("/candidates", response_model=List[CandidateOut], tags=["candidates"])
def list_candidates_endpoint(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    tenant: Tenant = Depends(get_current_tenant), db: Session = Depends(get_db)):
    return list_candidates(db, tenant.id, limit, offset)


# ---- assessments -----------------------------------------------------------
@router.post("/assessments", response_model=AssessmentOut, status_code=201, tags=["assessments"])
def create_assessment_endpoint(payload: AssessmentCreate, request: Request, tenant: Tenant = Depends(get_current_tenant),
                      db: Session = Depends(get_db)):
    assessment = create_assessment(db, tenant.id, payload)
    data = get_assessment_data(db, request.app.state.cache, tenant.id, assessment.id,
                                        request.app.state.settings.CACHE_TTL_SECONDS)
    return data   # the creator sees the full assessment, including hidden test cases


@router.get("/assessments", response_model=List[AssessmentSummary], tags=["assessments"])
def list_assessments_endpoint(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                     tenant: Tenant = Depends(get_current_tenant), db: Session = Depends(get_db)):
    return list_assessments(db, tenant.id, limit, offset)


@router.get("/assessments/{assessment_id}", response_model=AssessmentOut, tags=["assessments"])
def read_assessment_endpoint(assessment_id: UUID, request: Request,
                    include_hidden: bool = Query(False, description="Include hidden test cases (authoring only)"),
                    tenant: Tenant = Depends(get_current_tenant), db: Session = Depends(get_db)):
    data = get_assessment_data(db, request.app.state.cache, tenant.id, assessment_id,
                                        request.app.state.settings.CACHE_TTL_SECONDS)
    return redact_assessment(data, include_hidden)


# ---- submissions -----------------------------------------------------------
@router.post("/submissions", response_model=SubmissionOut, status_code=202, tags=["submissions"])
def create_submission_endpoint(payload: SubmissionCreate, background: BackgroundTasks, request: Request,
                      tenant: Tenant = Depends(get_current_tenant), db: Session = Depends(get_db)):
    """Accepts the submission and evaluates it in the background. Poll GET /submissions/{id}."""
    submission = create_submission(db, tenant.id, payload)
    background.add_task(process_submission, request.app.state.session_factory,
                        request.app.state.sandbox, tenant.id, submission.id)
    return submission


@router.get("/submissions/{submission_id}", response_model=SubmissionOut, tags=["submissions"])
def read_submission_endpoint(submission_id: UUID, tenant: Tenant = Depends(get_current_tenant), db: Session = Depends(get_db)):
    return get_submission(db, tenant.id, submission_id)


@router.get("/submissions/{submission_id}/evaluation", response_model=EvaluationOut, tags=["submissions"])
def read_evaluation_endpoint(submission_id: UUID, include_hidden: bool = Query(False),
                    tenant: Tenant = Depends(get_current_tenant), db: Session = Depends(get_db)):
    evaluation = get_evaluation(db, tenant.id, submission_id)
    out = EvaluationOut.model_validate(evaluation)
    out.breakdown = redact_breakdown(evaluation.breakdown, include_hidden)
    return out


@router.put("/submissions/{submission_id}/rubric", response_model=EvaluationOut, tags=["submissions"])
def grade_rubric_endpoint(submission_id: UUID, payload: RubricGradeIn, request: Request,
                 tenant: Tenant = Depends(get_current_tenant), db: Session = Depends(get_db)):
    evaluation = grade_rubric(db, tenant.id, submission_id, payload.scores,
                                       request.app.state.settings.CORRECTNESS_WEIGHT)
    out = EvaluationOut.model_validate(evaluation)
    out.breakdown = redact_breakdown(evaluation.breakdown, include_hidden=False)
    return out


# ============================================================================
# Application factory
# ============================================================================

def create_app(settings: Optional[Settings] = None, *, sandbox: Optional[Sandbox] = None,
               cache: Optional[Cache] = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine = build_engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.AUTO_CREATE_TABLES:
            Base.metadata.create_all(engine)
        yield
        engine.dispose()

    app = FastAPI(title=settings.PROJECT_NAME, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = build_session_factory(engine)
    app.state.sandbox = sandbox or DockerSandbox(settings)
    app.state.cache = cache or build_cache(settings)

    status_for = {NotFound: 404, Conflict: 409, Invalid: 422}

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(status_code=status_for.get(type(exc), 400), content={"detail": str(exc)})

    @app.get("/health/live", tags=["health"])
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready():
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Readiness check failed")
            return JSONResponse(status_code=503, content={"status": "database unavailable"})
        return {"status": "ready"}

    app.include_router(router, prefix=settings.API_V1_STR)
    return app


# ============================================================================
# Entry point
# ============================================================================

_app: Optional[FastAPI] = None


def __getattr__(name: str):
    """Build the default app lazily so that importing this module (for example in tests, which call
    create_app with their own settings) does not open a database engine. `uvicorn main:app` triggers this."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000)
