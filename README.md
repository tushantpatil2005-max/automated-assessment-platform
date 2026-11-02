# Automated Assessment & Evaluation Platform

A multi-tenant API for coding assessments. Tenants create assessments with test cases, candidates submit
Python solutions, and each submission runs in an isolated Docker sandbox. Scores come from the test results,
with an optional manual rubric grade on top.

The whole application is a single file, `main.py`.

**Stack:** FastAPI, SQLAlchemy 2.0 (PostgreSQL), Redis (cache), Docker (sandbox), Pydantic v2.

## Features

- Multi-tenant isolation: every record belongs to a tenant, and every query is filtered by it.
- API-key authentication for tenants; an admin key for creating tenants.
- Assessments with questions, visible and hidden test cases, and weighted rubric criteria.
- Asynchronous evaluation: `POST /submissions` returns immediately and the result is polled.
- Sandboxed execution with no network, a read-only filesystem, and memory, CPU, process, time and output limits.
- Deterministic scoring from test results, plus manual rubric grading.
- Redis caching of assessments (failures never break a request) and paginated list endpoints.

## Requirements

- Python 3.10 or newer
- PostgreSQL
- Redis
- A reachable Docker daemon (to run submissions)

```bash
pip install "fastapi>=0.110" "uvicorn[standard]" "sqlalchemy>=2.0.25" psycopg2-binary \
            "redis>=5" "docker>=7" "pydantic[email]>=2.5" "pydantic-settings>=2.1"
```

## Setup

1. Create a `.env` file next to `main.py` (see [Configuration](#configuration)):

   ```env
   ADMIN_API_KEY=change-me-to-a-long-random-value
   POSTGRES_USER=postgres
   POSTGRES_PASSWORD=change-me
   POSTGRES_SERVER=localhost
   POSTGRES_DB=assessment_platform
   AUTO_CREATE_TABLES=true
   ```

   `AUTO_CREATE_TABLES=true` creates the tables on startup. It is meant for development; use migrations
   (for example Alembic) in production. Never commit your `.env` file.

2. Pull the sandbox image once: `docker pull python:3.11-slim`
3. Start the server: `uvicorn main:app` (or `python main.py`). Interactive docs are at `http://localhost:8000/docs`.

## Quick start

```bash
# 1. Create a tenant. The api_key in the response is shown once; store it.
curl -X POST localhost:8000/api/v1/tenants \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"Acme","slug":"acme"}'

export KEY=apk_...   # the key you just received

# 2. Create a candidate
curl -X POST localhost:8000/api/v1/candidates -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"email":"sam@example.com","full_name":"Sam Rivera"}'

# 3. Create an assessment with one question and a hidden test case
curl -X POST localhost:8000/api/v1/assessments -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{
  "title": "Warm-up",
  "questions": [{
    "prompt": "Read an integer and print twice its value.",
    "max_score": 50,
    "test_cases": [
      {"input": "2", "expected_output": "4"},
      {"input": "5", "expected_output": "10", "is_hidden": true}]}]}'

# 4. Submit a solution (use the candidate and question ids from the responses above)
curl -X POST localhost:8000/api/v1/submissions -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"candidate_id":"<id>","question_id":"<id>","code":"print(int(input()) * 2)"}'

# 5. Poll the submission, then read the evaluation
curl localhost:8000/api/v1/submissions/<submission_id>            -H "X-API-Key: $KEY"
curl localhost:8000/api/v1/submissions/<submission_id>/evaluation -H "X-API-Key: $KEY"
```

## API reference (`/api/v1`)

| Method and path | Auth | Description |
|---|---|---|
| `POST /tenants` | Admin key | Create a tenant and return its API key (shown once) |
| `POST /candidates` | API key | Create a candidate (email unique per tenant) |
| `GET /candidates` | API key | List candidates (`limit`, `offset`) |
| `POST /assessments` | API key | Create an assessment with questions, test cases and rubric criteria |
| `GET /assessments` | API key | List assessments (`limit`, `offset`) |
| `GET /assessments/{id}` | API key | Read an assessment. Hidden test cases are omitted unless `?include_hidden=true` |
| `POST /submissions` | API key | Accept a submission (`202`) and evaluate it in the background |
| `GET /submissions/{id}` | API key | Status: `pending`, `running`, `evaluated` or `failed` |
| `GET /submissions/{id}/evaluation` | API key | Scores and per-test breakdown. Hidden tests show pass or fail only unless `?include_hidden=true` |
| `PUT /submissions/{id}/rubric` | API key | Record a manual rubric grade: a score from 0 to 100 for every criterion |
| `GET /health/live`, `GET /health/ready` | None | Liveness, and readiness (database check) |

Authentication headers: `X-API-Key` for tenants, `X-Admin-Key` for tenant creation. Errors use standard HTTP
codes: `401` invalid key, `404` not found (including other tenants' ids), `409` conflict, `422` invalid input.

## How scoring works

| Score | Definition |
|---|---|
| Correctness | Percentage of test cases passed. Output is compared after normalising line endings and trailing whitespace |
| Rubric | Weighted average of the manual criterion scores (0 to 100 each), set through the rubric endpoint |
| Total | Without a rubric grade: equal to correctness. With one: `correctness x 0.7 + rubric x 0.3` (weight set by `CORRECTNESS_WEIGHT`) |
| Points awarded | Total, scaled to the question's `max_score` |

A test case fails if the program exits with an error, times out, runs out of memory, produces too much output,
or prints something different from the expected output.

## Sandbox

Each test case runs in a fresh container with: no network, a read-only root filesystem, all Linux capabilities
dropped, a non-root user, `no-new-privileges`, and limits on memory, CPU, processes, run time and output size.
The container is always removed afterwards.

> Giving the API access to the Docker socket is a privileged setup. For untrusted code at scale, run
> submissions with a stronger boundary (gVisor or Firecracker) or in a separate execution service.

## Configuration

All settings are environment variables (or `.env`).

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_API_KEY` | empty | Enables `POST /tenants` when set |
| `DATABASE_URL` | unset | Full SQLAlchemy URL; overrides the `POSTGRES_*` values |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_SERVER` / `POSTGRES_PORT` / `POSTGRES_DB` | `postgres` / `postgres` / `localhost` / `5432` / `assessment_platform` | Database connection |
| `AUTO_CREATE_TABLES` | `false` | Create tables on startup (development only) |
| `CACHE_BACKEND` | `redis` | `redis`, `memory` or `none` |
| `REDIS_HOST` / `REDIS_PORT` | `localhost` / `6379` | Redis connection |
| `CACHE_TTL_SECONDS` | `300` | Assessment cache lifetime |
| `SANDBOX_IMAGE` | `python:3.11-slim` | Image used to run submissions |
| `SANDBOX_MEM_LIMIT` | `128m` | Memory limit per run |
| `SANDBOX_CPUS` | `0.5` | CPU limit per run |
| `SANDBOX_PIDS_LIMIT` | `64` | Process limit per run |
| `SANDBOX_TIMEOUT_SECONDS` | `10` | Time limit per test case |
| `SANDBOX_NETWORK_DISABLED` | `true` | Disable container networking |
| `SANDBOX_MAX_OUTPUT_BYTES` | `65536` | Output captured per run |
| `SANDBOX_WORKDIR` | system temp | Directory for run files. When the API itself runs in a container, use the same path on the host and in the container |
| `CORRECTNESS_WEIGHT` | `0.7` | Weight of correctness once a rubric grade exists |

## Project layout

`main.py` is organised top to bottom as: settings, security, scoring, sandbox, evaluator, cache, database,
models, schemas, services, routes, application factory. `test_main.py` holds the tests.

## Tests

```bash
pip install httpx
python -m unittest test_main -v
```

The tests cover scoring, redaction of hidden test cases, API-key handling, output capping, the evaluator,
authentication, tenant isolation, evaluation, timeouts and rubric grading. They run code in a local subprocess
instead of Docker, which is **not** a security boundary and exists only so the tests run without Docker.

## Production notes

- Evaluation runs inside the API process through background tasks. For production, move it to a queue worker
  (for example RQ or Celery on the existing Redis) and add recovery for submissions stuck in `running` after a crash.
- Not included: rate limiting, API-key rotation, audit logging, database migrations and metrics.
- Each test case starts its own container. This is simple and isolated, but slower than batching.
