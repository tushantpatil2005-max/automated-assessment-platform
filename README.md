Automated Assessment & Evaluation Platform
A multi-tenant REST API for candidate coding assessments: submissions run in sandboxed Docker containers against test cases, and results are combined with rubric-based scoring into a single evaluation.
Stack: Python, FastAPI, PostgreSQL, Redis, Docker, SQLAlchemy, Alembic, Pydantic
Architecture
Client ──▶ FastAPI ──▶ PostgreSQL (candidates, assessments, submissions, evaluations)
              │             ▲
              │             └── indexed on tenant_id (+ composite indexes,
              │                 e.g. (tenant_id, status) on submissions)
              │
              ├──▶ Redis (cache-aside for hot, read-heavy assessment lookups)
              │
              └──▶ Docker Engine (ephemeral, network-disabled sandbox
                    containers for untrusted candidate code execution)
Multi-tenancy
Every domain table inherits TenantScopedModel, which adds an indexed tenant_id column. All queries filter on (tenant_id, ...), and the composite indexes on submissions and candidates keep those lookups off sequential scans as data grows. The tenant is resolved per-request from an X-Tenant-ID header (see app/api/deps.py — swap in a verified JWT claim for production).
Sandboxed code execution (app/services/code_executor.py)
Candidate code never runs on the API host. Each submission spins up a throwaway container with:
network_disabled=True — no outbound network access
mem_limit / nano_cpus / pids_limit — bounded resource usage
read_only root filesystem + a small tmpfs for /tmp
a hard wall-clock timeout that force-kills runaway processes
guaranteed container removal in a finally block
Rubric-based evaluation (app/services/rubric_evaluator.py)
evaluate_test_cases runs the submission against each test case and computes a correctness percentage.
evaluate_rubric computes a weighted average across rubric criteria.
compute_total_score blends the two (default 70% correctness / 30% rubric) into the final score stored on Evaluation.
Caching layer (app/core/redis_client.py, app/crud/assessment.py)
Assessment reads follow a cache-aside pattern: check Redis first, fall back to a joinedloaded Postgres query on cache miss, then repopulate the cache with a TTL. Writes call invalidate_assessment_cache so stale data doesn't linger. This — combined with the composite indexes above — is the pattern behind cutting concurrent read latency.
Validation (app/schemas/)
Pydantic v2 models validate every request: emails, non-empty test-case lists, positive rubric weights, bounded time limits, etc., so malformed payloads are rejected at the API boundary rather than surfacing as downstream bugs.
Project layout
app/
  core/        # settings, DB engine/session, Redis client
  models/      # SQLAlchemy ORM models (tenant, candidate, assessment, submission, evaluation)
  schemas/     # Pydantic request/response models + validation
  services/    # code_executor (sandbox), rubric_evaluator (scoring)
  crud/        # DB access + cache-aside logic
  api/v1/      # FastAPI routers/endpoints
alembic/       # DB migrations
tests/         # pytest unit tests
docker-compose.yml
Dockerfile
Running it
cp .env.example .env
docker compose up --build
This starts the API (:8000), Postgres (:5432), and Redis (:6379). The API container mounts the host's Docker socket so it can launch sandbox containers for code execution — this is standard for CI runners and sandboxed-execution services, but be aware it grants the API container Docker-level access on the host; in production, isolate this behind a dedicated execution-worker service with its own restricted Docker context.
Interactive API docs: http://localhost:8000/docs
Database migrations
docker compose exec api alembic revision --autogenerate -m "init schema"
docker compose exec api alembic upgrade head
Running tests
pip install -r requirements.txt
pytest
(tests/ covers scoring math and schema validation directly, with no DB/Docker dependency; testing code_executor.py end-to-end requires a live Docker daemon.)
Example flow
TENANT=$(python3 -c "import uuid; print(uuid.uuid4())")

# 1. Create an assessment with a question + test case + rubric criterion
curl -X POST localhost:8000/api/v1/assessments \
  -H "X-Tenant-ID: $TENANT" -H "Content-Type: application/json" -d '{
    "title": "Backend Round 1",
    "questions": [{
      "prompt": "Read two ints from stdin, print their sum",
      "test_cases": [{"input": "2 3", "expected_output": "5"}],
      "rubric_criteria": [{"name": "Code clarity", "weight": 1}]
    }]
  }'

# 2. Register a candidate
curl -X POST localhost:8000/api/v1/candidates \
  -H "X-Tenant-ID: $TENANT" -H "Content-Type: application/json" \
  -d '{"email": "ada@example.com", "full_name": "Ada Lovelace"}'

# 3. Submit code (evaluated asynchronously in the sandbox)
curl -X POST localhost:8000/api/v1/submissions \
  -H "X-Tenant-ID: $TENANT" -H "Content-Type: application/json" -d '{
    "candidate_id": "<candidate_id>",
    "question_id": "<question_id>",
    "code": "a, b = map(int, input().split()); print(a + b)"
  }'

# 4. Poll for the evaluation
curl localhost:8000/api/v1/evaluations/submission/<submission_id> \
  -H "X-Tenant-ID: $TENANT"
Notes on scope
Only Python submissions are executed by the sandbox in this version (AssessmentCreate.language is currently restricted to "python"); extending to other languages means adding a language→base-image map in code_executor.py.
Submission evaluation runs as a FastAPI BackgroundTask for simplicity. At higher throughput, swap this for a real queue (e.g. Celery/RQ backed by Redis) so evaluation work survives API process restarts.
Auth is stubbed via an X-Tenant-ID header; production would verify a signed JWT and derive the tenant from its claims.