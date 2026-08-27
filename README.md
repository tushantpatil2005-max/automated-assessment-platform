Automated Assessment & Evaluation Platform
A multi-tenant REST API for candidate coding assessments. Submissions run in
sandboxed Docker containers against test cases, and results are combined
with rubric-based scoring into a single evaluation.
Stack: Python, FastAPI, PostgreSQL, Redis, Docker, SQLAlchemy, Pydantic
Features
Multi-tenant architecture — every table is scoped by tenant_id,
resolved per-request from an X-Tenant-ID header
Sandboxed code execution — candidate code runs in ephemeral,
network-disabled Docker containers with memory/CPU/PID limits and a hard
timeout, guaranteed cleanup after every run
Rubric-based evaluation — automated test-case correctness blended
with weighted rubric-criterion scoring into a final score
Redis caching — cache-aside pattern on hot assessment reads, with
explicit invalidation on writes
Postgres indexing — composite indexes (e.g. (tenant_id, status) on
submissions) keep tenant-scoped queries off sequential scans
Request validation — Pydantic v2 schemas reject malformed payloads
(invalid emails, empty test-case lists, out-of-range weights) at the API
boundary
Requirements
Python 3.11+
A running PostgreSQL instance
A running Redis instance
A reachable Docker daemon (for sandboxed code execution)
Setup
Bash
Configure via environment variables or a .env file in the project root:
Env
Run it:
Bash
Tables are created automatically on startup. Interactive API docs:
http://localhost:8000/docs
Architecture
Code
Sandboxed code execution
Each submission spins up a throwaway container with:
network_disabled=True — no outbound network access
mem_limit / nano_cpus / pids_limit — bounded resource usage
read_only root filesystem + a small tmpfs for /tmp
a hard wall-clock timeout that force-kills runaway processes
guaranteed container removal in a finally block
Rubric-based evaluation
evaluate_test_cases runs the submission against each test case and
computes a correctness percentage
evaluate_rubric computes a weighted average across rubric criteria
compute_total_score blends the two (default 70% correctness / 30%
rubric) into the score stored on the evaluation record
Example flow
Bash
Notes on scope
Only Python submissions are executed by the sandbox in this version;
extending to other languages means adding a language→base-image map to
the executor.
Submission evaluation runs as a FastAPI BackgroundTask for simplicity.
At higher throughput, swap this for a real queue (e.g. Celery/RQ backed
by Redis) so evaluation work survives process restarts.
Auth is stubbed via an X-Tenant-ID header; production would verify a
signed JWT and derive the tenant from its claims.
Tables are created via Base.metadata.create_all on startup rather than
migrations — fine for a demo, but swap in Alembic for anything long-lived.
License
MIT