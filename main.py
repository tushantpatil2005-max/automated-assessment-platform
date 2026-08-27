"""
Automated Assessment & Evaluation Platform
-------------------------------------------
Single-file version for easy copy/paste into a repo.

Stack: FastAPI, SQLAlchemy (PostgreSQL), Redis, Docker (sandboxed execution)

Run:
    pip install fastapi uvicorn sqlalchemy psycopg2-binary redis pydantic \
                pydantic-settings email-validator docker
    uvicorn main:app --reload

Requires a running PostgreSQL instance, a running Redis instance, and a
reachable Docker daemon (for sandboxed code execution) — see the README in
the full multi-file version for docker-compose setup.
"""

import enum
import json
import os
import tempfile
import time
import uuid
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional
from uuid import UUID

import redis
from fastapi import (APIRouter, BackgroundTasks, Depends, FastAPI, Header,
                      HTTPException)
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import (JSON, Boolean, Column, DateTime, Enum, Float,
                         ForeignKey, Index, Integer, String, Text,
                         create_engine)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import (Session, declarative_base, joinedload,
                             relationship, sessionmaker)

import docker
from docker.errors import APIError, ContainerError

# ============================================================================
# Settings
# ============================================================================

class Settings(BaseSettings):
    PROJECT_NAME: str = "Automated Assessment & Evaluation Platform"
    API_V1_STR: str = "/api/v1"

    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "assessment_platform"

    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_TTL_SECONDS: int = 300

    SANDBOX_IMAGE: str = "python:3.11-slim"
    SANDBOX_MEM_LIMIT: str = "128m"
    SANDBOX_TIMEOUT_SECONDS: int = 10
    SANDBOX_NETWORK_DISABLED: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# ============================================================================
# Database
# ============================================================================

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============================================================================
# Redis cache-aside helper
# ============================================================================

_redis_pool = redis.ConnectionPool(
    host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=0, decode_responses=True
)


def get_redis() -> redis.Redis:
    return redis.Redis(connection_pool=_redis_pool)


def cache_get(key: str):
    raw = get_redis().get(key)
    return json.loads(raw) if raw is not None else None


def cache_set(key: str, value, ttl: int = None) -> None:
    get_redis().setex(key, ttl or settings.REDIS_TTL_SECONDS, json.dumps(value, default=str))


def cache_invalidate(key: str) -> None:
    get_redis().delete(key)


# ============================================================================
# Models
# ============================================================================

class BaseModelMixin(Base):
    __abstract__ = True
    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class TenantScopedModel(BaseModelMixin):
    __abstract__ = True
    tenant_id = Column(PG_UUID(as_uuid=True), index=True, nullable=False)


class Tenant(BaseModelMixin):
    __tablename__ = "tenants"
    name = Column(String(255), nullable=False, unique=True)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    is_active = Column(Boolean, default=True, nullable=False)


class Candidate(TenantScopedModel):
    __tablename__ = "candidates"
    email = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=False)
    __table_args__ = (Index("ix_candidates_tenant_email", "tenant_id", "email"),)


class Assessment(TenantScopedModel):
    __tablename__ = "assessments"
    title = Column(String(255), nullable=False)
    description = Column(Text)
    language = Column(String(50), nullable=False, default="python")
    time_limit_seconds = Column(Integer, default=3600)
    questions = relationship("Question", back_populates="assessment", cascade="all, delete-orphan")
    __table_args__ = (Index("ix_assessments_tenant_title", "tenant_id", "title"),)


class Question(TenantScopedModel):
    __tablename__ = "questions"
    assessment_id = Column(PG_UUID(as_uuid=True), ForeignKey("assessments.id"), nullable=False, index=True)
    prompt = Column(Text, nullable=False)
    starter_code = Column(Text, default="")
    test_cases = Column(JSON, nullable=False, default=list)  # [{"input", "expected_output", "is_hidden"}]
    max_score = Column(Integer, default=100)
    assessment = relationship("Assessment", back_populates="questions")
    rubric_criteria = relationship("RubricCriterion", back_populates="question", cascade="all, delete-orphan")


class RubricCriterion(TenantScopedModel):
    __tablename__ = "rubric_criteria"
    question_id = Column(PG_UUID(as_uuid=True), ForeignKey("questions.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    weight = Column(Integer, nullable=False, default=1)
    question = relationship("Question", back_populates="rubric_criteria")


class SubmissionStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    EVALUATED = "evaluated"
    FAILED = "failed"


class Submission(TenantScopedModel):
    __tablename__ = "submissions"
    candidate_id = Column(PG_UUID(as_uuid=True), ForeignKey("candidates.id"), nullable=False, index=True)
    question_id = Column(PG_UUID(as_uuid=True), ForeignKey("questions.id"), nullable=False, index=True)
    code = Column(Text, nullable=False)
    status = Column(Enum(SubmissionStatus), default=SubmissionStatus.PENDING, nullable=False)
    stdout = Column(Text)
    stderr = Column(Text)
    execution_time_ms = Column(Float)
    evaluation = relationship("Evaluation", back_populates="submission", uselist=False, cascade="all, delete-orphan")
    __table_args__ = (Index("ix_submissions_tenant_status", "tenant_id", "status"),)


class Evaluation(TenantScopedModel):
    __tablename__ = "evaluations"
    submission_id = Column(PG_UUID(as_uuid=True), ForeignKey("submissions.id"), nullable=False, unique=True, index=True)
    correctness_score = Column(Float, default=0.0)
    rubric_score = Column(Float, default=0.0)
    total_score = Column(Float, default=0.0)
    breakdown = Column(JSON, default=dict)
    submission = relationship("Submission", back_populates="evaluation")


# ============================================================================
# Schemas
# ============================================================================

class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: datetime
    updated_at: datetime


class CandidateCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=255)


class CandidateOut(ORMBase):
    tenant_id: UUID
    email: EmailStr
    full_name: str


class TestCase(BaseModel):
    input: Any = ""
    expected_output: Any
    is_hidden: bool = False


class RubricCriterionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    weight: int = Field(..., gt=0, le=100)


class RubricCriterionOut(ORMBase):
    question_id: UUID
    name: str
    description: Optional[str] = None
    weight: int


class QuestionCreate(BaseModel):
    prompt: str = Field(..., min_length=1)
    starter_code: str = ""
    test_cases: List[TestCase] = Field(default_factory=list)
    max_score: int = Field(100, gt=0)
    rubric_criteria: List[RubricCriterionCreate] = Field(default_factory=list)

    @field_validator("test_cases")
    @classmethod
    def require_at_least_one_test_case(cls, v):
        if not v:
            raise ValueError("Each question needs at least one test case")
        return v


class QuestionOut(ORMBase):
    assessment_id: UUID
    prompt: str
    starter_code: str
    test_cases: List[TestCase]
    max_score: int
    rubric_criteria: List[RubricCriterionOut] = Field(default_factory=list)


class AssessmentCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    language: str = Field("python", pattern="^(python)$")
    time_limit_seconds: int = Field(3600, gt=0, le=86400)
    questions: List[QuestionCreate] = Field(default_factory=list)


class AssessmentOut(ORMBase):
    tenant_id: UUID
    title: str
    description: Optional[str] = None
    language: str
    time_limit_seconds: int
    questions: List[QuestionOut] = Field(default_factory=list)


class SubmissionCreate(BaseModel):
    candidate_id: UUID
    question_id: UUID
    code: str = Field(..., min_length=1, max_length=100_000)


class SubmissionOut(ORMBase):
    tenant_id: UUID
    candidate_id: UUID
    question_id: UUID
    status: SubmissionStatus
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    execution_time_ms: Optional[float] = None


class EvaluationOut(ORMBase):
    tenant_id: UUID
    submission_id: UUID
    correctness_score: float
    rubric_score: float
    total_score: float
    breakdown: Dict[str, Any]


# ============================================================================
# Sandboxed code execution
# ============================================================================

class SandboxExecutionError(Exception):
    pass


class SandboxTimeoutError(SandboxExecutionError):
    pass


_docker_client = None


def _get_docker_client():
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def run_python_code(code: str, stdin_input: str = "", timeout: int = None) -> dict:
    """Runs `code` inside an isolated, network-disabled, resource-limited
    Docker container and returns stdout/stderr/exit code/timing."""
    timeout = timeout or settings.SANDBOX_TIMEOUT_SECONDS
    client = _get_docker_client()

    with tempfile.TemporaryDirectory() as tmp_dir:
        with open(os.path.join(tmp_dir, "solution.py"), "w") as f:
            f.write(code)
        with open(os.path.join(tmp_dir, "stdin.txt"), "w") as f:
            f.write(stdin_input or "")

        start = time.monotonic()
        container = None
        try:
            container = client.containers.run(
                image=settings.SANDBOX_IMAGE,
                command=["sh", "-c", "python /sandbox/solution.py < /sandbox/stdin.txt"],
                volumes={tmp_dir: {"bind": "/sandbox", "mode": "ro"}},
                working_dir="/sandbox",
                network_disabled=settings.SANDBOX_NETWORK_DISABLED,
                mem_limit=settings.SANDBOX_MEM_LIMIT,
                nano_cpus=int(0.5 * 1e9),
                pids_limit=64,
                read_only=True,
                tmpfs={"/tmp": "size=16m"},
                detach=True,
                security_opt=["no-new-privileges"],
            )
            try:
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode", 1)
            except Exception as exc:
                container.kill()
                raise SandboxTimeoutError(f"Execution exceeded {timeout}s timeout") from exc

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            elapsed_ms = (time.monotonic() - start) * 1000
            return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code, "execution_time_ms": elapsed_ms}
        except (ContainerError, APIError) as exc:
            raise SandboxExecutionError(str(exc)) from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass


# ============================================================================
# Rubric-based scoring
# ============================================================================

def _normalize(output: str) -> str:
    return output.strip()


def evaluate_test_cases(code: str, test_cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    results = []
    passed = 0
    for idx, case in enumerate(test_cases):
        stdin_input = str(case.get("input", ""))
        expected = str(case.get("expected_output", ""))
        is_hidden = bool(case.get("is_hidden", False))
        try:
            run_result = run_python_code(code, stdin_input=stdin_input)
            actual = _normalize(run_result["stdout"])
            success = run_result["exit_code"] == 0 and actual == _normalize(expected)
            results.append({
                "test_case_index": idx, "passed": success,
                "stderr": "" if success else run_result["stderr"],
                "execution_time_ms": run_result["execution_time_ms"], "hidden": is_hidden,
            })
            passed += int(success)
        except SandboxTimeoutError:
            results.append({"test_case_index": idx, "passed": False, "stderr": "Execution timed out", "hidden": is_hidden})
        except SandboxExecutionError as exc:
            results.append({"test_case_index": idx, "passed": False, "stderr": str(exc), "hidden": is_hidden})

    total = len(test_cases) or 1
    correctness_score = round((passed / total) * 100, 2)
    return {"correctness_score": correctness_score, "results": results, "passed": passed, "total": total}


def evaluate_rubric(criteria_scores: Dict[str, float], criteria_weights: Dict[str, int]) -> Dict[str, Any]:
    total_weight = sum(criteria_weights.values()) or 1
    weighted_sum = sum(criteria_scores.get(cid, 0) * w for cid, w in criteria_weights.items())
    return {"rubric_score": round(weighted_sum / total_weight, 2), "per_criterion": criteria_scores}


def compute_total_score(correctness_score: float, rubric_score: float, correctness_weight: float = 0.7) -> float:
    rubric_weight = 1 - correctness_weight
    return round(correctness_score * correctness_weight + rubric_score * rubric_weight, 2)


# ============================================================================
# CRUD
# ============================================================================

def _assessment_cache_key(tenant_id: UUID, assessment_id: UUID) -> str:
    return f"tenant:{tenant_id}:assessment:{assessment_id}"


def get_assessment(db: Session, tenant_id: UUID, assessment_id: UUID):
    key = _assessment_cache_key(tenant_id, assessment_id)
    cached = cache_get(key)
    if cached is not None:
        return cached

    assessment = (
        db.query(Assessment)
        .options(joinedload(Assessment.questions).joinedload(Question.rubric_criteria))
        .filter(Assessment.id == assessment_id, Assessment.tenant_id == tenant_id)
        .first()
    )
    if not assessment:
        return None

    data = AssessmentOut.model_validate(assessment).model_dump(mode="json")
    cache_set(key, data)
    return data


def create_assessment(db: Session, tenant_id: UUID, payload: AssessmentCreate) -> Assessment:
    assessment = Assessment(
        tenant_id=tenant_id, title=payload.title, description=payload.description,
        language=payload.language, time_limit_seconds=payload.time_limit_seconds,
    )
    db.add(assessment)
    db.flush()

    for q in payload.questions:
        question = Question(
            tenant_id=tenant_id, assessment_id=assessment.id, prompt=q.prompt,
            starter_code=q.starter_code, test_cases=[tc.model_dump() for tc in q.test_cases],
            max_score=q.max_score,
        )
        db.add(question)
        db.flush()
        for rc in q.rubric_criteria:
            db.add(RubricCriterion(
                tenant_id=tenant_id, question_id=question.id,
                name=rc.name, description=rc.description, weight=rc.weight,
            ))

    db.commit()
    db.refresh(assessment)
    return assessment


def create_submission(db: Session, tenant_id: UUID, payload: SubmissionCreate) -> Submission:
    submission = Submission(
        tenant_id=tenant_id, candidate_id=payload.candidate_id,
        question_id=payload.question_id, code=payload.code, status=SubmissionStatus.PENDING,
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return submission


def process_submission(db: Session, tenant_id: UUID, submission_id: UUID) -> Submission:
    submission = db.query(Submission).filter(
        Submission.id == submission_id, Submission.tenant_id == tenant_id
    ).first()
    if not submission:
        raise ValueError("Submission not found")

    submission.status = SubmissionStatus.RUNNING
    db.commit()

    question = db.query(Question).filter(Question.id == submission.question_id).first()
    if not question:
        submission.status = SubmissionStatus.FAILED
        submission.stderr = "Question not found"
        db.commit()
        raise ValueError("Question not found")

    try:
        result = evaluate_test_cases(submission.code, question.test_cases)
        total_score = compute_total_score(result["correctness_score"], rubric_score=0.0)

        submission.status = SubmissionStatus.EVALUATED
        submission.stdout = "\n".join(str(r) for r in result["results"])
        db.add(submission)

        evaluation = Evaluation(
            tenant_id=tenant_id, submission_id=submission.id,
            correctness_score=result["correctness_score"], rubric_score=0.0,
            total_score=total_score, breakdown=result,
        )
        db.add(evaluation)
        db.commit()
        db.refresh(submission)
    except Exception as exc:
        submission.status = SubmissionStatus.FAILED
        submission.stderr = str(exc)
        db.commit()
        raise

    return submission


# ============================================================================
# API layer
# ============================================================================

def get_tenant_id(x_tenant_id: str = Header(..., alias="X-Tenant-ID")) -> UUID:
    try:
        return UUID(x_tenant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid X-Tenant-ID header")


router = APIRouter()


@router.post("/assessments", response_model=AssessmentOut, status_code=201, tags=["assessments"])
def create_assessment_endpoint(payload: AssessmentCreate, tenant_id: UUID = Depends(get_tenant_id), db: Session = Depends(get_db)):
    return create_assessment(db, tenant_id, payload)


@router.get("/assessments/{assessment_id}", response_model=AssessmentOut, tags=["assessments"])
def read_assessment_endpoint(assessment_id: UUID, tenant_id: UUID = Depends(get_tenant_id), db: Session = Depends(get_db)):
    assessment = get_assessment(db, tenant_id, assessment_id)
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return assessment


@router.post("/candidates", response_model=CandidateOut, status_code=201, tags=["candidates"])
def create_candidate_endpoint(payload: CandidateCreate, tenant_id: UUID = Depends(get_tenant_id), db: Session = Depends(get_db)):
    candidate = Candidate(tenant_id=tenant_id, email=payload.email, full_name=payload.full_name)
    db.add(candidate)
    db.commit()
    db.refresh(candidate)
    return candidate


@router.post("/submissions", response_model=SubmissionOut, status_code=202, tags=["submissions"])
def submit_code_endpoint(payload: SubmissionCreate, background_tasks: BackgroundTasks, tenant_id: UUID = Depends(get_tenant_id), db: Session = Depends(get_db)):
    submission = create_submission(db, tenant_id, payload)
    background_tasks.add_task(process_submission, db, tenant_id, submission.id)
    return submission


@router.get("/submissions/{submission_id}", response_model=SubmissionOut, tags=["submissions"])
def get_submission_endpoint(submission_id: UUID, tenant_id: UUID = Depends(get_tenant_id), db: Session = Depends(get_db)):
    submission = db.query(Submission).filter(Submission.id == submission_id, Submission.tenant_id == tenant_id).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    return submission


@router.get("/evaluations/submission/{submission_id}", response_model=EvaluationOut, tags=["evaluations"])
def get_evaluation_endpoint(submission_id: UUID, tenant_id: UUID = Depends(get_tenant_id), db: Session = Depends(get_db)):
    evaluation = db.query(Evaluation).filter(Evaluation.submission_id == submission_id, Evaluation.tenant_id == tenant_id).first()
    if not evaluation:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return evaluation


app = FastAPI(title=settings.PROJECT_NAME)
app.include_router(router, prefix=settings.API_V1_STR)


@app.get("/health", tags=["health"])
def health_check():
    return {"status": "ok"}


@app.on_event("startup")
def create_tables():
    Base.metadata.create_all(bind=engine)